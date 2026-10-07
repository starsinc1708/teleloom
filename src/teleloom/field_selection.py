"""Bounded, schema-only response-field judgments; code owns every policy decision."""

import asyncio
import hashlib
import json
import math
import os
import re
from typing import Any, TypedDict

from .config import Settings
from .models import TeleloomError, utcnow
from .projection import catalog, resolve_fields
from .secrets import Secrets
from .store import Store

POLICY_VERSION = "response-fields-v4"
CACHE_STATE = "response_fields_cache"
CACHE_SECONDS = 86400
CACHE_ENTRIES = 128
DETAILS = ("full", "compact")


class SelectionQuestion(TypedDict):
    instructions: str
    criteria: dict[str, str]


def _summary_intent(intent: str) -> bool:
    return bool(re.search(r"\b(?:digest|summar\w*|recap)\b", intent)) or any(
        word in intent for word in ("дайджест", "сводк", "саммари", "суммариз")
    )


def _essential_content(request: str, schema: dict[str, Any]) -> set[str]:
    intent = request.casefold()
    metadata_only = bool(
        re.fullmatch(
            r"(?:(?:show|return|compare)\s+)?(?:only|just)\s+"
            r"(?:metadata|identifiers|ids|reactions?(?:\s+counts?)?|views?(?:\s+counts?)?|"
            r"(?:reply|comment)\s+counts?)(?:\s+please)?[.!]?"
            r"|(?:(?:покажи|верни|сравни)\s+)?только\s+"
            r"(?:метаданные|идентификаторы|сч[её]тчики|реакции|просмотры|"
            r"(?:число|количество)\s+комментариев)(?:\s+пожалуйста)?[.!]?",
            " ".join(intent.split()),
        )
    )
    # Suggestions on content-bearing readers keep content conservatively. Unknown
    # language/phrasing must not turn an ordinary read into identities alone.
    if metadata_only:
        return set()
    names = {field["name"] for field in schema["fields"]}
    essential = {"text"}
    if re.search(r"\b(?:links?|urls?|hyperlinks?)\b", intent) or "ссылк" in intent:
        # Text URLs can be hidden behind an entity label rather than in the caption.
        essential.add("entities")
        essential.add("rich_text")
    return essential & names


def _excluded_engagement(intent: str) -> set[str]:
    terms = {
        "reactions": r"reaction\w*|реакц\w*",
        "views": r"views?|просмотр\w*",
        "reply_count": r"repl(?:y|ies)|comment\w*|комментар\w*|ответ\w*",
    }
    term = (
        r"(?:(?:the|число|количество)\s+|number\s+of\s+)?(?:"
        + "|".join(terms.values())
        + r")(?:\s+counts?)?"
    )
    listing = term + r"(?:\s*(?:,\s*(?:(?:and|or|и|или)\s+)?|\s+(?:and|or|и|или)\s+)" + term + ")*"
    clauses = re.findall(
        r"\b(?:without|no|exclude|omit|ignore|без)\s+(" + listing + r")"
        r"|(" + listing + r")\s+не\s+нуж\w*",
        intent,
    )
    return {
        name
        for name, pattern in terms.items()
        if any(re.search(r"\b(?:" + pattern + r")\b", left or right) for left, right in clauses)
    }


def _fallback(tool_name: str, request: str, schema: dict[str, Any]) -> list[str]:
    intent = request.casefold()
    names = {field["name"] for field in schema["fields"]}
    preset = "digest" if _summary_intent(intent) else "compact"
    selection = resolve_fields(tool_name, preset=preset)
    fields = set(selection.fields or names)
    extras = {
        ("reaction", "реакц"): {"reactions"},
        ("comment", "reply", "комментар", "ответ"): {"reply_count"},
        ("engagement", "активност", "популяр"): {
            "reactions",
            "views",
            "reply_count",
        },
        ("author", "sender", "автор", "отправител", "подпис", "пересыл"): {
            "sender_name",
            "sender_username",
            "author_signature",
            "forwarded_from",
        },
        ("attachment", "media", "ocr", "transcri", "вложен", "голосов", "файл"): {"media"},
        ("edit", "редактир", "изменён", "изменен"): {"edited_at"},
    }
    for words, additions in extras.items():
        if any(word in intent for word in words):
            fields.update(additions & names)
    if re.search(r"\bviews?\b", intent) or "просмотр" in intent:
        fields.update({"views"} & names)
    fields.difference_update(_excluded_engagement(intent))
    fields.update(_essential_content(request, schema))
    return [field["name"] for field in schema["fields"] if field["name"] in fields]


def compact_selection(result: dict[str, Any]) -> dict[str, Any]:
    """Project a completed full decision into the opt-in compact selector shape.

    This runs after the full selection and cache lookup, so it never reclassifies
    content, changes the cache key, spends budget or calls an external service. It
    keeps the decision identity (tool/schema), effective fields, status, a short
    explanation, the applied content safeguards and a concrete fallback reason.
    """
    status = result["status"]
    return {
        "tool_name": result["tool_name"],
        "schema_id": result["schema_id"],
        "fields": result["fields"],
        "status": status,
        "explanation": result["explanation"],
        "safeguards": sorted(
            name for name, reason in result["reasons"].items() if "safeguard" in reason
        ),
        "fallback_reason": result["explanation"]
        if status in {"disabled", "unavailable", "budget_exceeded", "uncertain"}
        else None,
        "detail": "compact",
    }


class FieldSelector:
    def __init__(self, settings: Settings, store: Store, credentials: Secrets) -> None:
        self.settings, self.store, self.credentials = settings, store, credentials
        self.lock = asyncio.Lock()

    def _result(
        self,
        tool_name: str,
        schema: dict[str, Any],
        fields: list[str],
        status: str,
        explanation: str,
        reasons: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        required = [field["name"] for field in schema["fields"] if field["required"]]
        selected = set(fields) | set(required)
        all_fields = [field["name"] for field in schema["fields"]]
        return {
            "tool_name": tool_name,
            "schema_id": schema["schema_id"],
            "fields": [name for name in all_fields if name in selected],
            "omitted": [name for name in all_fields if name not in selected],
            "required": required,
            "status": status,
            "explanation": explanation,
            "reasons": {
                name: (
                    "Required source identity, date or structural marker."
                    if name in required
                    else (reasons or {}).get(
                        name,
                        "Retained by the deterministic intent preset."
                        if name in selected
                        else "Omitted by the deterministic intent preset.",
                    )
                )
                for name in all_fields
            },
        }

    async def select(
        self,
        tool_name: str,
        request: str,
        fields: list[str] | None = None,
        preset: str | None = None,
        use_jev: bool = False,
        detail: str = "full",
    ) -> dict[str, Any]:
        if detail not in DETAILS:
            raise TeleloomError("invalid_request", "detail must be 'full' or 'compact'.")
        result = await self._select_full(tool_name, request, fields, preset, use_jev)
        return compact_selection(result) if detail == "compact" else result

    async def _select_full(
        self,
        tool_name: str,
        request: str,
        fields: list[str] | None = None,
        preset: str | None = None,
        use_jev: bool = False,
    ) -> dict[str, Any]:
        if len(request) > 2000:
            raise TeleloomError(
                "invalid_request", "Field-selection tasks must be at most 2000 characters."
            )
        schema = catalog(tool_name)
        # Validate client choices before any optional external call. An explicit full
        # preset returns all fields rather than treating None as an inferred choice.
        if fields is not None or preset is not None:
            chosen = resolve_fields(tool_name, fields=fields, preset=preset)
            all_fields = [field["name"] for field in schema["fields"]]
            effective = list(chosen.fields) if chosen.fields is not None else all_fields
            return self._result(
                tool_name,
                schema,
                effective,
                "explicit",
                "Explicit client fields or preset take precedence; Jev was not called.",
                {
                    field["name"]: (
                        "Retained by explicit client selection."
                        if field["name"] in effective
                        else "Omitted by explicit client selection."
                    )
                    for field in schema["fields"]
                },
            )
        fallback = _fallback(tool_name, request, schema)
        essential = _essential_content(request, schema)

        def default(status: str, explanation: str) -> dict[str, Any]:
            return self._result(tool_name, schema, fallback, status, explanation)

        if not use_jev:
            return default("disabled", "Jev is disabled; the deterministic intent preset was used.")
        optional = [field for field in schema["fields"] if not field["required"]]
        if not optional:
            return default("disabled", "This schema has no removable fields; Jev was not called.")
        if len(optional) > 64:
            return default(
                "budget_exceeded", "The schema exceeds the 64-question selection budget."
            )
        state = {"request": request, "tool_name": tool_name, "available_fields": optional}
        questions: dict[str, SelectionQuestion] = {
            field["name"]: {
                "instructions": (
                    f"Retain `{field['name']}` for `request`? "
                    f"`available_fields[{index}].description`: {field['description']} "
                    "Judge static usefulness only; never invent values or permissions."
                ),
                "criteria": {
                    "true": (
                        "Needed for the task: reading/summaries need text/captions; links need URL entities."
                    ),
                    "false": (
                        "Irrelevant/excluded. Excluded engagement counts never exclude message content."
                    ),
                },
            }
            for index, field in enumerate(optional)
        }
        encoded = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
        if len(encoded) > self.settings.limits.jev_max_characters:
            return default(
                "budget_exceeded", "The schema and task exceed the owner character budget."
            )
        normalized = " ".join(request.casefold().split())
        cache_key = hashlib.sha256(
            json.dumps(
                [
                    tool_name,
                    schema["schema_id"],
                    normalized,
                    self.settings.jev_model,
                    POLICY_VERSION,
                ],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        timeout = min(5.0, self.settings.read_timeout_seconds)
        try:
            async with asyncio.timeout(timeout), self.lock:
                now = utcnow()
                cache = self.store.state(CACHE_STATE, {})
                cache = {
                    key: value
                    for key, value in cache.items()
                    if value["expires_at"] > now.timestamp()
                }
                with self.store.db:
                    self.store.set_state(CACHE_STATE, cache)
                cached = cache.get(cache_key)
                if cached:
                    result = cached["selection"].copy()
                    result["tool_name"] = tool_name
                    result["status"] = "cached"
                    result["explanation"] = (
                        "A schema, intent, model and policy-matched judgment is cached for 24 hours."
                    )
                    return result
                daily = "jev_daily:" + now.date().isoformat()
                if self.store.state(daily, 0) >= self.settings.limits.jev_daily_calls:
                    return default(
                        "budget_exceeded", "The shared owner daily Jev call budget is exhausted."
                    )
                key = os.getenv("TYPESAFE_API_KEY") or self.credentials.get("", "typesafe_api_key")
                if not key:
                    return default(
                        "unavailable",
                        "Jev credentials are unavailable; the deterministic preset was used.",
                    )
                from typesafe_sdk import AsyncTypeSafeClient, Noul, NoulCriteria, RetryPolicy

                typed = {
                    name: Noul(
                        instructions=question["instructions"],
                        criteria=NoulCriteria(
                            true=question["criteria"]["true"],
                            false=question["criteria"]["false"],
                        ),
                    )
                    for name, question in questions.items()
                }
                with self.store.db:
                    self.store.set_state(daily, self.store.state(daily, 0) + 1)
                async with AsyncTypeSafeClient(
                    api_key=key, timeout=timeout, retry=RetryPolicy(max_retries=0)
                ) as client:
                    response = await client.system_one(
                        state=state, questions=typed, model=self.settings.jev_model
                    )
                raw = response.model_dump(mode="json")["answers"]
                if not isinstance(raw, dict) or set(raw) != set(questions):
                    return default(
                        "unavailable",
                        "Jev returned incomplete or invalid field judgments; the deterministic preset was used.",
                    )
                selected = set(fallback)
                reasons = {}
                uncertain = False
                for name, answer in raw.items():
                    value = answer.get("noul") if isinstance(answer, dict) else None
                    if (
                        not isinstance(answer, dict)
                        or answer.get("type") != "noul"
                        or isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or not 0 <= value <= 1
                    ):
                        return default(
                            "unavailable",
                            "Jev returned incomplete or invalid field judgments; the deterministic preset was used.",
                        )
                    if value >= 0.7:
                        selected.add(name)
                        reasons[name] = "Retained: schema-only Jev judgment is at least 0.7."
                    elif value <= 0.3:
                        selected.discard(name)
                        reasons[name] = "Omitted: schema-only Jev judgment is at most 0.3."
                    else:
                        uncertain = True
                        reasons[name] = (
                            "Uncertain Jev judgment; retained by the deterministic preset."
                            if name in selected
                            else "Uncertain Jev judgment; omitted by the deterministic preset."
                        )
                overridden = essential - selected
                if overridden:
                    uncertain = True
                    selected.update(overridden)
                    for name in overridden:
                        reasons[name] = (
                            "Retained by the reading/summary content safeguard; "
                            "the optional Jev omission was overridden."
                        )
                result = self._result(
                    tool_name,
                    schema,
                    list(selected),
                    "uncertain" if uncertain else "evaluated",
                    "The content safeguard overrode a Jev omission; reading/summary content is retained, and coverage and permissions are unchanged."
                    if overridden
                    else "Uncertain field judgments use the deterministic preset; judgments do not change coverage or permissions."
                    if uncertain
                    else "Schema-only Jev judgments selected optional fields; coverage and permissions are unchanged.",
                    reasons,
                )
                cache[cache_key] = {
                    "expires_at": now.timestamp() + CACHE_SECONDS,
                    "selection": result,
                }
                while len(cache) > CACHE_ENTRIES:
                    oldest = min(cache, key=lambda name: cache[name]["expires_at"])
                    del cache[oldest]
                with self.store.db:
                    self.store.set_state(CACHE_STATE, cache)
                return result
        except TimeoutError:
            return default(
                "unavailable",
                "Jev exceeded the selection deadline; the deterministic preset was used.",
            )
        except ImportError:
            return default(
                "unavailable",
                "Install teleloom[jev] to use Jev; the deterministic preset was used.",
            )
        except Exception:
            return default(
                "unavailable",
                "Jev service or credential storage is unavailable; the deterministic preset was used.",
            )
