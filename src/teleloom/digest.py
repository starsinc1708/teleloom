"""Caller-owned historical grounding and explicit bounded source revalidation."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from mcp import ClientSession
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .file_snapshots import FILE_LIMIT
from .models import EvidenceKey, FrozenEvidenceSource, Message, TeleloomError, iso, utcnow
from .output_budget import compact_json
from .runtime import number

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Key(EvidenceKey):
    model_config = ConfigDict(extra="forbid", strict=True)


class Citation(Key):
    profile_id: Identifier
    source_version: SHA256
    rendering: Literal["verbatim", "paraphrase", "reconstruction", "excerpt"]
    text: str = Field(min_length=1)


class Claim(Record):
    id: Identifier
    text: str = Field(min_length=1)
    kind: Literal["fact", "decision", "question", "task"]
    uncertainty: str = Field(min_length=1)
    supporting: list[Citation] = Field(min_length=1)
    contradicting: list[Citation]
    retracts: list[Identifier]


class Chunk(Record):
    id: Identifier
    message_keys: list[Key]
    claim_ids: list[Identifier]


class Reduction(Record):
    id: Identifier
    inputs: list[Identifier] = Field(min_length=1)
    claim_ids: list[Identifier]


class Budgets(Record):
    max_chunk_messages: int = Field(ge=1, le=1000)
    max_chunk_bytes: int = Field(ge=1, le=2_000_000)
    reduce_fan_in: int = Field(ge=2, le=16)
    max_reduce_claims: int = Field(ge=1, le=1000)
    max_reduce_bytes: int = Field(ge=1, le=2_000_000)


class ModelRun(Record):
    provider: Identifier
    model: Identifier
    prompt_sha256: SHA256
    upload_policy: str = Field(min_length=1)
    budget: str = Field(min_length=1)
    attribution: str = Field(min_length=1)


class Revision(Record):
    schema_name: Literal["teleloom-digest-revision-v1"] = Field(alias="schema")
    revision_id: Identifier
    mode: Literal["historical_as_of"]
    source_manifest: dict[str, Any]
    export_sha256: SHA256
    budgets: Budgets
    model_run: ModelRun | None
    claims: list[Claim] = Field(max_length=1000)
    chunks: list[Chunk] = Field(min_length=1, max_length=10000)
    reductions: list[Reduction] = Field(max_length=10000)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TeleloomError("invalid_digest", message)


def unique(values: list[Any]) -> bool:
    return len(set(values)) == len(values)


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    require(len(content) <= limit, "Local input exceeds the supported byte limit.")
    return content


def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    require(unique([key for key, _ in pairs]), "JSON contains duplicate object keys.")
    return dict(pairs)


def _ground_revision(
    manifest: Path, export: Path, sha256: str
) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    """Ground one historical revision against an independently pinned JSONL file hash."""
    manifest_bytes = read_bounded(manifest, 10_000_000)
    revision = Revision.model_validate(
        json.loads(
            manifest_bytes,
            object_pairs_hook=object_without_duplicates,
        )
    )
    # ponytail: one bounded 50 MB export in memory; stream grounded records if this
    # caller-side ceiling becomes a measured memory bottleneck.
    content = read_bounded(export, FILE_LIMIT)
    require(
        hashlib.sha256(content).hexdigest() == sha256 == revision.export_sha256,
        "Export SHA-256 differs from the caller's pinned hash or revision.",
    )
    lines = content.splitlines()
    require(bool(lines), "Export must contain a frozen source manifest.")
    header = json.loads(lines[0], object_pairs_hook=object_without_duplicates)
    require(
        isinstance(header, dict) and header.get("type") == "manifest",
        "Use a frozen JSONL export, including its first manifest record.",
    )
    source = header.get("manifest")
    require(
        source == revision.source_manifest,
        "Selection, period, coverage or source manifest changed.",
    )
    require(source.get("schema") == "teleloom-frozen-export-v1", "Unsupported export schema.")
    FrozenEvidenceSource.model_validate(source["source"])
    require(source["source_version"] == source["source_sha256"], "Frozen source versions differ.")
    require(len(lines) <= 10001, "Export exceeds 10000 original records.")
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    selected_chats = {chat["id"] for chat in source["selection"]["items"]}
    for line in lines[1:]:
        raw = json.loads(line, object_pairs_hook=object_without_duplicates)
        row = Message.model_validate(raw)
        number(row.chat_id)
        number(row.id, positive=True)
        require(row.profile_id == source["profile_id"], "Original belongs to another profile.")
        require(row.chat_id in selected_chats, "Original is outside the frozen selection.")
        iso(row.date)
        key = (row.chat_id, row.id)
        require(key not in rows, "Duplicate original identity in export.")
        rows[key] = raw

    claims = {claim.id: claim for claim in revision.claims}
    require(len(claims) == len(revision.claims), "Claim IDs must be unique.")
    for claim in revision.claims:
        require(
            unique(claim.retracts)
            and all(id in claims and id != claim.id for id in claim.retracts),
            "Retractions must name distinct existing claims other than themselves.",
        )
        for citation in claim.supporting + claim.contradicting:
            require(
                citation.profile_id == source["profile_id"]
                and citation.source_version == source["source_version"],
                "Citation belongs to another profile or frozen source version.",
            )
            original = rows.get((citation.chat_id, citation.message_id))
            require(original is not None, "Citation has no exact original in this export.")
            if citation.rendering == "verbatim":
                assert original is not None
                text = original.get("original_text")
                if text is None and original.get("text_source", "original") == "original":
                    text = original.get("text")
                require(
                    isinstance(text, str) and citation.text in text,
                    "Verbatim citation is absent from original text; reconstruction is not verbatim.",
                )

    budgets = revision.budgets
    # Canonical claims stay in one registry. Reductions carry whole records by ID,
    # including uncertainty, contrary citations and retractions, never summary-only strings.
    nodes: dict[str, list[str]] = {}
    seen_keys: list[tuple[str, str]] = []
    chunk_metrics: list[dict[str, Any]] = []
    for chunk in revision.chunks:
        require(
            chunk.id not in nodes
            and unique(chunk.claim_ids)
            and all(id in claims for id in chunk.claim_ids),
            "Chunk IDs/claim IDs must be unique and claims must exist.",
        )
        keys = [(key.chat_id, key.message_id) for key in chunk.message_keys]
        require(all(key in rows for key in keys), "Chunk contains an unknown original key.")
        require(len(keys) <= budgets.max_chunk_messages, "Chunk exceeds its message budget.")
        for id in chunk.claim_ids:
            require(
                any((cite.chat_id, cite.message_id) in keys for cite in claims[id].supporting),
                "Each chunk claim needs a supporting original in that chunk.",
            )
        packet = {
            "source_manifest": source,
            "items": [rows[key] for key in keys],
            "claims": [claims[id].model_dump() for id in chunk.claim_ids],
        }
        size = len(compact_json(packet).encode("utf-8"))
        require(size <= budgets.max_chunk_bytes, "Chunk exceeds its UTF-8 byte budget.")
        chunk_metrics.append({"id": chunk.id, "messages": len(keys), "bytes": size})
        seen_keys.extend(keys)
        nodes[chunk.id] = chunk.claim_ids
    require(
        unique(seen_keys) and set(seen_keys) == set(rows),
        "Chunks must partition every exported original exactly once, including unclaimed evidence.",
    )

    reduction_metrics: list[dict[str, Any]] = []
    for reduction in revision.reductions:
        require(
            reduction.id not in nodes
            and unique(reduction.inputs)
            and all(id in nodes for id in reduction.inputs),
            "Reduction needs distinct earlier inputs and a unique ID.",
        )
        require(
            len(reduction.inputs) <= budgets.reduce_fan_in, "Reduction exceeds its fan-in budget."
        )
        expected = {claim for id in reduction.inputs for claim in nodes[id]}
        require(
            unique(reduction.claim_ids) and set(reduction.claim_ids) == expected,
            "Reduction dropped or introduced claims; preserve decisions, uncertainty and contrary evidence.",
        )
        require(len(expected) <= budgets.max_reduce_claims, "Reduction exceeds its claim budget.")
        packet = {
            "source_manifest": source,
            "inputs": [
                {"id": id, "claims": [claims[claim].model_dump() for claim in nodes[id]]}
                for id in reduction.inputs
            ],
        }
        size = len(compact_json(packet).encode("utf-8"))
        require(size <= budgets.max_reduce_bytes, "Reduction exceeds its UTF-8 byte budget.")
        reduction_metrics.append(
            {"id": reduction.id, "inputs": len(reduction.inputs), "bytes": size}
        )
        nodes[reduction.id] = reduction.claim_ids
    final = revision.reductions[-1].id if revision.reductions else revision.chunks[-1].id
    dependencies = {reduction.id: reduction.inputs for reduction in revision.reductions}
    pending = [final]
    visited: set[str] = set()
    while pending:
        id = pending.pop()
        if id not in visited:
            visited.add(id)
            pending.extend(dependencies.get(id, []))
    require(
        {chunk.id for chunk in revision.chunks} <= visited and set(nodes[final]) == set(claims),
        "Final reduction must retain every chunk and registered claim.",
    )
    return {
        "validation": "schema_tracing_quotes",
        "semantic_quality": "NOT MEASURED",
        "current_access": "NOT CHECKED",
        "mode": revision.mode,
        "revision_id": revision.revision_id,
        "revision_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_version": source["source_version"],
        "export_sha256": sha256,
        "source_manifest": source,
        "claims": [claim.model_dump() for claim in revision.claims],
        "final_claim_ids": nodes[final],
        "messages": len(rows),
        "chunks": chunk_metrics,
        "reductions": reduction_metrics,
        "max_chunk_messages": max(row["messages"] for row in chunk_metrics),
        "max_chunk_bytes": max(row["bytes"] for row in chunk_metrics),
        "max_reduce_fan_in": max((row["inputs"] for row in reduction_metrics), default=0),
        "max_reduce_bytes": max((row["bytes"] for row in reduction_metrics), default=0),
        "model_run": revision.model_run.model_dump() if revision.model_run else None,
        "revocation_boundary": "Caller files and already issued model context are outside server revocation.",
    }, rows


def validate_revision(manifest: Path, export: Path, sha256: str) -> dict[str, Any]:
    return _ground_revision(manifest, export, sha256)[0]


def digest_validate_command(
    manifest: Path,
    export: Path = typer.Option(..., "--export"),
    sha256: str = typer.Option(
        ..., help="File hash pinned from export jobs_status, not from this revision."
    ),
) -> None:
    """Validate historical caller claims against a frozen JSONL export, entirely offline."""
    try:
        report = validate_revision(manifest, export, sha256)
    except (
        ValidationError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OSError,
        TeleloomError,
    ) as exc:
        raise TeleloomError(
            "invalid_digest",
            exc.message
            if isinstance(exc, TeleloomError)
            else "Invalid revision schema or frozen JSONL input.",
        ) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


class SourceCheck(Record):
    status: Literal["unknown", "validated", "invalidated"] = "unknown"
    reason: str = "source_not_checked"
    checked_at: str | None = None


class RefreshCheckpoint(Record):
    schema_name: Literal["teleloom-digest-refresh-v1"] = Field(
        default="teleloom-digest-refresh-v1", alias="schema"
    )
    revision_sha256: SHA256
    sources: dict[str, SourceCheck] = Field(max_length=10000)
    processed_jobs: list[Identifier] = Field(default_factory=list, max_length=1000)
    positions: dict[str, int] = Field(default_factory=dict)
    event_epoch: str | None = None
    event_cursor: str | None = None
    blocked_reason: str | None = None
    unresolved_gap: bool = False
    unknown_facts: list[str] = Field(default_factory=list, max_length=1000)
    event_coverage: dict[str, Any] = Field(default_factory=dict)
    event_warnings: list[str] = Field(default_factory=list)
    reconciliation: dict[str, Any] = Field(default_factory=dict)


async def refresh_revision(
    session: ClientSession | None,
    manifest: Path,
    export: Path,
    sha256: str,
    checkpoint: dict[str, Any],
    *,
    event_job_id: str | None = None,
    reconcile_keys: list[dict[str, str]] | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a caller checkpoint/report; never mutate originals or commit host progress.

    Only actual owned MCP calls check access. Exact-key reconciliation is explicit;
    events prove observed changes, never Telegram completeness. Commit the report
    and checkpoint together before using eligible claims on another invocation.
    """
    historical, rows = _ground_revision(manifest, export, sha256)
    source = historical["source_manifest"]
    profile = source["profile_id"]
    chats = sorted(
        {item["id"] for item in source["selection"]["items"]}
        | {item["chat_id"] for item in source["selection"]["unavailable"]}
    )
    expected_scope = {"chat_ids": chats, "period": source["period"], "query": source["query"]}
    binding = historical["revision_sha256"]
    source_keys = {f"{chat}/{id}" for chat, id in rows}
    require(len(compact_json(checkpoint).encode()) <= 10_000_000, "Checkpoint exceeds byte limit.")
    state = (
        RefreshCheckpoint.model_validate(checkpoint)
        if checkpoint
        else RefreshCheckpoint(
            revision_sha256=binding, sources={key: SourceCheck() for key in sorted(source_keys)}
        )
    )
    require(
        state.revision_sha256 == binding and set(state.sources) == source_keys,
        "Checkpoint belongs to another immutable revision; start a separate checkpoint.",
    )
    keys = [Key.model_validate(value) for value in reconcile_keys or []]
    require(
        len(keys) <= 20, "Explicit reconciliation supports at most 20 exact keys per invocation."
    )
    requested = [f"{key.chat_id}/{key.message_id}" for key in keys]
    require(
        unique(requested) and set(requested) <= source_keys,
        "Reconciliation keys must be distinct originals from this revision.",
    )
    if scope is not None and scope != expected_scope:
        state.blocked_reason = "scope_or_period_changed"

    current_access = "NOT CHECKED"
    error: str | None = None
    processed = 0
    fresh_delta = False
    checked: set[str] = set()
    calls = 0
    denied = {
        "read_not_allowed",
        "read_policy_changed",
        "account_changed",
        "invalid_reference",
        "reference_expired",
        "tool_not_exposed",
        "events_not_allowed",
        "profile_not_found",
    }

    def gap(reason: str) -> None:
        state.unresolved_gap = True
        state.unknown_facts = sorted(set(state.unknown_facts) | {reason})
        for check in state.sources.values():
            if check.status == "validated":
                check.status, check.reason = "unknown", reason

    async def call(name: str, **arguments: Any) -> dict[str, Any]:
        nonlocal calls
        assert session is not None
        calls += 1
        try:
            async with asyncio.timeout(40):
                response = await session.call_tool(name, {"profile_id": profile, **arguments})
        except Exception:
            raise TeleloomError(
                "mcp_unavailable", "Current MCP call failed; source access is unknown."
            ) from None
        content = response.structuredContent
        if response.isError or not content or not content.get("ok"):
            code = (content or {}).get("error", {}).get("code", "mcp_unavailable")
            raise TeleloomError(code, "Current MCP validation failed; no latest access inferred.")
        return dict(content["data"])

    async def access() -> None:
        envelope = await call(
            "jobs_results",
            job_id=source["source"]["job_id"],
            evidence_ref=source["source"]["evidence_ref"],
        )
        require(
            envelope["source_version"] == source["source_version"]
            and envelope["reference"]["generation"] == source["profile_generation"],
            "Owned reference no longer matches the frozen source version/generation.",
        )

    async def work() -> None:
        nonlocal current_access, processed, fresh_delta
        await access()
        current_access = "CHECKED_THIS_INVOCATION"
        if event_job_id and event_job_id not in state.processed_jobs:
            require(len(state.processed_jobs) < 1000, "Checkpoint reached its 1000-delta lifetime.")
            job = await call("jobs_status", job_id=event_job_id)
            payload = job["payload"]
            require(
                job["kind"] == "events"
                and job["status"] == "completed"
                and sorted(payload["chat_ids"]) == chats
                and not payload.get("filter"),
                "Use a completed unfiltered observed delta for the exact revision scope.",
            )
            positions = payload["after_sequence_by_chat"]
            journal_epoch = job.get("journal_epoch")
            if (
                not journal_epoch
                or state.event_epoch is None
                or positions != state.positions
                or state.event_epoch != journal_epoch
                or payload["epoch"] != journal_epoch
            ):
                gap("journal_discontinuity")
            items: list[dict[str, Any]] = []
            cursor = None
            for _ in range(10):
                page = await call("jobs_results", job_id=event_job_id, cursor=cursor, limit=100)
                items.extend(page["items"])
                cursor = page["next_cursor"]
                if not cursor:
                    break
            else:
                raise TeleloomError(
                    "event_page_budget", "Keep the prior checkpoint; delta not committed."
                )
            coverage = page["coverage"]
            if coverage.get("gap") or page["incomplete"] or not coverage.get("next_cursor"):
                gap("observed_retention_or_coverage_gap")
            for item in items:
                require(
                    item["profile_id"] == profile and item["chat_id"] in chats,
                    "Observed event belongs to another source scope.",
                )
                key = f"{item['chat_id']}/{item['id']}"
                if item["sequence"] <= state.positions.get(item["chat_id"], 0):
                    continue
                processed += 1
                state.unknown_facts = sorted(set(state.unknown_facts) | set(item["unknown_facts"]))
                if item["event"] in {"edit", "delete"} and key in state.sources:
                    state.sources[key].status = "invalidated"
                    state.sources[key].reason = f"observed_{item['event']}"
                elif key not in state.sources:
                    gap("uncollected_observed_source")
            state.positions = coverage["next_sequence_by_chat"]
            state.event_epoch = journal_epoch
            state.event_cursor = coverage.get("next_cursor")
            state.event_coverage = coverage
            state.event_warnings = page["warnings"]
            state.processed_jobs.append(event_job_id)
            fresh_delta = True

        if keys:
            results = []
            for chat in sorted({key.chat_id for key in keys}):
                selected = [key.message_id for key in keys if key.chat_id == chat]
                page = await call(
                    "messages_get",
                    chat_id=chat,
                    message_ids=selected,
                    source="live",
                    limit=len(selected),
                )
                originals = {(row["chat_id"], row["id"]): row for row in page["items"]}
                results.append(
                    {
                        "chat_id": chat,
                        "message_ids": selected,
                        "source": page["source"],
                        "coverage": page["coverage"],
                        "incomplete": page["incomplete"],
                        "warnings": page["warnings"],
                    }
                )
                for id in selected:
                    key = f"{chat}/{id}"
                    check = state.sources[key]
                    if check.status == "invalidated":
                        continue  # a later copy never resurrects an observed invalidation
                    original = originals.get((chat, id))
                    if page["source"] != "telegram" or page["incomplete"] or original is None:
                        check.status, check.reason = (
                            "unknown",
                            "current_source_missing_or_incomplete",
                        )
                    elif Message.model_validate(original) != Message.model_validate(
                        rows[(chat, id)]
                    ):
                        check.status, check.reason = "invalidated", "source_version_changed"
                    else:
                        check.status, check.reason = "validated", "exact_original_checked"
                        check.checked_at = utcnow().isoformat()
                        checked.add(key)
            state.reconciliation = {
                "keys": requested,
                "results": results,
                "checked_at": utcnow().isoformat(),
                "limitations": ["Offline edits/deletes and uncollected sources remain unknown."],
            }
        await access()  # policy/generation/expiry can change while exact sources are read

    if session is not None and state.blocked_reason is None:
        try:
            async with asyncio.timeout(120):
                await work()
        except TeleloomError as exc:
            error = exc.code
            current_access = "NOT CHECKED"
            if exc.code in denied:
                state.blocked_reason = exc.code
            else:
                gap(exc.code)
        except (TimeoutError, OSError):
            error = "mcp_unavailable"
            current_access = "NOT CHECKED"
            gap(error)

    claim_status = []
    for claim in historical["claims"]:
        citations = []
        for role in ("supporting", "contradicting"):
            for cite in claim[role]:
                key = f"{cite['chat_id']}/{cite['message_id']}"
                check = state.sources[key]
                eligible = (
                    current_access == "CHECKED_THIS_INVOCATION"
                    and state.blocked_reason is None
                    and check.status == "validated"
                    and (fresh_delta or key in checked)
                )
                citations.append(
                    {
                        "role": role,
                        "source": cite,
                        "status": "reusable"
                        if eligible
                        else "invalidated"
                        if check.status == "invalidated"
                        else "stale",
                        "reason": check.reason,
                        "checked_at": check.checked_at,
                    }
                )
        statuses = {cite["status"] for cite in citations}
        claim_status.append(
            {
                "id": claim["id"],
                "status": "invalidated"
                if "invalidated" in statuses
                else "reusable"
                if statuses == {"reusable"}
                else "stale",
                "citations": citations,
            }
        )
    return {
        "revision_id": historical["revision_id"],
        "source_version": source["source_version"],
        "claims": historical["claims"],
        "claim_status": claim_status,
        "reusable_claim_ids": [
            claim["id"] for claim in claim_status if claim["status"] == "reusable"
        ],
        "stale_claim_ids": [claim["id"] for claim in claim_status if claim["status"] == "stale"],
        "invalidated_claim_ids": [
            claim["id"] for claim in claim_status if claim["status"] == "invalidated"
        ],
        "current_access": current_access,
        "semantic_quality": "NOT MEASURED",
        "telegram_completeness": "UNKNOWN",
        "latest_complete": False,
        "source_manifest": source,
        "scope": expected_scope,
        "processed_events": processed,
        "mcp_calls": calls,
        "blocked_reason": state.blocked_reason,
        "error": error,
        "checkpoint": state.model_dump(by_alias=True),
        "revocation_boundary": historical["revocation_boundary"],
    }
