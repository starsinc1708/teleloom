"""Bounded read jobs using the daemon's existing adapter and durable queue."""

import asyncio
import json
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adapters import telegram_error
from .models import EvidenceAggregate, EvidenceKey, Message, TeleloomError
from .output_budget import compact_json, evidence_rows, fit

if TYPE_CHECKING:
    from .jobs import Jobs


# Bounded lifetimes: pagination cursors are short-lived; an owned evidence
# reference pins its frozen snapshot for its own bounded window.
PAGINATION_TTL_SECONDS = 900
REFERENCE_TTL_SECONDS = 1800
EVIDENCE_KINDS = {"evidence", "unread_export"}
CONTENT_KINDS = EVIDENCE_KINDS | {"attachments", "events", "transcription"}
DIRECT_FREEZE_BYTES = 2 * 1024 * 1024
DIRECT_FREEZE_RECORDS = 100
DIRECT_FREEZE_SNAPSHOTS = 16


def now() -> datetime:
    # Share the queue's clock, including clock substitutes in public workflow tests.
    from .jobs import utcnow

    return utcnow()


def source_version_of(job_id: str, status: str, result: dict[str, Any], chats: list[str]) -> str:
    """Verifiable identity of one frozen evidence snapshot, stable across restarts."""
    from .runtime import fingerprint

    return fingerprint(
        {
            "job_id": job_id,
            "status": status,
            "source": result.get("source"),
            "snapshot_at": result.get("coverage", {}).get("snapshot_at"),
            "chats": chats,
            "items": result.get("items", []),
        }
    )


class ReadingJobs:
    def __init__(self, owner: "Jobs") -> None:
        self.owner = owner

    @staticmethod
    def _original_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        index: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("chat_id")), str(row.get("id", row.get("message_id"))))
            original = index.get(key)
            # Context annotations do not turn one source message into a new version.
            if original is not None and (
                {k: v for k, v in original.items() if k not in {"is_target", "referenced_by"}}
                != {k: v for k, v in row.items() if k not in {"is_target", "referenced_by"}}
            ):
                raise TeleloomError(
                    "output_freeze_conflict",
                    "This exact key has multiple observed originals; request a narrower page or omit reply context.",
                    details={"chat_id": key[0], "message_id": key[1]},
                )
            index.setdefault(key, row)
        return index

    def freeze_direct(self, profile: str, tool: str, data: dict[str, Any]) -> dict[str, Any]:
        """Freeze only the returned page, inside the caller's SQLite transaction."""
        config = self.owner.settings.profile(profile)
        unique = self._original_index(evidence_rows(data))
        for row in unique.values():
            if row["profile_id"] != profile:
                raise TeleloomError("source_changed", "Evidence belongs to another profile.")
            config.require_read(row["chat_id"])
        if (
            len(unique) > DIRECT_FREEZE_RECORDS
            or len(compact_json(data).encode("utf-8")) > DIRECT_FREEZE_BYTES
        ):
            raise TeleloomError(
                "output_freeze_limit",
                "Request a smaller direct page before bounding its output.",
                details={"max_records": DIRECT_FREEZE_RECORDS, "max_bytes": DIRECT_FREEZE_BYTES},
            )
        self.owner.store.db.execute(
            "DELETE FROM state WHERE key LIKE 'evidence_ref:%' AND json_extract(data,'$.expires_at')<=?",
            (now().timestamp(),),
        )
        self.owner.store.db.execute(
            "DELETE FROM state WHERE key LIKE 'reading_results:%' AND json_extract(data,'$.expires_at')<=? AND COALESCE(json_extract(data,'$.pinned_until'),0)<=?",
            (now().timestamp(), now().timestamp()),
        )
        active = self.owner.store.db.execute(
            "SELECT count(*) FROM state WHERE key LIKE 'evidence_ref:%' AND json_extract(data,'$.profile_id')=? AND json_extract(data,'$.job_id') IS NULL",
            (profile,),
        ).fetchone()[0]
        if active >= DIRECT_FREEZE_SNAPSHOTS:
            raise TeleloomError(
                "output_freeze_limit",
                "The profile has 16 active direct freezes; reuse a reference or wait for expiry.",
            )
        reference, snapshot_id = uuid.uuid4().hex, uuid.uuid4().hex
        from .runtime import fingerprint

        version = fingerprint(data)
        expires = now().timestamp() + REFERENCE_TTL_SECONDS
        transcript_jobs = set()
        transcription = self.owner.transcription
        if transcription:
            for row in unique.values():
                if row.get("transcript") and {"id", "date"} <= row.keys():
                    cache = transcription.cached(profile, Message.model_validate(row))
                    if cache and cache["item"] == row["transcript"]:
                        self._transcript_source(profile, cache["job_id"])
                        transcript_jobs.add(cache["job_id"])
                        expires = min(expires, cache["expires_at"])
        chats = sorted({row["chat_id"] for row in unique.values()})
        snapshot_at = now().isoformat()
        record = {
            "profile_id": profile,
            "job_id": None,
            "snapshot_id": snapshot_id,
            "generation": config.generation,
            "source_version": version,
            "chats": chats,
            "status": "completed",
            "snapshot_at": snapshot_at,
            "expires_at": expires,
            "transcript_jobs": sorted(transcript_jobs),
        }
        result = {
            "items": list(unique.values()),
            "origin_tool": tool,
            **{
                key: data[key]
                for key in ("source", "coverage", "incomplete", "warnings")
                if key in data
            },
        }
        self.owner.store.set_state(f"evidence_ref:{profile}:{reference}", record)
        self.owner.store.set_state(
            f"reading_results:{profile}:{snapshot_id}",
            {
                **record,
                "result": result,
                "pinned_until": expires,
            },
        )
        return {
            **data,
            "evidence_ref": reference,
            "source_version": version,
            "reference_expires_at": datetime.fromtimestamp(expires, tz=UTC).isoformat(),
            "reference": self._reference_meta(record),
        }

    async def unread_start(
        self,
        profile: str,
        chat_ids: list[str],
        format: str,
        *,
        max_messages: int,
        max_requests: int,
        max_bytes: int,
        max_duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        from .runtime import number

        config = self.owner.settings.profile(profile)
        if not chat_ids or len(chat_ids) > 200 or len(set(chat_ids)) != len(chat_ids):
            raise TeleloomError("invalid_selection", "Provide 1–200 unique canonical chat IDs.")
        for chat in chat_ids:
            number(chat)
            config.require_read(chat)
        binding = self.owner._binding(profile)
        chats = {chat.id: chat for chat in await (await self.owner.adapter(profile)).chats()}
        self.owner._check_account(profile, binding)
        missing_bounds = {
            chat
            for chat in chat_ids
            if chat in chats
            and config.kind == "user"
            and chats[chat].unread_count
            and int(chats[chat].top_message_id) <= int(chats[chat].read_inbox_max_id)
        }
        members = [
            chats[chat].model_dump(mode="json")
            for chat in chat_ids
            if chat in chats and chat not in missing_bounds
        ]
        unavailable = [
            {
                "chat_id": chat,
                "error": {
                    "code": "unread_state_unavailable"
                    if chat in missing_bounds
                    else "chat_unavailable",
                    "message": "The unread dialog has no usable message bounds."
                    if chat in missing_bounds
                    else "The selected dialog was not observed.",
                },
            }
            for chat in chat_ids
            if chat not in chats or chat in missing_bounds
        ]
        frozen = {
            "profile_id": profile,
            "generation": config.generation,
            "items": members,
            "unavailable": unavailable,
            "snapshot_at": now().isoformat(),
            "incomplete": bool(unavailable),
        }
        result = self.start(
            profile,
            "unread_export",
            frozen,
            until=now(),
            max_messages=max_messages,
            max_requests=max_requests,
            max_characters=1000000,
            max_duration_seconds=max_duration_seconds,
        )
        job = self.owner._owned(profile, result["job_id"])
        job["payload"].update(format=format, max_bytes=max_bytes, read_policy=config.read_policy())
        if config.kind == "bot":
            # Freeze already observed pending originals; later ingress/ack cannot change this export.
            remaining = max_messages
            for index, member in enumerate(members):
                rows = self.owner.store.messages(
                    profile,
                    member["id"],
                    limit=remaining + 1,
                    incoming_only=True,
                    unprocessed_only=True,
                )
                selected = []
                for row in rows[:remaining]:
                    if self.expired(job):
                        job["result"]["coverage"]["stopped_reason"] = "total_deadline"
                        break
                    if (
                        job["payload"]["characters"] + len(row.text)
                        > job["payload"]["max_characters"]
                    ):
                        job["result"]["coverage"]["stopped_reason"] = "character_budget"
                        break
                    selected.append(row)
                    job["payload"]["characters"] += len(row.text)
                complete = len(rows) <= len(selected)
                job["result"]["items"].extend(row.model_dump(mode="json") for row in selected)
                job["result"]["coverage"]["chats"][index].update(
                    returned=len(selected),
                    complete=complete,
                    status="completed" if complete else "budget_exhausted",
                )
                remaining -= len(selected)
                job["payload"]["selection"]["incomplete"] = bool(
                    job["payload"]["selection"]["incomplete"] or not complete
                )
            job["progress"] = len(job["result"]["items"])
            job["payload"]["chat_index"] = len(members)
        job["result"]["warnings"].append(
            "Unread boundaries are frozen; Telegram originals are observed during collection. This export never acknowledges messages."
        )
        with self.owner.store.db:
            self.owner.store.put("jobs", job)
        return {
            **result,
            "source": "telegram_unread" if config.kind == "user" else "local_unprocessed",
        }

    def _selection(self, profile: str, selection: dict[str, Any]) -> dict[str, Any]:
        from .runtime import number

        config = self.owner.settings.profile(profile)
        if (
            selection.get("profile_id", profile) != profile
            or selection.get("generation", config.generation) != config.generation
        ):
            raise TeleloomError("account_changed", "Selection belongs to another account.")
        items = selection.get("items", [])
        if len(items) > 1000:
            raise TeleloomError("invalid_limit", "Select at most 1000 chats.")
        seen = set()
        for item in items:
            number(item["id"])
            config.require_read(item["id"])
            if item["id"] in seen:
                raise TeleloomError("invalid_recipients", "Select each chat only once.")
            seen.add(item["id"])
        return json.loads(json.dumps(selection))

    @staticmethod
    def _limit(value: int, maximum: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise TeleloomError("invalid_limit", f"{label} must be between 1 and {maximum}.")

    def start(
        self,
        profile: str,
        kind: str,
        selection: dict[str, Any],
        *,
        top: int = 5,
        max_requests: int = 200,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        max_messages: int = 1000,
        max_characters: int = 100000,
        max_duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        from .runtime import dates

        self._limit(max_requests, 10000, "max_requests")
        self._limit(top, 100, "top")
        self._limit(max_messages, 10000, "max_messages")
        self._limit(max_characters, 1000000, "max_characters")
        if max_duration_seconds is not None:
            self._limit(max_duration_seconds, 86400, "max_duration_seconds")
        if query is not None and (not query.strip() or len(query) > 1000):
            raise TeleloomError("invalid_query", "Query must contain 1–1000 characters.")
        start, end = dates(since, until)
        if kind == "evidence" and (start is None or end is None):
            raise TeleloomError("invalid_range", "A fixed start and end are required.")
        frozen = self._selection(profile, selection)
        started_at = now()
        comparison = started_at.isoformat()
        payload = {
            "selection": frozen,
            "comparison_at": comparison,
            "since": start,
            "until": end,
            "query": query,
            "top": top,
            "max_requests": max_requests,
            "max_messages": max_messages,
            "max_characters": max_characters,
            "chat_index": 0,
            "before": None,
            "requests": 0,
            "characters": 0,
            "retries": 0,
            "started_at": comparison,
            "deadline_at": (started_at + timedelta(seconds=max_duration_seconds)).isoformat()
            if max_duration_seconds is not None
            else None,
            "max_duration_seconds": max_duration_seconds,
        }
        source = "bot_updates" if self.owner.settings.profile(profile).kind == "bot" else "telegram"
        result: dict[str, Any] = {
            "items": [],
            "empty": [],
            "unavailable": frozen.get("unavailable", []),
            "errors": frozen.get("unavailable", []),
            "source": source,
            "incomplete": True,
            "coverage": {
                "snapshot_at": frozen.get("snapshot_at", comparison),
                "selected_chats": len(frozen["items"]),
                "requested_since": start,
                "requested_until": end,
                "requests": 0,
                "messages": 0,
                "characters": 0,
                "chats": [
                    {"chat_id": item["id"], "complete": False, "returned": 0, "status": "pending"}
                    for item in frozen["items"]
                ],
                "limits": {
                    "requests": max_requests,
                    "messages": max_messages,
                    "characters": max_characters,
                    "duration_seconds": max_duration_seconds,
                },
            },
            "warnings": ["Bot history covers saved updates only."]
            if source == "bot_updates"
            else [],
        }
        if kind == "activity":
            result["comparison_at"] = comparison
        with self.owner.store.db:
            job = self.owner._job(profile, kind, payload)
            job["result"] = result
            self.owner.store.put("jobs", job)
        return {"job_id": job["id"], "status": job["status"], "source": source}

    @staticmethod
    def remaining(job: dict[str, Any]) -> float | None:
        deadline = job["payload"].get("deadline_at")
        return (datetime.fromisoformat(deadline) - now()).total_seconds() if deadline else None

    def expired(self, job: dict[str, Any]) -> bool:
        remaining = self.remaining(job)
        return remaining is not None and remaining <= 0

    def _current(self, job: dict[str, Any]) -> dict[str, Any] | None:
        current = self.owner.store.get("jobs", job["id"])
        if current is None or current["status"] == "cancelled":
            return None
        self.owner._check_account(current["profile_id"], current.get("account"))
        self.owner._check_read(current)
        return current

    def _save(self, job: dict[str, Any]) -> None:
        p, result = job["payload"], job["result"]
        result["coverage"].update(
            requests=p["requests"],
            logical_requests=p["requests"],
            # No SDK-wide RPC instrumentation: connection/peer lookup can make extra calls.
            observed_rpc_requests=None,
            messages=job["progress"],
            characters=p["characters"],
            deadline_at=p.get("deadline_at"),
            elapsed_seconds=max(
                result["coverage"].get("elapsed_seconds", 0),
                (
                    now() - datetime.fromisoformat(p.get("started_at", job["created_at"]))
                ).total_seconds(),
            ),
        )
        with self.owner.store.db:
            # Results and the input position are one durable SQLite record.
            self.owner.store.put("jobs", job)

    def _complete(self, job: dict[str, Any], reason: str | None = None) -> None:
        p, result = job["payload"], job["result"]
        if reason:
            result["coverage"]["stopped_reason"] = reason
            result["coverage"]["budget_stop"] = True
            for chat in result["coverage"]["chats"]:
                if chat["status"] in {"pending", "reading"}:
                    chat["status"] = "budget_exhausted"
        if job["kind"] == "activity":
            result["items"].sort(key=lambda row: (-row["silence_seconds"], int(row["chat_id"])))
            result["coverage"]["ranked_chats"] = len(result["items"])
        result["incomplete"] = bool(
            reason
            or result["source"] == "bot_updates"
            or p["selection"].get("incomplete")
            or result["unavailable"]
            or any(not chat["complete"] for chat in result["coverage"]["chats"])
        )
        if job["kind"] == "unread_export":
            from .config import private_dir
            from .rich_reads import scoped_evidence

            directory = self.owner.settings.data_dir / "exports"
            private_dir(directory)
            output = directory / (job["id"] + (".jsonl" if p["format"] == "jsonl" else ".md"))
            temporary = output.with_suffix(output.suffix + ".part")
            count = 0
            with temporary.open("wb") as stream:
                for item in result["items"]:
                    if self.expired(job):
                        result["incomplete"] = True
                        result["coverage"].update(stopped_reason="total_deadline", budget_stop=True)
                        break
                    visible = scoped_evidence(
                        self.owner.settings.profile(job["profile_id"]), job["profile_id"], item
                    )
                    text = (
                        (json.dumps(visible, ensure_ascii=False) + "\n")
                        if p["format"] == "jsonl"
                        else f"## {visible['date']} · chat {visible['chat_id']} · message {visible['id']}\n\n{visible['text']}\n\nSource: {visible.get('link') or visible['chat_id']}\n\n"
                    )
                    encoded = text.encode("utf-8")
                    if stream.tell() + len(encoded) > p["max_bytes"]:
                        result["incomplete"] = True
                        result["coverage"]["stopped_reason"] = "export_byte_budget"
                        break
                    stream.write(encoded)
                    count += 1
            temporary.replace(output)
            result.update(path=str(output.resolve()), exported_messages=count)
        if reason or job["status"] != "paused":
            job["status"] = "completed"
        self._save(job)

    @staticmethod
    def _advance(job: dict[str, Any]) -> None:
        p = job["payload"]
        p["chat_index"] += 1
        p["before"] = None
        p["retries"] = 0
        job["next_run"] = 0.0

    def _failed_chat(self, job: dict[str, Any], error: TeleloomError) -> None:
        p, result = job["payload"], job["result"]
        chat_id = p["selection"]["items"][p["chat_index"]]["id"]
        detail = {"chat_id": chat_id, "error": {"code": error.code, "message": error.message}}
        result["unavailable"].append(detail)
        result["errors"].append(detail)
        result["coverage"]["chats"][p["chat_index"]].update(status="failed", error=detail["error"])
        self._advance(job)
        if p["chat_index"] == len(p["selection"]["items"]):
            self._complete(job)
        else:
            self._save(job)

    async def step(self, job: dict[str, Any]) -> None:
        p = job["payload"]
        if self.expired(job):
            self._complete(job, "total_deadline")
            return
        if p["chat_index"] == len(p["selection"]["items"]):
            self._complete(job)
            return
        if p["requests"] >= p["max_requests"]:
            self._complete(job, "request_budget")
            return
        member = p["selection"]["items"][p["chat_index"]]
        profile = job["profile_id"]
        p["requests"] += 1
        job["result"]["coverage"]["chats"][p["chat_index"]]["status"] = "reading"
        self._save(job)

        async def read() -> dict[str, Any]:
            until = p["comparison_at"] if job["kind"] == "activity" else p["until"]
            arguments = {
                "before": p["before"],
                "since": datetime.fromisoformat(p["since"]) if p["since"] else None,
                "until": datetime.fromisoformat(until),
                "limit": 100,
                "query": p["query"],
            }
            if self.owner.settings.profile(profile).kind == "bot":
                rows = self.owner.store.messages(
                    profile,
                    member["id"],
                    before=p["before"],
                    since=p["since"],
                    until=until,
                    query=p["query"],
                    limit=100,
                )
            else:
                adapter = await self.owner.adapter(profile)
                if self.expired(job):
                    raise TimeoutError
                batch = getattr(adapter, "history_batch", None)
                if batch is not None:
                    return await batch(
                        member["id"],
                        before=arguments["before"],
                        since=arguments["since"],
                        until=arguments["until"],
                        query=arguments["query"],
                    )
                rows = await adapter.history(member["id"], **arguments)
            return {
                "items": rows,
                "next_before": min(int(row.id) for row in rows) if rows else None,
                "complete": len(rows) < 100,
            }

        try:
            remaining = self.remaining(job)
            timeout = self.owner.settings.read_timeout_seconds
            batch = await asyncio.wait_for(
                read(), min(timeout, remaining) if remaining is not None else timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self._current(job)
            if current is None:
                return
            if self.expired(current):
                self._complete(current, "total_deadline")
                return
            error = (
                TeleloomError("read_timeout", "Telegram read timed out.", retry_after=1)
                if isinstance(exc, TimeoutError)
                else exc
                if isinstance(exc, TeleloomError)
                else telegram_error(exc)
            )
            p = current["payload"]
            if (
                error.retry_after is not None
                and p["retries"] < 3
                and p["requests"] < p["max_requests"]
            ):
                p["retries"] += 1
                current["next_run"] = now().timestamp() + max(error.retry_after, 0)
                current["error"] = {"code": error.code, "message": error.message}
                self._save(current)
            else:
                self._failed_chat(current, error)
            return
        current = self._current(job)
        if current is None:
            return
        p, result = current["payload"], current["result"]
        if self.expired(current):
            self._complete(current, "total_deadline")
            return
        rows = batch["items"]
        if current["kind"] == "unread_export":
            lower = int(member["read_inbox_max_id"])
            upper = int(member["top_message_id"])
            if rows and min(int(row.id) for row in rows) <= lower:
                batch["complete"] = True
            rows = [row for row in rows if lower < int(row.id) <= upper and not row.outgoing]
            if not member["unread_count"]:
                rows = []
                batch["complete"] = True
        current["error"] = None
        current["next_run"] = 0.0
        p["retries"] = 0
        try:
            from .runtime import number

            for row in rows:
                if row.profile_id != profile or row.chat_id != member["id"]:
                    raise TeleloomError(
                        "invalid_evidence", "Telegram returned another chat's message."
                    )
                number(row.id, positive=True)
            rows.sort(key=lambda row: int(row.id), reverse=True)
            next_before = batch["next_before"]
            if not batch["complete"] and (
                next_before is None
                or next_before <= 0
                or p["before"] is not None
                and next_before >= p["before"]
            ):
                raise TeleloomError(
                    "pagination_stalled", "History did not advance its message cursor."
                )
        except TeleloomError as error:
            self._failed_chat(current, error)
            return
        coverage = result["coverage"]["chats"][p["chat_index"]]
        until = datetime.fromisoformat(
            p["comparison_at"] if current["kind"] == "activity" else p["until"]
        )
        since = datetime.fromisoformat(p["since"]) if p["since"] else None
        accepted = [
            row
            for row in rows
            if not row.deleted and row.date < until and (since is None or row.date >= since)
        ]
        if current["kind"] == "activity":
            post = next(
                (row for row in accepted if getattr(row, "kind", "message") != "service"), None
            )
            if post:
                result["items"].append(
                    {
                        "chat_id": member["id"],
                        "title": member["title"],
                        "kind": member.get("kind", "chat"),
                        "id": post.id,
                        "message_id": post.id,
                        "date": post.model_dump(mode="json")["date"],
                        "link": post.link,
                        "silence_seconds": (until - post.date).total_seconds(),
                    }
                )
                current["progress"] += 1
                coverage.update(complete=True, returned=1, status="completed")
                self._advance(current)
            elif batch["complete"]:
                result["empty"].append({"chat_id": member["id"], "title": member["title"]})
                coverage.update(complete=True, status="empty")
                self._advance(current)
            else:
                p["before"] = next_before
        else:
            keys = {(item["profile_id"], item["chat_id"], item["id"]) for item in result["items"]}
            reason = None
            for row in accepted:
                if self.expired(current):
                    reason = "total_deadline"
                    break
                key = (row.profile_id, row.chat_id, row.id)
                if key in keys:
                    continue
                if current["progress"] >= p["max_messages"]:
                    reason = "message_budget"
                    break
                if p["characters"] + len(row.text) > p["max_characters"]:
                    reason = "character_budget"
                    break
                result["items"].append(row.model_dump(mode="json"))
                keys.add(key)
                p["characters"] += len(row.text)
                current["progress"] += 1
                coverage["returned"] += 1
            if reason:
                self._complete(current, reason)
                return
            if batch["complete"]:
                coverage.update(complete=True, status="completed")
                self._advance(current)
            else:
                p["before"] = next_before
        if self.expired(current):
            self._complete(current, "total_deadline")
        elif p["chat_index"] == len(p["selection"]["items"]):
            self._complete(current)
        else:
            self._save(current)

    def local_search(
        self,
        profile: str,
        chat_ids: list[str],
        query: str,
        *,
        since: datetime | None,
        until: datetime | None,
        cursor: str | None,
        limit: int,
        max_hits: int,
        snippet_characters: int,
    ) -> dict[str, Any]:
        from .rich_reads import scoped_evidence
        from .runtime import dates, fingerprint, number

        config = self.owner.settings.profile(profile)
        if not chat_ids or len(chat_ids) > 200 or len(set(chat_ids)) != len(chat_ids):
            raise TeleloomError("invalid_selection", "Select 1–200 unique canonical chat IDs.")
        for chat in chat_ids:
            number(chat)
            config.require_read(chat)
        if not query.strip() or len(query) > 1024 or len(query.split()) > 32:
            raise TeleloomError(
                "invalid_query", "Provide 1–1024 characters and at most 32 literal terms."
            )
        self._limit(limit, 100, "limit")
        self._limit(max_hits, 1000, "max_hits")
        self._limit(snippet_characters, 1000, "snippet_characters")
        start, end = dates(since, until)
        chats = sorted(chat_ids, key=int)
        scope = [chats, query, start, end, max_hits, snippet_characters, limit]
        if cursor:
            snapshot_id, sep, _ = cursor.partition(".")
            snapshot = self.owner.store.state(f"reading_results:{profile}:{snapshot_id}")
            if not sep or not snapshot:
                raise TeleloomError(
                    "invalid_cursor",
                    "Local retrieval cursor expired or belongs to another profile.",
                )
            job = self.owner._owned(profile, snapshot["job_id"])
            if job["payload"].get("local_search_scope") != scope:
                raise TeleloomError(
                    "invalid_cursor",
                    "Local retrieval cursor belongs to another selection, query or budget.",
                )
        else:
            found = self.owner.store.local_search(
                profile, chats, query, since=start, until=end, limit=max_hits + 1
            )
            items = []
            characters = 0
            for row in found["items"][:max_hits]:
                if characters + len(row["text"]) > 1000000:
                    break
                characters += len(row["text"])
                row["source_message_version"] = fingerprint(
                    {k: v for k, v in row.items() if k not in {"rank", "snippet"}}
                )
                row["snippet_truncated"] = (
                    row["snippet"] != row["text"] or len(row["snippet"]) > snippet_characters
                )
                row["snippet"] = (
                    ""
                    if row.get("text_source") == "reconstructed"
                    else row["snippet"][:snippet_characters]
                )
                row["snippet_characters"] = snippet_characters
                items.append(row)
            stopped = (
                "character_budget"
                if len(items) < min(len(found["items"]), max_hits)
                else "hit_budget"
                if len(found["items"]) > max_hits
                else None
            )
            gap = self.owner.store.state(f"gap:{profile}")
            coverage = []
            for chat in chats:
                stats = found["chats"].get(
                    chat, {"indexed_messages": 0, "earliest_date": None, "latest_date": None}
                )
                sync = self.owner.store.state(f"sync:{profile}:{chat}")
                coverage.append(
                    {
                        "chat_id": chat,
                        **stats,
                        "sync": sync,
                        "index_status": "missing"
                        if not stats["indexed_messages"] and not sync
                        else "collected"
                        if sync and sync.get("complete")
                        else "partial",
                        "last_collection_at": sync.get("updated_at") if sync else None,
                        "freshness_unknown": not bool(sync and sync.get("updated_at")),
                        "stale_possible": True,
                        "known_gap": gap,
                        "telegram_complete": False,
                        "requested_range_collected": bool(
                            sync
                            and sync.get("complete")
                            and sync.get("since")
                            and sync.get("until")
                            and sync["since"] <= start
                            and sync["until"] >= end
                        )
                        if start and end
                        else None,
                    }
                )
            result: dict[str, Any] = {
                "items": items,
                "source": "local_index",
                "incomplete": True,
                "coverage": {
                    "snapshot_at": now().isoformat(),
                    "selected_chats": len(chats),
                    "chats": coverage,
                    "requested_since": start,
                    "requested_until": end,
                    "query_semantics": "literal_AND",
                    "ordering": "bm25_asc_date_desc_chat_asc_message_id_desc",
                    "matched_total": None,
                    "frozen_hits": len(items),
                    "stopped_reason": stopped,
                    "missing_chats": [
                        c["chat_id"] for c in coverage if c["index_status"] == "missing"
                    ],
                    "limits": {
                        "hits": max_hits,
                        "characters": 1000000,
                        "snippet_characters": snippet_characters,
                    },
                    "backend_scope": "saved_updates_only"
                    if config.kind == "bot"
                    else "collected_index_only",
                    "telegram_complete": False,
                },
                "warnings": [
                    "Local index only; missing/stale evidence and no hit never prove absence in Telegram. No implicit sync, history read or AI upload."
                ],
            }
            if gap:
                result["warnings"].append(gap)
            if config.kind == "bot":
                result["warnings"].append(
                    "Bot index covers saved observations only, including MTProto bots."
                )
            with self.owner.store.db:
                job = self.owner._job(
                    profile,
                    "evidence",
                    {
                        "selection": {
                            "profile_id": profile,
                            "generation": config.generation,
                            "items": [{"id": chat} for chat in chats],
                        },
                        "local_search_scope": scope,
                    },
                )
                job.update(status="completed", result=result, progress=len(items))
                self.owner.store.put("jobs", job)
        page = self.results(profile, job["id"], cursor, limit, hit_view=True)
        page = scoped_evidence(config, profile, page)
        hits = []
        for row in page["items"]:
            # Redacted reconstructed text must not reappear through a stored snippet.
            snippet = (
                row["text"][:snippet_characters]
                if row.get("text_source") == "reconstructed"
                else row["snippet"]
            )
            hits.append(
                {
                    **{
                        key: row.get(key)
                        for key in (
                            "profile_id",
                            "chat_id",
                            "id",
                            "date",
                            "link",
                            "sender_id",
                            "text_source",
                            "rank",
                            "source_message_version",
                        )
                    },
                    "snippet": snippet,
                    "snippet_is_excerpt": True,
                    "snippet_truncated": row["snippet_truncated"]
                    or len(row["text"]) > len(snippet),
                }
            )
        return {**page, "items": hits}

    def results(
        self,
        profile: str,
        job_id: str | None,
        cursor: str | None = None,
        limit: int = 50,
        *,
        evidence_ref: str | None = None,
        message_keys: list[EvidenceKey] | None = None,
        view: str = "messages",
        coverage: str = "full",
        aggregate: EvidenceAggregate | None = None,
        max_output_bytes: int | None = None,
        original_field: str | None = None,
        content_cursor: str | None = None,
        output_projection: dict[str, Any] | None = None,
        hit_view: bool = False,
    ) -> dict[str, Any]:
        from .runtime import cursor_decode, cursor_encode

        self._limit(limit, 100, "limit")
        if coverage not in {"full", "compact"}:
            raise TeleloomError("invalid_coverage", "Choose full or compact coverage.")
        job = None

        def present(response: dict[str, Any]) -> dict[str, Any]:
            return (
                self._compact_coverage(profile, job, response)
                if coverage == "compact"
                else response
            )

        if view == "coverage" and any(
            value is not None for value in (cursor, message_keys, original_field, content_cursor)
        ):
            raise TeleloomError(
                "invalid_reference", "Coverage views take no cursor or message keys."
            )
        if view == "aggregate":
            if (
                job_id is None
                or cursor is not None
                or message_keys is not None
                or original_field is not None
                or content_cursor is not None
            ):
                raise TeleloomError(
                    "invalid_aggregate", "Aggregate an owned frozen job without cursor or keys."
                )
        elif view not in {"messages", "coverage"} or aggregate is not None:
            raise TeleloomError(
                "invalid_aggregate", "An aggregate definition requires view=aggregate."
            )
        if (original_field is not None or content_cursor is not None) and (
            not evidence_ref
            or not message_keys
            or len(message_keys) != 1
            or original_field is None
            or cursor
        ):
            raise TeleloomError(
                "invalid_reference",
                "Content continuation requires evidence_ref, one exact key and original_field, without a page cursor.",
            )
        if job_id is None:
            if not evidence_ref or cursor:
                raise TeleloomError(
                    "invalid_reference",
                    "Provide a job_id or a direct-page evidence_ref without a page cursor.",
                )
            if message_keys is not None:
                return self._originals(
                    profile,
                    None,
                    evidence_ref,
                    message_keys,
                    max_output_bytes,
                    original_field,
                    content_cursor,
                    output_projection,
                    coverage,
                )
            return present(self._reference_view(profile, None, evidence_ref))
        job = self.owner._owned(profile, job_id)
        self.owner._check_read(job)
        self.owner._check_account(profile, job.get("account"))
        if view == "aggregate":
            metadata = None
            if coverage == "compact":
                metadata = (
                    self._reference_view(profile, job, evidence_ref)
                    if evidence_ref is not None
                    else self.results(profile, job_id, view="coverage")
                )
                evidence_ref = metadata["evidence_ref"]
            response = self._aggregate(profile, job, aggregate or EvidenceAggregate(), evidence_ref)
            if metadata is not None:
                response.update(
                    {
                        key: metadata[key]
                        for key in (
                            "evidence_ref",
                            "reference_expires_at",
                            "result_snapshot_at",
                        )
                    }
                )
            return present(response)
        if (
            job["kind"] in {"events", "transcription"}
            and job["payload"]["expires_at"] <= now().isoformat()
        ):
            worker = self.owner.events if job["kind"] == "events" else self.owner.transcription
            if worker:
                with self.owner.store.db:
                    worker.discard(job)
        if job["kind"] == "events" and self.owner.events:
            self.owner.events.check(job)
        if job["kind"] == "transcription" and self.owner.transcription:
            self.owner.transcription.check(
                profile,
                job["payload"]["chat_id"],
                job["payload"]["engine"]["provider"],
                job["payload"]["allow_external_upload"],
            )
        if job["kind"] not in {
            "activity",
            "evidence",
            "attachments",
            "unread_export",
            "events",
            "transcription",
        }:
            raise TeleloomError(
                "unsupported_job_results", "This job does not produce paged reading evidence."
            )
        config = self.owner.settings.profile(profile)
        if (coverage == "compact" or view == "coverage") and job["kind"] not in EVIDENCE_KINDS:
            raise TeleloomError(
                "unsupported_job_results",
                "Compact coverage and standalone coverage views require reading-evidence jobs.",
            )
        if evidence_ref is not None or message_keys is not None:
            if cursor is not None:
                raise TeleloomError(
                    "invalid_reference",
                    "Resolve an evidence reference without a pagination cursor.",
                )
            record = self.owner.store.state(f"evidence_ref:{profile}:{evidence_ref}", {})
            if job["kind"] not in EVIDENCE_KINDS and not (
                job["kind"] in CONTENT_KINDS
                and record.get("content_reference")
                and record.get("job_id") == job_id
            ):
                raise TeleloomError(
                    "unsupported_job_results",
                    "Only frozen evidence jobs expose exact message originals.",
                )
            if not evidence_ref:
                raise TeleloomError(
                    "invalid_reference",
                    "Exact-key retrieval requires the evidence_ref issued with jobs_results.",
                )
            if message_keys is not None:
                return self._originals(
                    profile,
                    job,
                    evidence_ref,
                    message_keys,
                    max_output_bytes,
                    original_field,
                    content_cursor,
                    output_projection,
                    coverage,
                )
            return present(self._reference_view(profile, job, evidence_ref))
        if cursor:
            snapshot_id, separator, position = cursor.partition(".")
            snapshot = self.owner.store.state(f"reading_results:{profile}:{snapshot_id}")
            if (
                not separator
                or not snapshot
                or snapshot["job_id"] != job_id
                or snapshot["generation"] != config.generation
                or snapshot["expires_at"] <= now().timestamp()
            ):
                raise TeleloomError(
                    "invalid_cursor",
                    "Reading result snapshot expired or belongs to another job/account.",
                )
            offset = (
                cursor_decode(position, [profile, config.generation, job_id, snapshot_id]) or 1
            ) - 1
        else:
            snapshot_id = uuid.uuid4().hex
            result = job.get("result") or {"items": [], "coverage": {}, "incomplete": True}
            if job["kind"] == "activity":
                ranked = sorted(
                    result["items"], key=lambda row: (-row["silence_seconds"], int(row["chat_id"]))
                )
                result = {**result, "items": ranked[: job["payload"]["top"]]}
            chats = [
                str(item["id"])
                for item in job["payload"].get("selection", {}).get("items", [])
                if item.get("id")
            ]
            chats = list(
                dict.fromkeys(
                    [
                        *chats,
                        *job["payload"].get("chat_ids", []),
                        *([job["payload"]["chat_id"]] if job["payload"].get("chat_id") else []),
                    ]
                )
            )
            source_version = source_version_of(job_id, job["status"], result, chats)
            reference = (
                uuid.uuid4().hex
                if job["kind"] in EVIDENCE_KINDS
                or max_output_bytes is not None
                and job["kind"] in CONTENT_KINDS
                else None
            )
            snapshot = {
                "job_id": job_id,
                "generation": config.generation,
                "status": job["status"],
                "result": result,
                "snapshot_at": now().isoformat(),
                "expires_at": now().timestamp() + PAGINATION_TTL_SECONDS,
                "source_version": source_version,
                "chats": chats,
                "ref_id": reference,
                "pinned_until": now().timestamp() + REFERENCE_TTL_SECONDS if reference else 0.0,
            }
            if reference and job["payload"].get("expires_at"):
                snapshot["pinned_until"] = min(
                    snapshot["pinned_until"],
                    datetime.fromisoformat(job["payload"]["expires_at"]).timestamp(),
                )
            offset = 0
            with self.owner.store.db:
                self.owner.store.db.execute(
                    "DELETE FROM state WHERE key LIKE 'evidence_ref:%' AND json_extract(data,'$.expires_at')<=?",
                    (now().timestamp(),),
                )
                self.owner.store.db.execute(
                    "DELETE FROM state WHERE key LIKE 'reading_results:%' AND json_extract(data,'$.expires_at')<=? AND COALESCE(json_extract(data,'$.pinned_until'),0)<=?",
                    (now().timestamp(), now().timestamp()),
                )
                self.owner.store.set_state(f"reading_results:{profile}:{snapshot_id}", snapshot)
                if reference:
                    self._pin(profile, snapshot_id, snapshot, max_output_bytes is not None)
        if (
            max_output_bytes is not None
            and job["kind"] in CONTENT_KINDS
            and not snapshot.get("ref_id")
        ):
            # Adding a budget on an existing default cursor pins that same revision.
            snapshot["ref_id"] = uuid.uuid4().hex
            deadline = (
                datetime.fromisoformat(job["payload"]["expires_at"]).timestamp()
                if job["payload"].get("expires_at")
                else float("inf")
            )
            snapshot["pinned_until"] = min(now().timestamp() + REFERENCE_TTL_SECONDS, deadline)
            with self.owner.store.db:
                self.owner.store.set_state(f"reading_results:{profile}:{snapshot_id}", snapshot)
                self._pin(profile, snapshot_id, snapshot, True)
        if view == "coverage":
            return present(self._reference_view(profile, job, snapshot["ref_id"]))
        result = snapshot["result"]
        reference = snapshot.get("ref_id")
        source_version = snapshot.get("source_version")
        items = result.get("items", [])
        if max_output_bytes is not None and job["kind"] in CONTENT_KINDS:
            self._original_index(items)
        selected = []
        characters = 0
        for item in items[offset : offset + limit]:
            size = len(item.get("snippet" if hit_view else "text", ""))
            if hit_view and item.get("text_source") == "reconstructed":
                size = min(len(item["text"]), item["snippet_characters"])
            if size > 32000 and max_output_bytes is None:
                raise TeleloomError(
                    "result_item_too_large",
                    "An original message exceeds the 32000-character page limit; request narrower evidence.",
                )
            if characters + size > 32000 and max_output_bytes is None:
                break
            selected.append(item)
            characters += size
        more = offset + len(selected) < len(items)
        return present(
            {
                **({"_frozen": True} if max_output_bytes is not None else {}),
                **{key: value for key, value in result.items() if key != "items"},
                "job_id": job_id,
                "status": snapshot["status"],
                "items": selected,
                "next_cursor": snapshot_id
                + "."
                + cursor_encode(
                    [profile, config.generation, job_id, snapshot_id], offset + len(selected) + 1
                )
                if more
                else None,
                "result_snapshot_at": snapshot["snapshot_at"],
                "evidence_ref": reference,
                "reference_expires_at": (
                    datetime.fromtimestamp(snapshot["pinned_until"], tz=UTC).isoformat()
                    if reference and snapshot.get("pinned_until")
                    else None
                ),
                "source_version": source_version,
                "incomplete": bool(
                    result.get("incomplete") or more or snapshot["status"] != "completed"
                ),
            }
        )

    def _aggregate(
        self,
        profile: str,
        job: dict[str, Any],
        definition: EvidenceAggregate,
        reference: str | None,
    ) -> dict[str, Any]:
        from .runtime import fingerprint

        if job["kind"] not in EVIDENCE_KINDS:
            raise TeleloomError(
                "unsupported_job_results", "Only reading-evidence jobs have counts."
            )
        result = job.get("result") or {"items": [], "coverage": {}, "incomplete": True}
        status = job["status"]
        if reference is not None:
            _, snapshot = self._reference_record(profile, job, reference)
            result, status = snapshot["result"], snapshot["status"]
        if status not in {"completed", "failed", "cancelled"}:
            raise TeleloomError("aggregate_not_ready", "Counts require a terminal frozen result.")
        zone: tzinfo | None
        try:
            if definition.timezone == "UTC":
                zone = UTC
            elif re.fullmatch(r"[+-](?:[01]\d|2[0-3]):[0-5]\d", definition.timezone):
                zone = datetime.fromisoformat("2000-01-01T00:00:00" + definition.timezone).tzinfo
            else:
                zone = ZoneInfo(definition.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise TeleloomError(
                "invalid_timezone",
                "Use UTC, a numeric offset (+04:00), or a valid IANA zone (Indian/Mauritius, America/New_York).",
            ) from None

        def counts() -> dict[str, Any]:
            return {
                "count": 0,
                "known_incoming_count": 0,
                "unknown_incoming_count": 0,
                "unknown_sender_count": 0,
            }

        def metrics(counts: dict[str, Any]) -> dict[str, Any]:
            return {
                **({"count": counts["count"]} if "count" in definition.metrics else {}),
                **(
                    {
                        "incoming_count": None
                        if counts["unknown_incoming_count"]
                        else counts["known_incoming_count"],
                        "known_incoming_count": counts["known_incoming_count"],
                        "unknown_incoming_count": counts["unknown_incoming_count"],
                    }
                    if "incoming_count" in definition.metrics
                    else {}
                ),
                "unknown_sender_count": counts["unknown_sender_count"],
            }

        totals = counts()
        groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        unknown_filter_count = 0
        selection = job["payload"]["selection"]
        chats = [str(item["id"]) for item in selection["items"]]
        # ponytail: one pass over at most 10000 collected messages; use JSON1 if that ceiling grows.
        for row in result.get("items", []):
            outgoing, kind = row.get("outgoing"), row.get("kind")
            if (not definition.include_outgoing and outgoing is True) or (
                not definition.include_service and kind == "service"
            ):
                continue
            if (not definition.include_outgoing and outgoing is not False) or (
                not definition.include_service and kind not in {"message", "service"}
            ):
                unknown_filter_count += 1
                continue
            chat = str(row["chat_id"]) if "chat" in definition.group_by else None
            day = (
                datetime.fromisoformat(row["date"]).astimezone(zone).date().isoformat()
                if "day" in definition.group_by
                else None
            )
            bucket = groups.setdefault((chat, day), counts()) if definition.group_by else None
            for target in [totals, bucket] if bucket is not None else [totals]:
                target["count"] += 1
                target["known_incoming_count"] += outgoing is False
                target["unknown_incoming_count"] += outgoing is not True and outgoing is not False
                target["unknown_sender_count"] += row.get("sender_id") is None
        ranked = sorted(
            groups.items(),
            key=lambda item: (-item[1]["count"], int(item[0][0] or 0), item[0][1] or ""),
        )
        selected = ranked[: definition.top_k] if definition.top_k is not None else ranked
        incomplete = bool(result.get("incomplete") or status != "completed")
        warnings = list(result.get("warnings", []))
        if incomplete:
            warnings.append(
                "Collection has coverage gaps; observed counts are not Telegram totals."
            )
        if unknown_filter_count:
            warnings.append(
                "Some collected messages have unknown filter facts and were not counted."
            )
        identity = {
            "profile_id": profile,
            "generation": self.owner.settings.profile(profile).generation,
            "job_id": job["id"],
            "kind": job["kind"],
        }
        period = {
            "since": job["payload"].get("since"),
            "until": job["payload"].get("until"),
            "interval": "[since,until)",
            "time_basis": "publication_date",
        }
        return {
            **{key: value for key, value in result.items() if key != "items"},
            "view": "aggregate",
            "job_id": job["id"],
            "status": status,
            "source_identity": identity,
            "source_version": source_version_of(job["id"], status, result, chats),
            "source_hash": fingerprint(
                {
                    "identity": identity,
                    "selection": selection,
                    "payload_period": period,
                    "query": job["payload"].get("query"),
                    "result": result,
                    "status": status,
                }
            ),
            "selection": selection,
            "period": period,
            "query": job["payload"].get("query"),
            "items": [],
            "next_cursor": None,
            "incomplete": incomplete or bool(unknown_filter_count),
            "warnings": warnings,
            "aggregate": {
                "definition": definition.model_dump(mode="json"),
                "denominator": "collected_evidence",
                "ranking": "count_desc,chat_id_numeric_asc,day_asc",
                "observed_count": totals["count"],
                "total": None if incomplete or unknown_filter_count else totals["count"],
                "unknown_filter_count": unknown_filter_count,
                "totals": metrics(totals),
                "groups": [
                    {
                        **({"chat_id": chat} if chat is not None else {}),
                        **({"day": day} if day is not None else {}),
                        **metrics(bucket),
                    }
                    for (chat, day), bucket in selected
                ],
                "group_count": len(ranked),
                "groups_truncated": len(selected) < len(ranked),
            },
        }

    @staticmethod
    def _key_parts(value: EvidenceKey) -> tuple[str, str]:
        return str(value.chat_id), str(value.message_id)

    def _pin(self, profile: str, snapshot_id: str, snapshot: dict[str, Any], content: bool) -> None:
        self.owner.store.set_state(
            f"evidence_ref:{profile}:{snapshot['ref_id']}",
            {
                "profile_id": profile,
                "snapshot_id": snapshot_id,
                **{
                    key: snapshot[key]
                    for key in (
                        "job_id",
                        "generation",
                        "source_version",
                        "chats",
                        "status",
                        "snapshot_at",
                    )
                },
                "created_at": now().timestamp(),
                "expires_at": snapshot["pinned_until"],
                "content_reference": content,
            },
        )

    @staticmethod
    def _reference_meta(record: dict[str, Any]) -> dict[str, Any]:
        """Caller-visible description of one pinned evidence reference."""
        return {
            "job_id": record["job_id"],
            "chats": list(record["chats"]),
            "generation": record["generation"],
            "status": record["status"],
            "snapshot_at": record["snapshot_at"],
            "expires_at": datetime.fromtimestamp(record["expires_at"], tz=UTC).isoformat(),
        }

    def _reference_record(
        self, profile: str, job: dict[str, Any] | None, reference: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve one owned evidence reference, re-checking access at every hit."""
        config = self.owner.settings.profile(profile)
        record = self.owner.store.state(f"evidence_ref:{profile}:{reference}")
        if (
            not record
            or record.get("profile_id") != profile
            or record.get("job_id") != (job["id"] if job else None)
            or record.get("generation") != config.generation
        ):
            raise TeleloomError(
                "invalid_reference",
                "Evidence reference does not belong to this profile, account generation or job.",
            )
        if record["expires_at"] <= now().timestamp():
            raise TeleloomError(
                "reference_expired",
                "This evidence reference is past its bounded lifetime; the job's durable originals remain available.",
            )
        # A reference is not a grant: mixed-scope revocation closes the whole scope.
        for chat in record["chats"]:
            config.require_read(chat)
        for source_job in record.get("transcript_jobs", []):
            self._transcript_source(profile, source_job)
        snapshot = self.owner.store.state(f"reading_results:{profile}:{record['snapshot_id']}")
        if (
            not snapshot
            or snapshot.get("job_id") != (job["id"] if job else None)
            or snapshot.get("generation") != config.generation
            or snapshot.get("source_version") != record.get("source_version")
        ):
            raise TeleloomError(
                "reference_expired",
                "The pinned frozen snapshot is outside its bounded lifetime; the job's durable originals remain available.",
            )
        return record, snapshot

    def _transcript_source(self, profile: str, job_id: str) -> None:
        job = self.owner._owned(profile, job_id)
        self.owner._check_account(profile, job.get("account"))
        self.owner._check_read(job)
        p = job["payload"]
        if (job.get("result") or {}).get("cleaned") or datetime.fromisoformat(
            p["expires_at"]
        ).timestamp() <= now().timestamp():
            raise TeleloomError(
                "reference_expired", "The frozen transcript's source job was discarded or expired."
            )
        assert self.owner.transcription is not None
        self.owner.transcription.check(
            profile, p["chat_id"], p["engine"]["provider"], p["allow_external_upload"]
        )

    def _originals(
        self,
        profile: str,
        job: dict[str, Any] | None,
        reference: str,
        message_keys: list[EvidenceKey],
        max_output_bytes: int | None = None,
        original_field: str | None = None,
        content_cursor: str | None = None,
        output_projection: dict[str, Any] | None = None,
        coverage: str = "full",
    ) -> dict[str, Any]:
        from .runtime import number

        record, snapshot = self._reference_record(profile, job, reference)
        result = snapshot["result"]
        index = self._original_index(result.get("items", []))
        selected: list[dict[str, Any]] = []
        missing: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for value in message_keys:
            chat_id, message_id = self._key_parts(value)
            number(chat_id)
            number(message_id, positive=True)
            identifier = (chat_id, message_id)
            if identifier in seen:
                continue
            seen.add(identifier)
            row = index.get(identifier)
            if row is None:
                missing.append({"chat_id": chat_id, "message_id": message_id})
            else:
                selected.append(row)
        if missing:
            raise TeleloomError(
                "message_not_in_snapshot",
                "One or more exact keys are not part of this frozen evidence snapshot.",
                details={"missing_keys": missing, "reference_chats": record["chats"]},
            )
        for row in selected:
            if (
                len(row.get("text", "")) > 32000
                and max_output_bytes is None
                and original_field is None
            ):
                raise TeleloomError(
                    "result_item_too_large",
                    "A requested original exceeds the 32000-character page limit; "
                    "retrieve it through a narrower reading view.",
                )
        response = {
            **{key: value for key, value in result.items() if key != "items"},
            "job_id": job["id"] if job else None,
            "status": snapshot["status"],
            "items": selected,
            "next_cursor": None,
            "result_snapshot_at": snapshot["snapshot_at"],
            "evidence_ref": reference,
            "source_version": record["source_version"],
            "reference_expires_at": datetime.fromtimestamp(
                record["expires_at"], tz=UTC
            ).isoformat(),
            "reference": self._reference_meta(record),
            "incomplete": bool(result.get("incomplete") or snapshot["status"] != "completed"),
        }
        if coverage == "compact":
            response = self._compact_coverage(profile, job, response)
        if original_field is not None:
            if output_projection is not None:
                response["projection"] = output_projection
            return self._content(
                profile,
                reference,
                selected[0],
                response,
                original_field,
                content_cursor,
                max_output_bytes,
            )
        return {**response, "_frozen": True}

    def _content(
        self,
        profile: str,
        reference: str,
        row: dict[str, Any],
        response: dict[str, Any],
        field: str,
        cursor: str | None,
        budget: int | None,
    ) -> dict[str, Any]:
        from .rich_reads import scoped_evidence
        from .runtime import cursor_decode, cursor_encode

        if field not in {"record", "text", "original_text", "rich_text", "transcript"}:
            raise TeleloomError(
                "invalid_field", "Choose record, text, original_text, rich_text or transcript."
            )
        config = self.owner.settings.profile(profile)
        # Check/redact dereferenced private context before it becomes an opaque JSON chunk.
        visible = scoped_evidence(config, profile, {"items": [row]})
        row = visible["items"][0]
        if field != "record" and field not in row:
            raise TeleloomError("invalid_field", "The selected field is absent from this original.")
        text = compact_json(row if field == "record" else row[field])
        scope = [
            profile,
            config.generation,
            "evidence_content",
            reference,
            response["source_version"],
            row["chat_id"],
            row.get("id", row.get("message_id")),
            field,
            config.read_policy(),
        ]
        offset = (cursor_decode(cursor, scope) or 1) - 1
        if offset >= len(text):
            raise TeleloomError("invalid_cursor", "Content cursor is outside this original.")

        def build(length: int) -> dict[str, Any]:
            end = offset + length
            return {
                **response,
                "items": [],
                **(
                    {"coverage": {**response.get("coverage", {}), **visible["coverage"]}}
                    if "coverage" in visible
                    else {}
                ),
                **(
                    {
                        "warnings": list(
                            dict.fromkeys([*response.get("warnings", []), *visible["warnings"]])
                        )
                    }
                    if "warnings" in visible
                    else {}
                ),
                "content": {
                    "field": field,
                    "encoding": "json",
                    "offset_unit": "unicode_codepoints",
                    "offset": offset,
                    "next_offset": end,
                    "total_characters": len(text),
                    "value": text[offset:end],
                },
                "next_content_cursor": cursor_encode(scope, end + 1) if end < len(text) else None,
            }

        maximum = min(32000, len(text) - offset)
        result = fit(build, maximum, budget) if budget is not None else build(maximum)
        if not result["content"]["value"]:
            raise TeleloomError(
                "output_budget_too_small",
                "Increase max_output_bytes to fit metadata and at least one original content character.",
            )
        return {**result, "_frozen": True}

    def _reference_view(
        self, profile: str, job: dict[str, Any] | None, reference: str
    ) -> dict[str, Any]:
        """Describe one pinned reference: coverage and version, never the originals."""
        record, snapshot = self._reference_record(profile, job, reference)
        result = snapshot["result"]
        return {
            **{key: value for key, value in result.items() if key != "items"},
            "job_id": job["id"] if job else None,
            "status": snapshot["status"],
            "items": [],
            "next_cursor": None,
            "result_snapshot_at": snapshot["snapshot_at"],
            "evidence_ref": reference,
            "source_version": record["source_version"],
            "reference_expires_at": datetime.fromtimestamp(
                record["expires_at"], tz=UTC
            ).isoformat(),
            "reference": self._reference_meta(record),
            "incomplete": bool(result.get("incomplete") or snapshot["status"] != "completed"),
        }

    def _compact_coverage(
        self, profile: str, job: dict[str, Any] | None, response: dict[str, Any]
    ) -> dict[str, Any]:
        """Summarize calculated coverage; the same owned reference keeps all details."""
        if job is None or job["kind"] not in EVIDENCE_KINDS:
            raise TeleloomError(
                "unsupported_job_results", "Compact coverage requires a reading-evidence job."
            )
        reference = response["evidence_ref"]
        record, snapshot = self._reference_record(profile, job, reference)
        result = snapshot["result"]
        full = result.get("coverage", {})
        chats = full.get("chats", [])
        complete = sum(chat.get("complete") is True for chat in chats)
        incomplete = bool(result.get("incomplete", True) or snapshot["status"] != "completed")
        flags = {
            f"{flag}_chats": sum(bool(chat.get(flag)) for chat in chats)
            for flag in ("stale_possible", "freshness_unknown", "known_gap")
        }
        warnings = list(response.get("warnings", []))
        if incomplete:
            warnings.append(
                "Collection has coverage gaps; observed counts are not Telegram totals."
            )
        if flags["stale_possible_chats"] or flags["freshness_unknown_chats"]:
            warnings.append("Index freshness is unknown or stale evidence is possible.")
        if flags["known_gap_chats"]:
            warnings.append("The frozen scope contains known gaps.")
        observed = len(result.get("items", []))
        return {
            **{
                key: value
                for key, value in response.items()
                if key not in {"errors", "unavailable", "empty"}
            },
            "coverage": {
                **{
                    key: value
                    for key, value in response.get("coverage", {}).items()
                    if key not in {"chats", "missing_chats"}
                },
                "detail": "compact",
                "scope": {"profile_id": profile, "chat_ids": record["chats"]},
                "counts": {
                    "observed_messages": observed,
                    "total_messages": None if incomplete else observed,
                    "complete_chats": complete,
                    "unknown_chats": len(record["chats"]) - complete,
                    "unavailable_chats": len(result.get("unavailable", [])),
                    "empty_chats": len(result.get("empty", [])),
                },
                "gap_counts": dict(
                    Counter(
                        chat.get("status", chat.get("index_status", "unknown"))
                        for chat in chats
                        if chat.get("complete") is not True
                    )
                ),
                **{key: value for key, value in flags.items() if value},
                "details": {
                    "profile_id": profile,
                    "job_id": record["job_id"],
                    "evidence_ref": reference,
                    "view": "coverage",
                },
            },
            "warnings": list(dict.fromkeys(warnings)),
        }
