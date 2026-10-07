"""Opt-in presentation limits; originals and coverage are never shortened here."""

import json
from collections.abc import Callable
from typing import Any

from .models import TeleloomError

CONTENT_FIELDS = {
    "text",
    "original_text",
    "rich_text",
    "transcript",
    "reply_quote",
    "web_preview",
    "buttons",
}
SPAN_NOTICE = "Entities omitted from the excerpt; original spans use UTF-16, not UTF-8 bytes."


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def measured(data: dict[str, Any], budget: int) -> dict[str, Any]:
    result = {**data, "output_budget": {"max_output_bytes": budget, "normalized_data_bytes": 0}}
    meta = result["output_budget"]
    while (size := len(compact_json(result).encode("utf-8"))) != meta["normalized_data_bytes"]:
        meta["normalized_data_bytes"] = size
    return result


def fit(build: Callable[[int], dict[str, Any]], maximum: int, budget: int) -> dict[str, Any]:
    """Find a fitting prefix, including the byte counter's own serialized digits."""
    result = measured(build(maximum), budget)
    if result["output_budget"]["normalized_data_bytes"] <= budget:
        return result
    result = measured(build(0), budget)
    minimum = result["output_budget"]["normalized_data_bytes"]
    if minimum > budget:
        raise TeleloomError(
            "output_budget_too_small",
            "The mandatory metadata and excerpt envelope exceed max_output_bytes; increase it or request fewer records/fields.",
            details={"max_output_bytes": budget, "minimum_bytes": minimum},
        )
    low, high = 0, maximum
    while low + 1 < high:
        middle = (low + high) // 2
        candidate = measured(build(middle), budget)
        if candidate["output_budget"]["normalized_data_bytes"] <= budget:
            low, result = middle, candidate
        else:
            high = middle
    return result


def evidence_rows(value: Any) -> list[dict[str, Any]]:
    """Message/selected-file records, including nested inbox and reply evidence."""
    if isinstance(value, list):
        return [row for child in value for row in evidence_rows(child)]
    if not isinstance(value, dict):
        return []
    if {"profile_id", "chat_id", "text"} <= value.keys() and (
        "id" in value or "message_id" in value
    ):
        return [value]
    return [row for child in value.values() for row in evidence_rows(child)]


def bounded_content(data: dict[str, Any], budget: int) -> dict[str, Any]:
    unchanged = measured(data, budget)
    if unchanged["output_budget"]["normalized_data_bytes"] <= budget:
        return unchanged

    # ponytail: a shared prefix cap; add field weighting only if callers need it.
    def shorten(value: Any, cap: int) -> Any:
        if isinstance(value, str):
            return value[:cap]
        if isinstance(value, list):
            return [shorten(child, cap) for child in value[:cap]]
        if isinstance(value, dict):
            # Provenance, warnings and format tags remain verbatim in derived content.
            return {
                key: child
                if key
                in {
                    "_",
                    "type",
                    "id",
                    "url",
                    "link",
                    "date",
                    "published_date",
                    "provider",
                    "model",
                    "source_version",
                    "warnings",
                }
                or key.endswith("_id")
                else shorten(child, cap)
                for key, child in value.items()
            }
        return value

    def build(cap: int) -> dict[str, Any]:
        def omit_spans(value: Any) -> bool:
            omitted = False
            if isinstance(value, list):
                for child in value:
                    omitted = omit_spans(child) or omitted
            elif isinstance(value, dict):
                if "entities" in value:
                    value["entities"] = []
                    omitted = True
                for key in ("offset", "length"):
                    if key in value:
                        del value[key]
                        omitted = True
                for child in value.values():
                    omitted = omit_spans(child) or omitted
            return omitted

        result = json.loads(compact_json(data))
        for row in evidence_rows(result):
            changed = []
            for field in sorted(CONTENT_FIELDS & row.keys()):
                original = row[field]
                excerpt = shorten(original, cap)
                if excerpt != original:
                    row[field] = excerpt
                    changed.append(field)
            if changed:
                row["excerpt"] = {
                    "truncated": True,
                    "fields": changed,
                    "evidence_ref": data["evidence_ref"],
                    "source_version": data["source_version"],
                }
                # No shortened text is ever paired with original UTF-16 ranges.
                if "text" in changed or "original_text" in changed:
                    if "entities" in row:
                        row["entities"] = []
                    row["excerpt"]["entities_notice"] = SPAN_NOTICE
                for field in changed:
                    if omit_spans(row[field]):
                        row["excerpt"]["entities_notice"] = SPAN_NOTICE
        return result

    return fit(build, len(compact_json(data)), budget)
