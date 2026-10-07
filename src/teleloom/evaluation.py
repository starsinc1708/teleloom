"""Manual schema-only evaluation through isolated, real HTTP MCP sessions.

No owner configuration is loaded. Only the external SDK and Telegram boundary
are substituted; runtime, SQLite, queues, selection policy and MCP remain real.
"""

import asyncio
import hashlib
import importlib
import json
import math
import os
import sys
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx
import typer
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from .adapters import Adapter
from .config import Limits, Profile, Settings
from .field_selection import POLICY_VERSION
from .models import Message, TeleloomError
from .runtime import AdapterFactory, Runtime
from .secrets import Secrets
from .server import create_server

CORPUS_VERSION = "response-fields-2026-10-05-v1"
FIXED_TIME = datetime(2026, 10, 5, 12, tzinfo=UTC)
REGRESSION_REQUEST = (
    "Сделай саммари всех сообщений за последние 48 часов. "
    "Выдели полезные заметки и технологии со ссылками. "
    "Реакции, просмотры и число комментариев не нужны."
)
REGRESSION_SCORES = {
    "sender_name": 0.18,
    "text": 0.29,
    "author_signature": 0.12,
    "entities": 0.4,
    "media": 0.21,
    "pinned": 0.09,
    "sender_username": 0.41,
    "views": 0.29,
    "edited_at": 0.19,
    "forwarded_from": 0.13,
    "outgoing": 0.19,
    "reactions": 0.06,
    "reply_count": 0.08,
}


@dataclass(frozen=True)
class Case:
    id: str
    language: str
    request: str
    necessary: tuple[str, ...]
    excluded: tuple[str, ...] = ()
    scores: dict[str, float] = field(default_factory=dict)
    fields: tuple[str, ...] | None = None
    preset: str | None = None
    replay_kind: str = "fake"
    invalid_response: bool = False


CORPUS = (
    Case(
        "ru-summary-regression",
        "ru",
        REGRESSION_REQUEST,
        ("text", "entities"),
        ("reactions", "views", "reply_count"),
        REGRESSION_SCORES,
        replay_kind="replay",
    ),
    Case(
        "en-summary-links",
        "en",
        "Summarize notes with links, without reactions or views.",
        ("text", "entities"),
        ("reactions", "views"),
        {"text": 0.2, "entities": 0.2},
    ),
    Case(
        "ru-no-engagement",
        "ru",
        "Читай сообщения без реакций и просмотров.",
        ("text",),
        ("reactions", "views"),
        {"text": 0.29},
    ),
    Case("en-history", "en", "Read the complete message history.", ("text",), scores={"text": 0.2}),
    Case("ru-history", "ru", "Прочитай историю сообщений.", ("text",), scores={"text": 0.2}),
    Case("en-only-counts", "en", "Only views", ("views",), ("text", "entities"), {"views": 0.9}),
    Case(
        "ru-only-counts",
        "ru",
        "Только счётчики",
        ("views", "reactions", "reply_count"),
        ("text", "entities"),
        {"views": 0.9, "reactions": 0.9, "reply_count": 0.9},
    ),
    Case(
        "en-mixed-negation",
        "en",
        "Read history, not just metadata; omit views and reactions.",
        ("text",),
        ("views", "reactions"),
        {"text": 0.29},
    ),
    Case(
        "ru-mixed-negation",
        "ru",
        "Нужен текст и ссылки, не только счётчики. Без просмотров.",
        ("text", "entities"),
        ("views",),
        {"text": 0.2, "entities": 0.2},
    ),
    Case(
        "en-explicit-fields",
        "en",
        "Summarize notes with links",
        ("views",),
        ("text", "entities"),
        fields=("views",),
    ),
    Case(
        "ru-explicit-minimal",
        "ru",
        "Саммари со ссылками",
        ("id", "chat_id", "profile_id", "date"),
        ("text", "entities"),
        preset="minimal",
    ),
    Case(
        "en-fallback-invalid",
        "en",
        "Summary with URLs, without reactions or views",
        ("text", "entities"),
        ("views", "reactions"),
        invalid_response=True,
    ),
)


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class _Credentials(Secrets):
    def __init__(self, key: str) -> None:
        self.key = key

    def get(self, profile: str, name: str) -> str | None:
        return self.key if not profile and name == "typesafe_api_key" else None


class _Question:
    def __init__(self, **kwargs: Any) -> None:
        self.values = kwargs

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return {
            name: value.model_dump() if isinstance(value, _Question) else value
            for name, value in self.values.items()
        }


@dataclass
class _External:
    live: bool
    max_requests: int
    max_characters: int
    case: Case = CORPUS[0]
    calls: list[dict[str, Any]] = field(default_factory=list)
    characters: int = 0
    stopped: str | None = None

    def sdk(self) -> ModuleType:
        original = importlib.import_module("typesafe_sdk") if self.live else None
        real_client = original.AsyncTypeSafeClient if original else None
        external = self

        class Client:
            def __init__(self, **kwargs: Any) -> None:
                self.delegate = real_client(**kwargs) if real_client else None

            async def __aenter__(self) -> "Client":
                if self.delegate:
                    await self.delegate.__aenter__()
                return self

            async def __aexit__(self, *args: Any) -> None:
                if self.delegate:
                    await self.delegate.__aexit__(*args)

            async def system_one(
                self, *, state: dict[str, Any], questions: dict[str, Any], model: str
            ) -> Any:
                request = {
                    "state": state,
                    "questions": {
                        name: question.model_dump(mode="json")
                        for name, question in questions.items()
                    },
                    "model": model,
                }
                characters = len(json.dumps(request, ensure_ascii=False))
                if len(external.calls) >= external.max_requests:
                    external.stopped = "request_budget_exceeded"
                    raise TeleloomError("evaluation_budget", "External request budget exhausted.")
                if external.characters + characters > external.max_characters:
                    external.stopped = "character_budget_exceeded"
                    raise TeleloomError(
                        "evaluation_budget", "Static request character budget exhausted."
                    )
                external.characters += characters
                record: dict[str, Any] = {
                    "request_bytes": _json_bytes(request),
                    "response_bytes": 0,
                    "raw_scores": {},
                    "usage": None,
                    "model": model,
                    "mode": "live" if external.live else external.case.replay_kind,
                    "latency_ms": None,
                }
                external.calls.append(record)
                started = perf_counter()
                try:
                    if self.delegate:
                        response = await self.delegate.system_one(
                            state=state, questions=questions, model=model
                        )
                    else:
                        answers = {
                            name: {"type": "noul", "noul": external.case.scores.get(name, 0.1)}
                            for name in questions
                        }
                        payload = {
                            "answers": {} if external.case.invalid_response else answers,
                            "model": "jev-1.13.0"
                            if external.case.replay_kind == "replay"
                            else "fake-jev-corpus-v1",
                        }
                        response = SimpleNamespace(model_dump=lambda **kwargs: payload)
                    raw = response.model_dump(mode="json")
                    record["response_bytes"] = _json_bytes(raw)
                    record["model"] = raw.get("model", model)
                    record["raw_scores"] = {
                        name: answer.get("noul")
                        for name, answer in raw.get("answers", {}).items()
                        if name in questions
                        and isinstance(answer, dict)
                        and isinstance(answer.get("noul"), (int, float))
                        and not isinstance(answer["noul"], bool)
                        and math.isfinite(answer["noul"])
                    }
                    usage = raw.get("usage")
                    if isinstance(usage, dict):
                        record["usage"] = {
                            key: value
                            for key, value in usage.items()
                            if key in {"input_tokens", "output_tokens"}
                            and (
                                value is None
                                or isinstance(value, int)
                                and not isinstance(value, bool)
                            )
                        }
                    return response
                finally:
                    record["latency_ms"] = (perf_counter() - started) * 1000

        sdk = ModuleType("typesafe_sdk")
        sdk.AsyncTypeSafeClient = Client  # type: ignore[attr-defined]
        for name in ("Noul", "NoulCriteria", "RetryPolicy"):
            setattr(sdk, name, getattr(original, name) if original else _Question)
        return sdk


def _live_key(live: bool) -> str:
    if not live:
        return "isolated-replay-key"
    key = os.getenv("TYPESAFE_API_KEY")
    if not key:
        raise TeleloomError(
            "credentials_missing", "Live evaluation requires existing Jev credentials."
        )
    try:
        importlib.import_module("typesafe_sdk")
    except ImportError:
        raise TeleloomError(
            "engine_missing", "Install teleloom[jev] for explicit live evaluation."
        ) from None
    return key


@asynccontextmanager
async def _session(
    settings: Settings,
    external: _External,
    key: str,
    adapter_factory: AdapterFactory | None = None,
) -> AsyncIterator[ClientSession]:
    # Substitution is process-local and always restored; no environment, owner
    # profile, credential store or persistent config is modified.
    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {"typesafe_sdk": external.sdk()}))
        for module in ("field_selection", "runtime", "jobs"):
            stack.enter_context(patch(f"teleloom.{module}.utcnow", return_value=FIXED_TIME))
        runtime = Runtime(settings, adapter_factory) if adapter_factory else Runtime(settings)
        runtime.credentials = _Credentials(key)
        server = create_server(settings, runtime=runtime)
        application = server.streamable_http_app()
        try:
            await runtime.start()
            async with (
                server.session_manager.run(),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application),
                    trust_env=False,
                    timeout=10,
                ) as http,
                streamable_http_client(settings.url, http_client=http) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield session
        finally:
            await runtime.close()


async def _call(
    session: ClientSession, tool: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = perf_counter()
    result: CallToolResult = await session.call_tool(tool, arguments)
    latency = (perf_counter() - started) * 1000
    structured = result.structuredContent or {}
    texts = [item.text for item in result.content if item.type == "text"]
    agreement = len(texts) == 1 and json.loads(texts[0]) == structured
    if not structured.get("ok") or result.isError:
        raise TeleloomError("evaluation_tool_failed", "The isolated MCP tool failed.")
    metrics = {
        "tool": tool,
        "request_bytes": _json_bytes(
            {"method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        ),
        "response_bytes": _json_bytes(result.model_dump(mode="json", exclude_none=True)),
        "structured_content_bytes": _json_bytes(structured),
        "text_json_bytes": sum(len(text.encode("utf-8")) for text in texts),
        "representations_agree": agreement,
        "latency_ms": latency,
    }
    return structured["data"], metrics


async def _tool_schemas(session: ClientSession, names: tuple[str, ...]) -> dict[str, int]:
    """Measure the discovery inputSchema bytes for the named public tools."""
    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    measured = {name: _json_bytes(tools[name].inputSchema) for name in names if name in tools}
    measured["combined_bytes"] = sum(measured.values())
    return measured


def _row_schema_bytes(schemas: dict[str, int], tools: set[str]) -> int:
    return sum(schemas.get(name, 0) for name in tools)


async def _compact_selection_measurement(
    session: ClientSession, schemas: dict[str, int], task: str
) -> dict[str, Any]:
    """Compare the full and opt-in compact selector payloads on one disabled fixture."""
    full, full_metrics = await _call(
        session,
        "response_fields_select",
        {"tool_name": "messages_get", "request": task, "detail": "full", "use_jev": False},
    )
    compact, compact_metrics = await _call(
        session,
        "response_fields_select",
        {"tool_name": "messages_get", "request": task, "detail": "compact", "use_jev": False},
    )
    full_total = full_metrics["request_bytes"] + full_metrics["response_bytes"]
    compact_total = compact_metrics["request_bytes"] + compact_metrics["response_bytes"]
    reduction = round(100 * (full_total - compact_total) / full_total, 2)
    violations: list[str] = []
    if compact_total * 2 > full_total:
        violations.append("compact_under_50_percent")
    if compact["fields"] != full["fields"] or compact["schema_id"] != full["schema_id"]:
        violations.append("decision_mismatch")
    if compact["status"] != full["status"]:
        violations.append("status_mismatch")
    if "reasons" in compact:
        violations.append("reason_map_retained")

    def shape(metrics: dict[str, Any], name: str, status: str) -> dict[str, Any]:
        return {
            "detail": name,
            "status": status,
            "total_bytes": metrics["request_bytes"] + metrics["response_bytes"],
            "request_bytes": metrics["request_bytes"],
            "response_bytes": metrics["response_bytes"],
            "structured_content_bytes": metrics["structured_content_bytes"],
            "text_json_bytes": metrics["text_json_bytes"],
        }

    return {
        "task": task,
        "schema_bytes": schemas["response_fields_select"],
        "round_trips": 2,
        "full": shape(full_metrics, "full", full["status"]),
        "compact": shape(compact_metrics, "compact", compact["status"]),
        "reduction_percent": reduction,
        "same_effective_fields": compact["fields"] == full["fields"],
        "violations": violations,
    }


async def _select_once_workflow(
    session: ClientSession, schemas: dict[str, int], *, pages: int = 10, limit: int = 10
) -> dict[str, Any]:
    """Check that one selector call serves a 10-page read and preserves coverage."""
    task = "Summarize these fictional notes with URLs, without views and reactions."
    selection, select_metrics = await _call(
        session,
        "response_fields_select",
        {"tool_name": "messages_get", "request": task, "detail": "compact"},
    )
    fields = selection["fields"]
    projected_pages: list[dict[str, Any]] = []
    page_calls: list[dict[str, Any]] = []
    cursor: str | None = None
    identity_kept = True
    projection_clean = True
    for _ in range(pages):
        arguments: dict[str, Any] = {
            "profile_id": "fictional",
            "chat_id": "100",
            "limit": limit,
            "fields": fields,
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        page, metrics = await _call(session, "messages_get", arguments)
        projected_pages.append(page)
        page_calls.append(metrics)
        for item in page["items"]:
            if not {"profile_id", "chat_id", "id", "date", "link"} <= item.keys():
                identity_kept = False
            if {"reactions", "views", "reply_count"} & item.keys():
                projection_clean = False
        if set(page["projection"]["fields"]) != set(fields):
            projection_clean = False
        cursor = page.get("next_cursor")
        if cursor is None:
            break

    baseline_cursor: str | None = None
    coverage_match = True
    record_match = True
    baseline_calls: list[dict[str, Any]] = []
    for page in projected_pages:
        arguments = {"profile_id": "fictional", "chat_id": "100", "limit": limit}
        if baseline_cursor is not None:
            arguments["cursor"] = baseline_cursor
        baseline, metrics = await _call(session, "messages_get", arguments)
        baseline_calls.append(metrics)
        if (
            baseline["coverage"],
            baseline["incomplete"],
            baseline["warnings"],
            baseline["next_cursor"],
            baseline["source"],
        ) != (
            page["coverage"],
            page["incomplete"],
            page["warnings"],
            page["next_cursor"],
            page["source"],
        ):
            coverage_match = False
        if [item["id"] for item in baseline["items"]] != [item["id"] for item in page["items"]]:
            record_match = False
        baseline_cursor = baseline.get("next_cursor")

    violations: list[str] = []
    if selection["status"] != "disabled":
        violations.append("unexpected_status")
    if len(page_calls) != pages:
        violations.append("page_count")
    if not identity_kept:
        violations.append("identity_lost")
    if not projection_clean:
        violations.append("projection_not_applied")
    if not coverage_match:
        violations.append("coverage_mismatch")
    if not record_match:
        violations.append("record_mismatch")
    calls = [select_metrics, *page_calls]
    return {
        "task": task,
        "selector_calls": 1,
        "external_calls": 0,
        "pages_read": len(page_calls),
        "returned_records": sum(len(page["items"]) for page in projected_pages),
        "fields": fields,
        "round_trips": len(calls),
        "selection_overhead_bytes": select_metrics["request_bytes"]
        + select_metrics["response_bytes"],
        "schema_bytes": _row_schema_bytes(schemas, {call["tool"] for call in calls}),
        "structured_content_bytes": sum(call["structured_content_bytes"] for call in calls),
        "text_json_bytes": sum(call["text_json_bytes"] for call in calls),
        "coverage_baseline_calls": len(baseline_calls),
        "mcp_calls": calls,
        "violations": violations,
    }


async def run_evaluator(
    *,
    live: bool = False,
    max_requests: int = 16,
    max_characters: int = 200000,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    key = _live_key(live)
    external = _External(live, max_requests, max_characters)
    cases: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "report_version": 1,
        "corpus_version": CORPUS_VERSION,
        "mode": "live" if live else "replay",
        "measurement": "Real local HTTP MCP; substituted external API in replay mode.",
        "cases": cases,
        "ok": True,
    }
    try:
        async with asyncio.timeout(timeout_seconds):
            with TemporaryDirectory(prefix="teleloom-evaluate-") as temporary:
                settings = Settings(
                    data_dir=Path(temporary), limits=Limits(jev_daily_calls=max_requests)
                )
                async with _session(settings, external, key) as session:
                    schemas = await _tool_schemas(
                        session, ("response_fields_select", "messages_get")
                    )
                    report["schemas"] = schemas
                    for case in CORPUS:
                        external.case = case
                        before = len(external.calls)
                        if before >= max_requests and case.fields is None and case.preset is None:
                            external.stopped = "request_budget_exceeded"
                            break
                        arguments: dict[str, Any] = {
                            "tool_name": "messages_get",
                            "request": case.request,
                            "use_jev": True,
                        }
                        if case.fields is not None:
                            arguments["fields"] = list(case.fields)
                        if case.preset is not None:
                            arguments["preset"] = case.preset
                        selection, metrics = await _call(
                            session, "response_fields_select", arguments
                        )
                        actual = set(selection["fields"])
                        captured = external.calls[before:]
                        observation = captured[-1] if captured else {}
                        scores = observation.get("raw_scores", {})
                        violations = [
                            "missing:" + name for name in case.necessary if name not in actual
                        ]
                        violations += [
                            "excluded:" + name for name in case.excluded if name in actual
                        ]
                        if not metrics["representations_agree"]:
                            violations.append("representation_mismatch")
                        cases.append(
                            {
                                "id": case.id,
                                "language": case.language,
                                "request": case.request,
                                "mode": observation.get(
                                    "mode", "live" if live else case.replay_kind
                                ),
                                "model": observation.get("model"),
                                "model_source": "not_called_explicit_selection"
                                if selection["status"] == "explicit"
                                else "not_called"
                                if not captured
                                else "live API response"
                                if live
                                else "historical docs/acceptance.md"
                                if case.replay_kind == "replay"
                                else "synthetic corpus fixture",
                                "schema_id": selection["schema_id"],
                                "policy_version": POLICY_VERSION,
                                "expected_necessary": list(case.necessary),
                                "expected_excluded": list(case.excluded),
                                "raw_scores": scores,
                                "raw_score_sources": {
                                    name: "live API response"
                                    if live
                                    else "recorded tests/test_field_selection.py"
                                    if case.replay_kind == "replay"
                                    else "synthetic corpus fixture"
                                    for name in scores
                                },
                                "recorded_measurements": {
                                    "usage": {"input_tokens": 1520, "output_tokens": 226},
                                    "latency_ms": 1276,
                                    "source": "docs/acceptance.md original 2026-10-05 run",
                                }
                                if case.replay_kind == "replay"
                                else None,
                                "raw_missing": [
                                    name
                                    for name in case.necessary
                                    if name in scores and scores[name] <= 0.3
                                ],
                                "raw_excluded": [
                                    name
                                    for name in case.excluded
                                    if name in scores and scores[name] >= 0.7
                                ],
                                "raw_uncertain": [
                                    name for name, score in scores.items() if 0.3 < score < 0.7
                                ],
                                "final_fields": selection["fields"],
                                "status": selection["status"],
                                "safeguards": [
                                    name
                                    for name, reason in selection["reasons"].items()
                                    if "safeguard" in reason
                                ],
                                "fallback": selection["status"]
                                in {"unavailable", "budget_exceeded", "disabled"}
                                or any(
                                    "Uncertain Jev judgment" in reason
                                    for reason in selection["reasons"].values()
                                ),
                                "fallback_fields": [
                                    name
                                    for name, reason in selection["reasons"].items()
                                    if "Uncertain Jev judgment" in reason
                                ],
                                "violations": violations,
                                "usage": observation.get("usage"),
                                "latency_ms": metrics["latency_ms"],
                                "external_latency_ms": observation.get("latency_ms"),
                                "external_calls": len(captured),
                                "external_metrics": captured,
                                "round_trips": 1,
                                "schema_bytes": schemas["response_fields_select"]
                                + schemas["messages_get"],
                                "selection_overhead_bytes": metrics["request_bytes"]
                                + metrics["response_bytes"],
                                "structured_content_bytes": metrics["structured_content_bytes"],
                                "text_json_bytes": metrics["text_json_bytes"],
                                "mcp": metrics,
                            }
                        )
                        if external.stopped:
                            break
    except TimeoutError:
        report["stopped"] = "time_budget_exceeded"
    report["external_calls"] = len(external.calls)
    report["static_request_characters"] = external.characters
    if external.stopped:
        report["stopped"] = external.stopped
    report["ok"] = (
        not report.get("stopped")
        and len(cases) == len(CORPUS)
        and all(not case["violations"] for case in cases)
    )
    return report


class _FictionalTelegram:
    """Fictional external read service, never a real Telegram client."""

    def __init__(self) -> None:
        self.calls = 0
        self.rows = [
            Message(
                profile_id="fictional",
                chat_id="100",
                id=str(index),
                date=FIXED_TIME,
                text=f"Fictional engineering note {index}: compare designs, keep evidence and read the linked documentation.",
                link=f"https://t.me/fictional_example/{index}",
                entities=[
                    {
                        "type": "text_url",
                        "offset": 0,
                        "length": 9,
                        "url": f"https://example.invalid/notes/{index}",
                    }
                ],
                sender_name="Fictional author",
                sender_username="fictional_author",
                author_signature="Fictional signed author",
                media={
                    "type": "document",
                    "filename": "fictional-notes.txt",
                    "description": "Fictional attachment metadata. " * 8,
                },
                forwarded_from={"title": "Fictional source channel", "id": "200"},
                views=1000 + index,
                reactions=[
                    {"emoji": "star", "count": index},
                    {"emoji": "like", "count": 2 * index},
                ],
                reply_count=index,
                pinned=False,
            )
            for index in range(1, 102)
        ]

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def history(
        self,
        chat: str,
        *,
        before: int | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        query: str | None = None,
        ids: list[int] | None = None,
    ) -> list[Message]:
        self.calls += 1
        rows = [
            row
            for row in self.rows
            if row.chat_id == chat
            and (before is None or int(row.id) < before)
            and (since is None or row.date >= since)
            and (until is None or row.date < until)
            and (not query or query.casefold() in row.text.casefold())
            and (not ids or int(row.id) in ids)
        ]
        return sorted(rows, key=lambda row: int(row.id), reverse=True)[:limit]


def _integrity(original: dict[str, Any], reduced: dict[str, Any]) -> dict[str, bool]:
    before, after = original["items"], reduced["items"]
    identities = (
        "profile_id",
        "chat_id",
        "id",
        "date",
        "link",
        "kind",
        "deleted",
        "sender_id",
        "reply_to_message_id",
        "topic_id",
        "thread_root_id",
        "grouped_id",
    )
    equal_length = len(before) == len(after)
    return {
        "text": equal_length
        and all(
            left.get("text") == right.get("text")
            for left, right in zip(before, after, strict=False)
        ),
        "entities": equal_length
        and all(
            left.get("entities") == right.get("entities")
            for left, right in zip(before, after, strict=False)
        ),
        "identities": equal_length
        and all(
            all(left[name] == right.get(name) for name in identities)
            for left, right in zip(before, after, strict=False)
        ),
        "cursor": original["next_cursor"] == reduced["next_cursor"]
        and original["next_cursor"] is not None,
        "coverage": original["coverage"] == reduced["coverage"],
        "envelope": all(
            original[name] == reduced[name] for name in ("source", "incomplete", "warnings")
        ),
    }


async def run_benchmark(
    *,
    live: bool = False,
    max_requests: int = 6,
    max_characters: int = 200000,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    key = _live_key(live)
    external = _External(live, max_requests, max_characters)
    telegram = _FictionalTelegram()
    rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "report_version": 1,
        "corpus_version": CORPUS_VERSION,
        "mode": "live" if live else "replay",
        "telegram_mode": "fake",
        "sizes": [1, 20, 100],
        "rows": rows,
        "measurement": "Measured local HTTP MCP latency and compact UTF-8 application JSON bytes; external service is simulated unless --live.",
        "limitations": "JSON bytes are not tokens. No tokenizer is used. Client context savings depend on how each client consumes structuredContent and text JSON. Counts exclude HTTP headers, JSON-RPC IDs and SDK transport framing. schema_bytes is the discovery inputSchema size of the tools a row calls; selection_overhead_bytes is the selector round trip; round_trips counts MCP application calls. compact_selection compares one disabled full/compact selector payload; select_once reuses one selector decision across a 10-page read and checks coverage parity. Cached rows are warm-cache measurements; cold-start totals add the separately reported selection warmup. Direct digest intentionally omits URL entities; inferred selection retains them.",
        "ok": True,
    }

    def factory(*args: Any) -> Adapter:
        return cast(Adapter, telegram)

    try:
        async with asyncio.timeout(timeout_seconds):
            with TemporaryDirectory(prefix="teleloom-benchmark-") as temporary:
                settings = Settings(
                    data_dir=Path(temporary),
                    profiles={"fictional": Profile(kind="user", generation="fictional-v1")},
                    limits=Limits(jev_daily_calls=max_requests),
                )
                async with _session(settings, external, key, factory) as session:
                    schemas = await _tool_schemas(
                        session, ("response_fields_select", "messages_get")
                    )
                    report["schemas"] = schemas
                    for size in report["sizes"]:
                        baseline: dict[str, Any] | None = None
                        full_bytes = 0
                        warmup: dict[str, Any] = {}
                        task = f"Summarize this fictional {size}-message page with URLs, without views and reactions."
                        external.case = Case(
                            f"benchmark-{size}",
                            "en",
                            task,
                            ("text", "entities"),
                            ("views", "reactions"),
                            {"text": 0.29, "entities": 0.2},
                        )
                        for strategy in ("full", "preset", "jev_uncached", "jev_cached"):
                            if strategy == "jev_uncached" and len(external.calls) >= max_requests:
                                external.stopped = "request_budget_exceeded"
                                break
                            calls: list[dict[str, Any]] = []
                            first_external, first_telegram = len(external.calls), telegram.calls
                            started = perf_counter()
                            read_arguments: dict[str, Any] = {
                                "profile_id": "fictional",
                                "chat_id": "100",
                                "limit": size,
                            }
                            selection_status = None
                            if strategy == "preset":
                                read_arguments["preset"] = "digest"
                            elif strategy in {"jev_uncached", "jev_cached"}:
                                selection, metrics = await _call(
                                    session,
                                    "response_fields_select",
                                    {"tool_name": "messages_get", "request": task, "use_jev": True},
                                )
                                calls.append(metrics)
                                selection_status = selection["status"]
                                read_arguments["fields"] = selection["fields"]
                            page, metrics = await _call(session, "messages_get", read_arguments)
                            calls.append(metrics)
                            mcp_bytes = sum(
                                call["request_bytes"] + call["response_bytes"] for call in calls
                            )
                            captures = external.calls[first_external:]
                            external_bytes = sum(
                                call["request_bytes"] + call["response_bytes"] for call in captures
                            )
                            total = mcp_bytes + external_bytes
                            if baseline is None:
                                baseline, full_bytes = page, total
                            expected = {
                                name: True
                                for name in (
                                    "text",
                                    "entities",
                                    "identities",
                                    "cursor",
                                    "coverage",
                                    "envelope",
                                )
                            }
                            if strategy == "preset":
                                expected["entities"] = False
                            integrity = _integrity(baseline, page)
                            violations = [
                                "integrity:" + name
                                for name in expected
                                if integrity[name] != expected[name]
                            ]
                            if any(not call["representations_agree"] for call in calls):
                                violations.append("representation_mismatch")
                            if strategy == "jev_uncached" and not captures:
                                violations.append("uncached_selection_not_evaluated")
                            if strategy == "jev_cached" and selection_status != "cached":
                                violations.append("cache_not_used")
                            usage_records = [
                                call["usage"] for call in captures if call["usage"] is not None
                            ]
                            usage = (
                                {
                                    name: sum(
                                        item[name]
                                        for item in usage_records
                                        if item.get(name) is not None
                                    )
                                    if any(item.get(name) is not None for item in usage_records)
                                    else None
                                    for name in ("input_tokens", "output_tokens")
                                }
                                if usage_records
                                else None
                            )
                            row = {
                                "messages": size,
                                "strategy": strategy,
                                "mode": "live" if live else "fake",
                                "mcp_calls": calls,
                                "external_calls": {
                                    "jev": len(captures),
                                    "telegram_fake": telegram.calls - first_telegram,
                                },
                                "external_metrics": captures,
                                "external_json_bytes": external_bytes,
                                "mcp_json_bytes": mcp_bytes,
                                "round_trips": len(calls),
                                "schema_bytes": _row_schema_bytes(
                                    schemas, {call["tool"] for call in calls}
                                ),
                                "selection_overhead_bytes": (
                                    calls[0]["request_bytes"] + calls[0]["response_bytes"]
                                    if len(calls) > 1
                                    else 0
                                ),
                                "structured_content_bytes": sum(
                                    call["structured_content_bytes"] for call in calls
                                ),
                                "text_json_bytes": sum(call["text_json_bytes"] for call in calls),
                                "total_json_bytes": total,
                                "savings_bytes": full_bytes - total,
                                "savings_percent": round(
                                    100 * (full_bytes - total) / full_bytes, 2
                                ),
                                "latency_ms": (perf_counter() - started) * 1000,
                                "usage": usage,
                                "selection_status": selection_status,
                                "expected_integrity": expected,
                                "integrity": integrity,
                                "returned": len(page["items"]),
                                "next_cursor_present": bool(page["next_cursor"]),
                                "coverage": page["coverage"],
                                "violations": violations,
                                "evidence_sha256": hashlib.sha256(
                                    json.dumps(
                                        [
                                            [
                                                item["profile_id"],
                                                item["chat_id"],
                                                item["id"],
                                                item["text"],
                                                item.get("entities"),
                                            ]
                                            for item in page["items"]
                                        ],
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ).encode()
                                ).hexdigest(),
                                "limitation": "Authoritative digest preset omits URL entities; use inferred fields for summaries requiring hidden URLs."
                                if strategy == "preset"
                                else None,
                            }
                            if strategy == "jev_uncached":
                                warmup = {
                                    "mcp": calls[0],
                                    "external_calls": len(captures),
                                    "external_json_bytes": external_bytes,
                                    "total_json_bytes": calls[0]["request_bytes"]
                                    + calls[0]["response_bytes"]
                                    + external_bytes,
                                    "usage": usage,
                                }
                            elif strategy == "jev_cached":
                                row["cache_warmup"] = warmup
                                row["cold_start_total_json_bytes"] = (
                                    total + warmup["total_json_bytes"]
                                )
                                row["cold_start_savings_bytes"] = (
                                    full_bytes - row["cold_start_total_json_bytes"]
                                )
                            rows.append(row)
                            if external.stopped:
                                break
                        if external.stopped:
                            break
                    report["select_once"] = await _select_once_workflow(session, schemas)
                    report["compact_selection"] = await _compact_selection_measurement(
                        session,
                        schemas,
                        "Summarize these fictional notes with URLs, without views and reactions.",
                    )
    except TimeoutError:
        report["stopped"] = "time_budget_exceeded"
    if external.stopped:
        report["stopped"] = external.stopped
    report["external_calls"] = {"jev": len(external.calls), "telegram_fake": telegram.calls}
    report["static_request_characters"] = external.characters
    report["ok"] = (
        not report.get("stopped")
        and len(rows) == 12
        and all(not row["violations"] for row in rows)
        and not report.get("select_once", {}).get("violations", ["not_run"])
        and not report.get("compact_selection", {}).get("violations", ["not_run"])
    )
    return report


def evaluate_command(
    live: bool = False,
    max_requests: int = typer.Option(16, min=1, max=32),
    max_characters: int = typer.Option(200000, min=1, max=200000),
    timeout_seconds: float = typer.Option(30, min=0.05, max=60),
) -> None:
    """Evaluate the static RU/EN corpus; --live explicitly permits bounded Jev calls."""
    report = asyncio.run(
        run_evaluator(
            live=live,
            max_requests=max_requests,
            max_characters=max_characters,
            timeout_seconds=timeout_seconds,
        )
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise typer.Exit(1)


def benchmark_command(
    live: bool = False,
    max_requests: int = typer.Option(6, min=1, max=32),
    max_characters: int = typer.Option(200000, min=1, max=200000),
    timeout_seconds: float = typer.Option(30, min=0.05, max=60),
) -> None:
    """Measure selection plus fictional 1/20/100-message read workflows over MCP."""
    report = asyncio.run(
        run_benchmark(
            live=live,
            max_requests=max_requests,
            max_characters=max_characters,
            timeout_seconds=timeout_seconds,
        )
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise typer.Exit(1)
