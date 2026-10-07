import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import stat
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .account_operations import Management

from .adapters import Adapter, telegram_error
from .config import Settings, private_dir
from .file_snapshots import DISK_LIMIT, FILE_LIMIT, _handle_path, plain_path
from .models import EvidenceAggregate, EvidenceKey, FrozenEvidenceSource, TeleloomError, utcnow
from .mutations import (
    SEND_OPERATIONS,
    MessageOperation,
    operation_capability,
    source_targets,
    telegram_content,
)
from .reading_jobs import EVIDENCE_KINDS, ReadingJobs
from .rich_reads import scoped_evidence
from .store import Store

if TYPE_CHECKING:
    from .events import EventManager
    from .media import MediaManager
    from .transcription import TranscriptionManager


class AttachmentWorker(Protocol):
    async def step(self, job: dict[str, Any]) -> None: ...

    async def cleanup_expired(self) -> None: ...

    def discard(self, job: dict[str, Any]) -> None: ...


class Jobs:
    management: "Management"

    def __init__(
        self, settings: Settings, store: Store, adapter: Callable[[str], Awaitable[Adapter]]
    ) -> None:
        self.settings, self.store, self.adapter = settings, store, adapter
        self.lock = asyncio.Lock()
        self._export_cleanup_after = 0
        self.reading = ReadingJobs(self)
        self.attachments: AttachmentWorker | None = None
        self.events: EventManager | None = None
        self.transcription: TranscriptionManager | None = None
        self.media: MediaManager | None = None
        with store.db:
            for job in store.jobs():
                unknown = False
                for position, delivery in enumerate(store.deliveries(job["id"])):
                    if delivery["status"] == "sending":
                        delivery["status"] = "unknown"
                        delivery["error"] = "Process stopped during send; reconcile in Telegram."
                        store.put_delivery(job["id"], position, delivery)
                        unknown = True
                if unknown:
                    job["status"] = "needs_review"
                    store.put("jobs", job)
                elif job["status"] == "running":
                    job["status"] = "queued"
                    store.put("jobs", job)

    def _job(self, profile: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.settings.profile(profile)
        job = {
            "id": uuid.uuid4().hex,
            "profile_id": profile,
            "account": self._binding(profile),
            "kind": kind,
            "status": "queued",
            "created_at": utcnow().isoformat(),
            "payload": payload,
            "progress": 0,
            "next_run": 0.0,
            "error": None,
            "result": None,
        }
        self.store.put("jobs", job)
        return job

    def _binding(self, profile: str) -> dict[str, Any]:
        config = self.settings.profile(profile)
        return {
            "generation": config.generation,
            "kind": config.kind,
            "identity": config.identity,
            **(
                {"bot_backend": "mtproto"}
                if config.kind == "bot" and config.bot_backend == "mtproto"
                else {}
            ),
        }

    def _check_account(self, profile: str, binding: Any) -> None:
        if binding != self._binding(profile):
            raise TeleloomError(
                "account_changed",
                "This record belongs to a previous account. Create and confirm a new plan.",
            )

    def _owned(self, profile: str, id_: str, table: str = "jobs") -> dict[str, Any]:
        self.settings.profile(profile)
        record = self.store.get(table, id_)
        if not record or record["profile_id"] != profile:
            raise TeleloomError(
                "plan_not_found" if table == "plans" else "job_not_found",
                "Record not found for this profile.",
            )
        return record

    async def sync_start(
        self, profile: str, chat: str, since: datetime | None, until: datetime | None
    ) -> dict[str, Any]:
        from .runtime import dates, number

        number(chat)
        config = self.settings.profile(profile)
        config.require_read(chat)
        if config.kind == "bot":
            raise TeleloomError(
                "capability_unavailable",
                "Bot history consists of collected updates; enable polling instead.",
            )
        if chat not in config.sync_chats:
            raise TeleloomError(
                "sync_not_allowed",
                "Select this chat through teleloom profile allow --scope sync first.",
            )
        until = until or utcnow()
        since = since or until - timedelta(days=30)
        start, end = dates(since, until)
        for current in self.store.jobs():
            if (
                current["profile_id"] == profile
                and current["kind"] == "sync"
                and current["status"] in {"queued", "running", "paused"}
                and current["payload"]["chat_id"] == chat
                and current.get("account") == self._binding(profile)
            ):
                return {"job_id": current["id"], "status": current["status"], "existing": True}
        with self.store.db:
            job = self._job(
                profile, "sync", {"chat_id": chat, "since": start, "until": end, "before": None}
            )
        return {"job_id": job["id"], "status": job["status"]}

    async def export_start(
        self,
        profile: str,
        chat: str | None,
        format: str,
        since: datetime | None,
        until: datetime | None,
        source: FrozenEvidenceSource | None = None,
    ) -> dict[str, Any]:
        from .runtime import dates, number

        if source is not None:
            if chat is not None or since is not None or until is not None:
                raise TeleloomError("invalid_source", "Frozen source excludes chat/date filters.")
            return self._frozen_export_start(profile, source, format)
        if chat is None:
            raise TeleloomError("invalid_source", "Provide a frozen source or a legacy chat_id.")
        number(chat)
        self.settings.profile(profile).require_read(chat)
        start, end = dates(since, until)
        with self.store.db:
            job = self._job(
                profile,
                "export",
                {
                    "chat_id": chat,
                    "format": format,
                    "since": start,
                    "until": end,
                    "before": None,
                    "bytes_written": 0,
                    "read_policy": self.settings.profile(profile).read_policy(),
                },
            )
        return {"job_id": job["id"], "status": job["status"], "source": "local_index"}

    def _frozen_export_start(
        self, profile: str, source: FrozenEvidenceSource, format: str
    ) -> dict[str, Any]:
        original = self._owned(profile, source.job_id)
        if original["kind"] not in EVIDENCE_KINDS:
            raise TeleloomError(
                "invalid_source", "Export requires a frozen evidence job reference."
            )
        self._check_account(profile, original.get("account"))
        self._check_read(original)
        record, snapshot = self.reading._reference_record(profile, original, source.evidence_ref)
        result = snapshot["result"]
        manifest = {
            "schema": "teleloom-frozen-export-v1",
            "source": source.model_dump(),
            "source_version": record["source_version"],
            "source_sha256": record["source_version"],
            "source_revision": record["snapshot_at"],
            "source_status": snapshot["status"],
            "profile_id": profile,
            "profile_generation": record["generation"],
            "period": {key: original["payload"].get(key) for key in ("since", "until")},
            "selection": original["payload"]["selection"],
            "query": original["payload"].get("query"),
            **{
                key: result.get(key)
                for key in ("coverage", "empty", "unavailable", "errors", "warnings")
            },
            "incomplete": bool(result.get("incomplete") or snapshot["status"] != "completed"),
            "expires_at": self.reading._reference_meta(record)["expires_at"],
            "text_semantics": "original_text and rich_text.blocks retain source evidence; text_source=reconstructed is never a verbatim quote.",
            "revocation_boundary": "Copies already saved outside the server are outside server revocation.",
        }
        with self.store.db:
            job = self._job(
                profile,
                "export",
                {
                    "source": source.model_dump(),
                    "format": format,
                    "manifest": manifest,
                    "read_policy": self.settings.profile(profile).read_policy(),
                    # Journal the bounded lease and file checkpoint with the job. It never
                    # extends the owned reference's lifetime or deletes durable originals.
                    "pin": {
                        "snapshot_id": record["snapshot_id"],
                        "expires_at": record["expires_at"],
                    },
                    "offset": 0,
                    "bytes_written": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                },
            )
        return {
            "job_id": job["id"],
            "status": job["status"],
            "source": "frozen_evidence",
            "source_version": record["source_version"],
            "expires_at": manifest["expires_at"],
        }

    def _export_paths(self, job: dict[str, Any]) -> tuple[Path, Path]:
        directory = self.settings.data_dir / "exports"
        plain_path(directory.parent, directory=True)
        if not directory.exists():
            private_dir(directory)
        directory = plain_path(directory, directory=True)
        output = directory / (
            job["id"] + (".jsonl" if job["payload"]["format"] == "jsonl" else ".md")
        )
        return output, output.with_suffix(output.suffix + ".part")

    def _discard_export(self, job: dict[str, Any]) -> None:
        p = job["payload"]
        cleanup = p.setdefault("cleanup", {"remaining": ["temporary", "output"]})
        job["result"] = None
        p["pin"]["released"] = True
        cleanup.update(status="pending", error=None)
        # Commit logical denial and remaining work before any best-effort filesystem IO.
        with self.store.db:
            self.store.put("jobs", job)
        try:
            output, temporary = self._export_paths(job)
            paths = {"temporary": temporary, "output": output}
            for part in list(cleanup["remaining"]):
                try:
                    paths[part].unlink(missing_ok=True)
                except OSError:
                    continue
                cleanup["remaining"].remove(part)
        except (OSError, TeleloomError):
            pass
        cleanup["status"] = "failed" if cleanup["remaining"] else "completed"
        if cleanup["remaining"]:
            cleanup["error"] = {
                "code": "export_cleanup_failed",
                "message": "Private export removal failed; retry jobs_control cancel.",
            }
        if job.get("error"):
            job["error"].setdefault("details", {})["cleanup"] = cleanup
        with self.store.db:
            self.store.put("jobs", job)

    def _frozen_export_check(
        self, job: dict[str, Any], *, verify_file: bool = False
    ) -> dict[str, Any]:
        p, profile = job["payload"], job["profile_id"]
        if p.get("invalidated"):
            raise TeleloomError(
                p["invalidated"],
                "This export was revoked; create a new export.",
                details={"job_id": job["id"], "cleanup": p.get("cleanup")},
            )
        try:
            self._check_account(profile, job.get("account"))
            if p["pin"]["expires_at"] <= utcnow().timestamp():
                raise TeleloomError("reference_expired", "The bounded export lease expired.")
            original = self._owned(profile, p["source"]["job_id"])
            self._check_account(profile, original.get("account"))
            self._check_read(original)
            record, snapshot = self.reading._reference_record(
                profile, original, p["source"]["evidence_ref"]
            )
            if p["read_policy"] != self.settings.profile(profile).read_policy():
                raise TeleloomError(
                    "read_policy_changed", "Create an export under the current policy."
                )
            if (
                record["source_version"] != p["manifest"]["source_version"]
                or record["snapshot_id"] != p["pin"]["snapshot_id"]
            ):
                raise TeleloomError("source_changed", "The frozen export source changed.")
            if verify_file and job.get("result"):
                output, _ = self._export_paths(job)
                self._export_hash(output, p["bytes_written"], p["sha256"])
            return snapshot
        except (TeleloomError, OSError) as exc:
            error = (
                exc
                if isinstance(exc, TeleloomError)
                else TeleloomError(
                    "source_changed", "Private export bytes are unavailable; create a new export."
                )
            )
            p["invalidated"] = error.code
            job["status"] = "failed"
            job["error"] = {"code": error.code, "message": error.message}
            self._discard_export(job)
            raise TeleloomError(
                error.code,
                error.message,
                details={"job_id": job["id"], "cleanup": p["cleanup"]},
            ) from None

    async def activity_start(
        self,
        profile: str,
        selection: dict[str, Any],
        *,
        top: int = 5,
        max_requests: int = 200,
        max_duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        return self.reading.start(
            profile,
            "activity",
            selection,
            top=top,
            max_requests=max_requests,
            max_duration_seconds=max_duration_seconds,
        )

    async def evidence_start(
        self,
        profile: str,
        selection: dict[str, Any],
        *,
        since: datetime,
        until: datetime,
        query: str | None = None,
        max_messages: int = 1000,
        max_characters: int = 100000,
        max_requests: int = 200,
        max_duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        return self.reading.start(
            profile,
            "evidence",
            selection,
            since=since,
            until=until,
            query=query,
            max_messages=max_messages,
            max_characters=max_characters,
            max_requests=max_requests,
            max_duration_seconds=max_duration_seconds,
        )

    async def results(
        self,
        profile: str,
        job_id: str | None = None,
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
    ) -> dict[str, Any]:
        return self.reading.results(
            profile,
            job_id,
            cursor,
            limit,
            evidence_ref=evidence_ref,
            message_keys=message_keys,
            view=view,
            coverage=coverage,
            aggregate=aggregate,
            max_output_bytes=max_output_bytes,
            original_field=original_field,
            content_cursor=content_cursor,
            output_projection=output_projection,
        )

    def _allowed(self, profile: str, recipients: list[str], broadcast: bool) -> None:
        config = self.settings.profile(profile)
        for chat in recipients:
            config.require_read(chat)
        allowed = config.broadcast_chats if broadcast else config.send_chats
        if any(chat not in allowed for chat in recipients):
            raise TeleloomError(
                "recipient_not_allowed", "Recipients must be explicitly configured using CLI."
            )
        if len(recipients) > self.settings.limits.recipients:
            raise TeleloomError("recipient_limit", "Plan exceeds the configured recipient limit.")

    async def preview(
        self, profile: str, recipients: list[str], text: str, reply: str | None, broadcast: bool
    ) -> dict[str, Any]:
        from .runtime import fingerprint, number

        if not recipients or len(set(recipients)) != len(recipients):
            raise TeleloomError(
                "invalid_recipients", "Provide a nonempty list of unique numeric chat IDs."
            )
        if len(recipients) > 1 and not broadcast:
            raise TeleloomError(
                "broadcast_required",
                "Multiple recipients require broadcast=true and its separate allowlist.",
            )
        for chat in recipients:
            number(chat)
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
            raise TeleloomError(
                "invalid_text", "Text must be nonempty and at most 4096 UTF-16 units."
            )
        if reply:
            number(reply, positive=True)
            if len(recipients) != 1:
                raise TeleloomError(
                    "invalid_reply",
                    "A reply target belongs to one chat; broadcasts cannot share it.",
                )
        self._allowed(profile, recipients, broadcast)
        adapter = await self.adapter(profile)
        resolved = [await adapter.resolve(chat) for chat in recipients]
        if [chat.id for chat in resolved] != recipients:
            raise TeleloomError(
                "recipient_changed",
                "A resolved target changed its canonical identity. Preview canonical IDs.",
            )
        if reply and not await adapter.history(
            recipients[0], before=None, since=None, until=None, limit=1, ids=[int(reply)]
        ):
            raise TeleloomError("message_not_found", "Reply target was not found in this chat.")
        payload = {
            "profile_id": profile,
            "account": self._binding(profile),
            "recipients": recipients,
            "text": text,
            "reply_to_message_id": reply,
            "broadcast": broadcast,
            "format": "plain_text",
            "resolved_targets": [chat.model_dump(mode="json") for chat in resolved],
        }
        plan = {
            "id": uuid.uuid4().hex,
            "profile_id": profile,
            "payload": payload,
            "hash": fingerprint(payload),
            "expires_at": (utcnow() + timedelta(minutes=15)).isoformat(),
            "job_id": None,
            "resolved": [chat.model_dump() for chat in resolved],
        }
        with self.store.db:
            self.store.put("plans", plan)
        return {
            "plan_id": plan["id"],
            "plan_hash": plan["hash"],
            "expires_at": plan["expires_at"],
            "preview": payload,
            "resolved_recipients": plan["resolved"],
            "confirmation_required": True,
            "trust_boundary": "Client must obtain human confirmation in dialogue.",
        }

    async def execute(
        self, profile: str, plan_id: str, plan_hash: str, confirmed: bool
    ) -> dict[str, Any]:
        from .runtime import fingerprint

        plan = self._owned(profile, plan_id, "plans")
        self._check_account(profile, plan["payload"].get("account"))
        if not confirmed:
            raise TeleloomError(
                "confirmation_required",
                "Obtain the owner's explicit confirmation of the full preview.",
            )
        if plan_hash != plan["hash"] or fingerprint(plan["payload"]) != plan_hash:
            raise TeleloomError("plan_changed", "Hash does not match the immutable preview.")
        self._check_read({"profile_id": profile, "kind": "delivery", "payload": plan["payload"]})
        if plan["job_id"]:
            return {"job_id": plan["job_id"], "existing": True}
        if utcnow() >= datetime.fromisoformat(plan["expires_at"]):
            raise TeleloomError("plan_expired", "Create and confirm a fresh preview.")
        payload = plan["payload"]
        if "operation" in payload:
            self._operation_allowed(profile, payload["operation"])
            if payload["operation"]["kind"].startswith(("group_", "account_")):
                await self.management.validate(
                    profile, payload["operation"], payload["source_messages"]
                )
            elif payload["operation"]["kind"].startswith("media_") and self.media is not None:
                self.media.validate_sources(
                    profile, payload["operation"], payload["source_messages"]
                )
        else:
            self._allowed(profile, payload["recipients"], payload["broadcast"])
        with self.store.db:
            job = self._job(profile, "delivery", payload)
            targets = payload.get("targets") or [
                {"kind": "chat", "chat_id": chat} for chat in payload["recipients"]
            ]
            for position, target in enumerate(targets):
                self.store.put_delivery(
                    job["id"],
                    position,
                    {
                        **({"chat_id": target["chat_id"]} if target["kind"] == "chat" else {}),
                        "target": target,
                        "status": "pending",
                        "random_id": secrets.randbits(63),
                        "message_id": None,
                        "error": None,
                    },
                )
            plan["job_id"] = job["id"]
            self.store.put("plans", plan)
        return {"job_id": job["id"], "status": "queued", "existing": False}

    def _operation_allowed(self, profile: str, operation: dict[str, Any]) -> None:
        config = self.settings.profile(profile)
        self._operation_read(profile, operation)
        if operation["kind"].startswith("media_"):
            if self.media is None:
                raise TeleloomError("capability_unavailable", "The media worker is unavailable.")
            self.media.allowed(profile, operation)
            return
        if operation["kind"].startswith("contacts_"):
            from .contacts import contact_operation_allowed

            contact_operation_allowed(config, operation)
            return
        if operation["kind"].startswith("folder_"):
            from .folder_operations import folder_operation_allowed

            folder_operation_allowed(config, operation)
            return
        if operation["kind"].startswith(("group_", "account_")):
            from .account_operations import operation_allowed

            operation_allowed(config, operation)
            return
        operation_capability(config.kind, operation, getattr(config, "bot_backend", "bot_api"))
        allowed = (
            config.send_chats if operation["kind"] in SEND_OPERATIONS else config.mutation_chats
        )
        if operation["chat_id"] not in allowed:
            raise TeleloomError(
                "recipient_not_allowed"
                if operation["kind"] in SEND_OPERATIONS
                else "mutation_not_allowed",
                "Select this exact chat through the owner's CLI permission scope first.",
            )

    def _operation_read(self, profile: str, operation: dict[str, Any]) -> None:
        config = self.settings.profile(profile)
        for key in ("chat_id", "source_chat_id", "send_as", "bot_id"):
            if chat := operation.get(key):
                config.require_read(chat)

    async def _operation_sources(
        self, profile: str, operation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        from .rich_reads import scoped_evidence

        self._operation_read(profile, operation)
        adapter = await self.adapter(profile)
        if operation["kind"].startswith("contacts_"):
            from .contacts import validate_contact_revision

            await validate_contact_revision(adapter, operation)
            return []
        if operation["kind"].startswith("folder_"):
            from .folder_operations import validate_folder_revision

            await validate_folder_revision(adapter, operation)
            return []
        if operation["kind"].startswith(("group_", "account_")):
            from .account_operations import state

            return await state(adapter, operation)
        sources = []
        for chat, ids in source_targets(operation):
            rows = await adapter.history(
                chat,
                before=None,
                since=None,
                until=None,
                limit=len(ids),
                ids=[int(id_) for id_ in ids],
            )
            by_id = {
                row.id: row
                for row in rows
                if row.chat_id == chat and row.profile_id == profile and not row.deleted
            }
            if any(id_ not in by_id for id_ in ids):
                raise TeleloomError(
                    "message_not_found",
                    "A selected source message was not found in its exact chat.",
                )
            for id_ in ids:
                row = by_id[id_].model_dump(
                    mode="json", exclude={"views", "reactions", "reply_count", "pinned"}
                )
                sources.extend(
                    scoped_evidence(self.settings.profile(profile), profile, {"items": [row]})[
                        "items"
                    ]
                )
        return sources

    async def operation_preview(self, profile: str, operation: MessageOperation) -> dict[str, Any]:
        p = operation.model_dump(mode="json")
        self._operation_allowed(profile, p)
        if p["kind"] == "unpin_all":
            p["scope_semantics"] = (
                "Clears all current pins in this exact chat at execution, including pins "
                "added after preview or between batches. Telegram batches are not atomic."
            )
        if "text" in p and not p["format"].startswith("rich_"):
            rendered = telegram_content(p)
            rendered_entities = [entity.to_dict() for entity in rendered["entities"]]
            for entity in rendered_entities:
                if isinstance(entity.get("date"), datetime):
                    entity["date"] = entity["date"].isoformat()
            p["rendered"] = {
                "text": rendered["message"],
                "entities": rendered_entities,
            }
        adapter = await self.adapter(profile)
        resolved = await adapter.resolve(p["chat_id"])
        if resolved.id != p["chat_id"]:
            raise TeleloomError("recipient_changed", "The target changed its canonical identity.")
        sources = await self._operation_sources(profile, p)
        if p["kind"] == "forward" and p["expand_album"] and len(p["message_ids"]) == 1:
            anchor = next(
                row
                for row in sources
                if row["chat_id"] == p["source_chat_id"] and row["id"] == p["message_ids"][0]
            )
            if anchor["grouped_id"]:
                id_ = int(anchor["id"])
                neighbors = await adapter.history(
                    p["source_chat_id"],
                    before=None,
                    since=None,
                    until=None,
                    limit=19,
                    ids=list(range(max(1, id_ - 9), id_ + 10)),
                )
                siblings = sorted(
                    {
                        row.id
                        for row in neighbors
                        if row.grouped_id == anchor["grouped_id"]
                        and row.chat_id == p["source_chat_id"]
                        and row.profile_id == profile
                        and not row.deleted
                    },
                    key=int,
                )
                if anchor["id"] not in siblings or len(siblings) > 10:
                    raise TeleloomError(
                        "album_unavailable",
                        "Telegram's bounded album window did not identify a valid complete selection.",
                    )
                p["message_ids"] = siblings
                sources = await self._operation_sources(profile, p)
        if p.get("quote_text"):
            source = next(
                row
                for row in sources
                if row["id"] == p["reply_to_message_id"] and row["chat_id"] == p["chat_id"]
            )
            quote = p["quote_text"].encode("utf-16-le")
            original = source["text"].encode("utf-16-le")
            offset = p.get("quote_offset")
            if offset is None:
                found = original.find(quote)
                if found < 0 or found % 2 or original.find(quote, found + len(quote)) >= 0:
                    raise TeleloomError(
                        "invalid_quote", "Provide an exact quote_offset for an ambiguous quote."
                    )
                p["quote_offset"] = offset = found // 2
            if original[offset * 2 : offset * 2 + len(quote)] != quote:
                raise TeleloomError(
                    "invalid_quote",
                    "The quote must match the exact UTF-16 span of the reviewed source.",
                )
        if p.get("send_as"):
            state = await adapter.message_state(p["chat_id"], "send_as", None, 100, "", None)
            if p["send_as"] not in {item["id"] for item in state["items"]}:
                raise TeleloomError(
                    "send_as_not_allowed",
                    "Telegram did not list this send-as identity for the destination.",
                )
        if p["kind"] == "inline_callback":
            state = await adapter.message_state(
                p["chat_id"], "buttons", p["message_id"], 100, "", None
            )
            button = next(
                (item for item in state["items"] if item["index"] == p["button_index"]), None
            )
            if not button or not button["data_base64"] or button.get("requires_password"):
                raise TeleloomError(
                    "callback_unavailable",
                    "Select an existing callback button; URL and switch buttons do not send callbacks.",
                )
            p["callback"] = button
        if p["kind"] == "inline_send":
            state = await adapter.message_state(
                p["chat_id"], "inline_results", None, 100, p["query"], p["bot_id"]
            )
            result = next((item for item in state["items"] if item["id"] == p["result_id"]), None)
            if not result:
                raise TeleloomError(
                    "inline_result_not_found", "Select an exact result returned by this bot/query."
                )
            p["inline_result"] = {**result, "query_id": state["query_id"]}
        if p["kind"] == "scheduled_delete":
            state = await adapter.message_state(p["chat_id"], "scheduled", None, 100, "", None)
            scheduled = {item["id"]: item for item in state["items"]}
            if any(id_ not in scheduled for id_ in p["message_ids"]):
                raise TeleloomError(
                    "scheduled_message_not_found",
                    "Selected scheduled messages were not found in this exact chat.",
                )
            p["scheduled_messages"] = [scheduled[id_] for id_ in p["message_ids"]]
        return self.confirmed_preview(
            profile,
            p,
            targets=[{"kind": "chat", "chat_id": p["chat_id"]}],
            sources=sources,
            resolved=[resolved.model_dump(mode="json")],
        )

    def confirmed_preview(
        self,
        profile: str,
        operation: dict[str, Any],
        *,
        targets: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        resolved: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist an already validated family preview; never exposed as a raw MCP writer."""
        from .runtime import fingerprint

        if not targets or len(targets) > self.settings.limits.recipients:
            raise TeleloomError("invalid_targets", "Provide a bounded exact operation target.")
        payload = {
            "profile_id": profile,
            "account": self._binding(profile),
            "recipients": [target["chat_id"] for target in targets if target["kind"] == "chat"],
            "targets": targets,
            "broadcast": False,
            "operation": operation,
            "source_messages": sources,
            "resolved_targets": resolved,
        }
        plan = {
            "id": uuid.uuid4().hex,
            "profile_id": profile,
            "payload": payload,
            "hash": fingerprint(payload),
            "expires_at": (utcnow() + timedelta(minutes=15)).isoformat(),
            "job_id": None,
            "resolved": resolved,
        }
        with self.store.db:
            self.store.put("plans", plan)
        return {
            "plan_id": plan["id"],
            "plan_hash": plan["hash"],
            "expires_at": plan["expires_at"],
            "preview": payload,
            "source_messages": sources,
            "resolved_recipients": plan["resolved"],
            "confirmation_required": True,
            "trust_boundary": "Client must obtain human confirmation of this complete operation in dialogue.",
            "limitations": "Telegram enforces account rights, age/size limits and Premium at execution. Scheduled acceptance does not establish future delivery.",
        }

    async def status(self, profile: str, id_: str | None) -> dict[str, Any]:
        self.settings.profile(profile)
        if id_:
            job = self._owned(profile, id_)
            self._check_read(job, verify_export=True)
            if job["kind"] in {
                "activity",
                "evidence",
                "attachments",
                "unread_export",
                "events",
                "transcription",
            }:
                self._check_account(profile, job.get("account"))
            if job["kind"] == "events" and self.events:
                self.events.check(job)
            if job["kind"] == "transcription" and self.transcription:
                self.transcription.check(
                    profile,
                    job["payload"]["chat_id"],
                    job["payload"]["engine"]["provider"],
                    job["payload"]["allow_external_upload"],
                )
            if job["kind"] == "activity" and job.get("result"):
                job = {
                    **job,
                    "result": {
                        **job["result"],
                        "items": sorted(
                            job["result"]["items"],
                            key=lambda row: (-row["silence_seconds"], int(row["chat_id"])),
                        )[: job["payload"]["top"]],
                    },
                }
            if job["kind"] in {
                "evidence",
                "attachments",
                "unread_export",
                "events",
                "transcription",
            }:
                result = job.get("result")
                if result:
                    job = {
                        **job,
                        "result": {
                            **{key: value for key, value in result.items() if key != "items"},
                            "returned": len(result.get("items", [])),
                            "results_tool": "jobs_results",
                        },
                    }
            return {
                **job,
                **(
                    {"journal_epoch": self.events.epoch}
                    if job["kind"] == "events" and self.events
                    else {}
                ),
                "deliveries": self.store.deliveries(id_) if job["kind"] == "delivery" else [],
            }
        return {"jobs": self.store.job_summaries(profile)}

    async def control(self, profile: str, id_: str, action: str) -> dict[str, Any]:
        job = self._owned(profile, id_)
        if action == "cancel" and job["kind"] == "export" and job["payload"].get("source"):
            if job["status"] != "failed":
                job["status"] = "cancelled"
                job["payload"].setdefault("invalidated", "export_cancelled")
                job["error"] = {"code": "export_cancelled", "message": "This export was cancelled."}
            self._discard_export(job)
            return {"job_id": id_, "status": job["status"], "cleanup": job["payload"]["cleanup"]}
        if action != "cancel":
            self._check_account(profile, job.get("account"))
        if action == "resume":
            self._check_exposure(job)
            if job["kind"] == "export" and job["payload"].get("source"):
                self._check_read(job)
        if job["status"] in {"completed", "failed", "cancelled"}:
            raise TeleloomError("job_terminal", "This job is already terminal.")
        if action in {"pause", "resume"} and any(
            d["status"] in {"unknown", "sending"} for d in self.store.deliveries(id_)
        ):
            raise TeleloomError(
                "delivery_unknown",
                "Reconcile unknown deliveries in Telegram; automatic resume is prohibited.",
            )
        if action == "resume" and job["status"] != "paused":
            raise TeleloomError("job_not_paused", "Only paused jobs can be resumed.")
        if (
            action != "cancel"
            and job["kind"] == "transcription"
            and job["payload"].get("receipt", {}).get("status") in {"sending", "unknown"}
        ):
            raise TeleloomError(
                "transcription_unknown", "An uncertain upload cannot be resumed or replayed."
            )
        job["status"] = {"pause": "paused", "resume": "queued", "cancel": "cancelled"}[action]
        with self.store.db:
            self.store.put("jobs", job)
            if action == "cancel":
                if job["kind"] == "attachments" and self.attachments is not None:
                    self.attachments.discard(job)
                if job["kind"] == "events" and self.events:
                    self.events.discard(job)
                if job["kind"] == "transcription" and self.transcription:
                    self.transcription.discard(job)
                for position, delivery in enumerate(self.store.deliveries(id_)):
                    if delivery["status"] == "pending":
                        delivery["status"] = "cancelled"
                        self.store.put_delivery(id_, position, delivery)
        return {"job_id": id_, "status": job["status"]}

    async def tick(self) -> None:
        async with self.lock:
            # ponytail: reclaim at most 8 retained exports per tick, round-robin;
            # path access revalidates immediately regardless of background position.
            exports = self.store.retained_exports(self._export_cleanup_after)
            self._export_cleanup_after = exports[-1][0] if exports else 0
            for _, export_job in exports:
                cleanup = export_job["payload"].get("cleanup")
                if cleanup and cleanup["remaining"]:
                    self._discard_export(export_job)
                else:
                    with contextlib.suppress(TeleloomError):
                        self._check_read(export_job)
            if self.attachments is not None:
                await self.attachments.cleanup_expired()
            if self.events:
                await self.events.cleanup_expired()
            if self.transcription:
                await self.transcription.cleanup_expired()
            if self.media is not None:
                self.media.cleanup_expired()
            for metadata in self.store.active_jobs():
                deadline = metadata["deadline_at"]
                if metadata["next_run"] > utcnow().timestamp() and not (
                    metadata["kind"] in {"activity", "evidence", "unread_export"}
                    and deadline
                    and datetime.fromisoformat(deadline) <= utcnow()
                ):
                    continue
                job = self.store.get("jobs", metadata["id"])
                if job is None or job["status"] not in {"queued", "running"}:
                    continue
                try:
                    self._check_account(job["profile_id"], job.get("account"))
                    self._check_read(job)
                    self._check_exposure(job)
                    if job["kind"] == "sync":
                        await self._sync(job)
                    elif job["kind"] == "export":
                        self._export(job)
                    elif job["kind"] in {"activity", "evidence", "unread_export"}:
                        await self.reading.step(job)
                    elif job["kind"] == "attachments":
                        if self.attachments is None:
                            raise TeleloomError(
                                "capability_unavailable", "Attachment worker is unavailable."
                            )
                        await self.attachments.step(job)
                    elif job["kind"] == "events" and self.events:
                        await self.events.step(job)
                    elif job["kind"] == "transcription" and self.transcription:
                        await self.transcription.step(job)
                    else:
                        await self._deliver(job)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = exc if isinstance(exc, TeleloomError) else telegram_error(exc)
                    current = self.store.get("jobs", job["id"])
                    if current and current["status"] not in {"cancelled", "paused", "needs_review"}:
                        current["error"] = {
                            "code": error.code,
                            "message": error.message,
                            "details": error.details,
                        }
                        current["status"] = (
                            "paused"
                            if error.code == "tool_not_exposed"
                            else "queued"
                            if error.retry_after is not None
                            else "failed"
                        )
                        current["next_run"] = utcnow().timestamp() + (error.retry_after or 0)
                        if (
                            current["kind"] == "attachments"
                            and self.attachments is not None
                            and current["status"] == "failed"
                        ):
                            self.attachments.discard(current)
                        if (
                            current["kind"] == "export"
                            and current["payload"].get("source")
                            and current["status"] == "failed"
                            and not current["payload"].get("cleanup")
                        ):
                            self._discard_export(current)
                        with self.store.db:
                            self.store.put("jobs", current)

    def _check_exposure(self, job: dict[str, Any]) -> None:
        if job["kind"] == "delivery" and (
            self.settings.exposure_mode == "read-only"
            or self.settings.exposure_mode == "selected"
            and "delivery_execute" not in self.settings.exposed_tools
        ):
            raise TeleloomError(
                "tool_not_exposed",
                "External delivery is disabled by the owner's tool exposure policy.",
            )
        if job["kind"] == "transcription" and (
            self.settings.exposure_mode == "selected"
            and "transcription_start" not in self.settings.exposed_tools
            or self.settings.exposure_mode == "read-only"
            and job["payload"]["engine"]["provider"] in {"openai", "groq"}
        ):
            raise TeleloomError(
                "tool_not_exposed",
                "Transcription execution is disabled by the owner's tool exposure policy.",
            )

    def _check_read(self, job: dict[str, Any], *, verify_export: bool = False) -> None:
        if job["kind"] == "export" and job["payload"].get("source"):
            self._frozen_export_check(job, verify_file=verify_export)
            return
        config = self.settings.profile(job["profile_id"])
        payload = job["payload"]
        for chat in payload.get("operation", {}).get("read_chat_ids", []):
            config.require_read(chat)
        if payload.get("operation", {}).get("kind") == "account_privacy":
            self._operation_allowed(job["profile_id"], payload["operation"])
        if operation := payload.get("operation"):
            self._operation_read(job["profile_id"], operation)
        for source in payload.get("source_messages", []):
            if chat := source.get("chat_id"):
                config.require_read(chat)
        for chat in payload.get("recipients", []):
            config.require_read(chat)
        for chat in payload.get("chat_ids", []):
            config.require_read(chat)
        if payload.get("chat_id"):
            config.require_read(payload["chat_id"])
        for item in payload.get("selection", {}).get("items", []):
            config.require_read(item["id"])
        for item in payload.get("selection", {}).get("unavailable", []):
            config.require_read(item["chat_id"])
        if (
            job["kind"] in {"export", "unread_export"}
            and payload.get("read_policy", ["all", []]) != config.read_policy()
        ):
            raise TeleloomError(
                "read_policy_changed",
                "This export belongs to another read policy. Create a new export from the preserved evidence.",
            )

    async def _sync(self, job: dict[str, Any]) -> None:
        p = job["payload"]
        config = self.settings.profile(job["profile_id"])
        if p["chat_id"] not in config.sync_chats:
            raise TeleloomError("sync_not_allowed", "Synchronization permission was removed.")
        adapter = await self.adapter(job["profile_id"])
        rows = await adapter.history(
            p["chat_id"],
            before=p["before"],
            since=datetime.fromisoformat(p["since"]),
            until=datetime.fromisoformat(p["until"]),
            limit=100,
        )
        current = self.store.get("jobs", job["id"])
        if not current or current["status"] == "cancelled":
            return
        current["progress"] += len(rows)
        current["payload"]["before"] = int(rows[-1].id) if rows else p["before"]
        if not rows:
            current["status"] = "completed"
            current["result"] = {
                "messages_processed": current["progress"],
                "source": "telegram",
                "since": p["since"],
                "until": p["until"],
                "warning": "Deletion history while offline cannot be fully reconstructed.",
            }
        checkpoint = {
            "since": p["since"],
            "until": p["until"],
            "before": current["payload"]["before"],
            "complete": not rows,
            "updated_at": utcnow().isoformat(),
        }
        with self.store.db:
            self.store.save_messages(rows, (f"sync:{job['profile_id']}:{p['chat_id']}", checkpoint))
            self.store.put("jobs", current)

    def _export(self, job: dict[str, Any]) -> None:
        if job["payload"].get("source"):
            self._frozen_export(job)
            return
        p = job["payload"]
        directory = self.settings.data_dir / "exports"
        private_dir(directory)
        suffix = ".jsonl" if p["format"] == "jsonl" else ".md"
        output = directory / (job["id"] + suffix)
        temporary = output.with_suffix(output.suffix + ".part")
        rows = self.store.messages(
            job["profile_id"],
            p["chat_id"],
            before=p["before"],
            since=p["since"],
            until=p["until"],
            limit=100,
        )
        if rows:
            content = ""
            for row in rows:
                visible = scoped_evidence(
                    self.settings.profile(job["profile_id"]),
                    job["profile_id"],
                    row.model_dump(mode="json"),
                )
                if p["format"] == "jsonl":
                    content += json.dumps(visible, ensure_ascii=False) + "\n"
                else:
                    content += f"## {row.date.isoformat()} · message {row.id}\n\n{visible['text']}\n\nSource: {row.link or 'chat ' + row.chat_id + ', message ' + row.id}\n\n"
            with temporary.open("r+b" if temporary.exists() else "w+b") as stream:
                stream.truncate(p["bytes_written"])
                stream.seek(p["bytes_written"])
                stream.write(content.encode("utf-8"))
                p["bytes_written"] = stream.tell()
            p["before"] = int(rows[-1].id)
            job["progress"] += len(rows)
        else:
            if not temporary.exists() and not output.exists():
                temporary.touch()
            if temporary.exists():
                temporary.replace(output)
            job["status"] = "completed"
            job["result"] = {
                "path": str(output.resolve()),
                "messages": job["progress"],
                "source": "local_index",
                "incomplete": True,
                "sync": self.store.state(f"sync:{job['profile_id']}:{p['chat_id']}"),
                "warning": "Export covers the local snapshot; uncollected or stale messages may differ from Telegram.",
            }
        with self.store.db:
            self.store.put("jobs", job)

    @staticmethod
    def _export_hash(path: Path, size: int, expected: str) -> None:
        path = plain_path(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or _handle_path(stream, path) != path
                or info.st_size != size
            ):
                raise TeleloomError("source_changed", "Export bytes differ from their checkpoint.")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
            current = plain_path(path).stat()
            if (
                digest != expected
                or info.st_size != after.st_size
                or info.st_mtime_ns != after.st_mtime_ns
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                or _handle_path(stream, path) != path
            ):
                raise TeleloomError("source_changed", "Export bytes changed during verification.")

    def _frozen_export(self, job: dict[str, Any]) -> None:
        p = job["payload"]
        snapshot = self._frozen_export_check(job)
        output, temporary = self._export_paths(job)
        items = snapshot["result"].get("items", [])

        def encode(value: dict[str, Any], heading: str) -> bytes:
            text = json.dumps(value, ensure_ascii=False) + "\n"
            # JSON in Markdown preserves whitespace/entities/blocks and provenance,
            # rather than rendering reconstructed text as a verbatim original.
            if p["format"] == "markdown":
                text = f"## {heading}\n\n```json\n{text}```\n\n"
            return text.encode("utf-8")

        if output.exists():
            # Recovery after atomic rename but before the terminal SQLite write.
            if p["offset"] != len(items):
                raise TeleloomError("source_changed", "Export was published before its checkpoint.")
            self._export_hash(output, p["bytes_written"], p["sha256"])
        else:
            content = b""
            if p["bytes_written"] == 0:
                content = encode({"type": "manifest", "manifest": p["manifest"]}, "Source manifest")
            count = 0
            for item in items[p["offset"] : p["offset"] + 100]:
                visible = scoped_evidence(
                    self.settings.profile(job["profile_id"]), job["profile_id"], item
                )
                encoded = encode(visible, f"Chat {item['chat_id']} · message {item['id']}")
                if count and len(content) + len(encoded) > 1_000_000:
                    break
                content += encoded
                count += 1
            if p["bytes_written"] + len(content) > FILE_LIMIT:
                raise TeleloomError("export_byte_limit", "Frozen export exceeds 50000000 bytes.")
            used = sum(plain_path(child).stat().st_size for child in output.parent.iterdir())
            if used + len(content) > DISK_LIMIT:
                raise TeleloomError(
                    "file_disk_limit", "The private export disk budget is exhausted."
                )
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            if temporary.exists():
                plain_path(temporary)
            else:
                if p["bytes_written"]:
                    raise TeleloomError(
                        "source_changed", "The checkpointed partial export is missing."
                    )
                flags |= os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open(temporary, flags, 0o600), "r+b") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or _handle_path(stream, temporary) != temporary
                    or info.st_size < p["bytes_written"]
                ):
                    raise TeleloomError("unsafe_file_path", "The partial export identity changed.")
                stream.truncate(p["bytes_written"])
                # ponytail: bounded 50 MB prefix rehash per chunk; persisted chunk hashes
                # if repeated verification becomes a measured bottleneck.
                digest = hashlib.file_digest(stream, "sha256")
                if digest.hexdigest() != p["sha256"]:
                    raise TeleloomError("source_changed", "The partial export checkpoint changed.")
                stream.seek(p["bytes_written"])
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                digest.update(content)
                p.update(
                    bytes_written=stream.tell(),
                    sha256=digest.hexdigest(),
                    offset=p["offset"] + count,
                )
            temporary.chmod(0o600)
            self._check_read(job)
            job["progress"] = p["offset"]
            with self.store.db:
                self.store.put("jobs", job)
            if p["offset"] != len(items):
                return
            self._export_hash(temporary, p["bytes_written"], p["sha256"])
            temporary.replace(output)
        self._check_read(job)
        job["status"] = "completed"
        job["result"] = {
            "path": str(output),
            "messages": p["offset"],
            "bytes": p["bytes_written"],
            "sha256": p["sha256"],
            "source": "frozen_evidence",
            "manifest": p["manifest"],
            "incomplete": p["manifest"]["incomplete"],
            "revocation_boundary": p["manifest"]["revocation_boundary"],
        }
        with self.store.db:
            self.store.put("jobs", job)

    @staticmethod
    def _message_weight(operation: dict[str, Any] | None) -> int:
        operation = operation or {}
        if operation.get("kind") == "forward":
            return len(operation["message_ids"])
        if operation.get("kind") == "media_send_album":
            return len(operation["files"])
        return 0 if operation.get("kind") == "media_upload_file" else 1

    async def _deliver(self, job: dict[str, Any]) -> None:
        profile = job["profile_id"]
        p = job["payload"]
        if "operation" in p:
            self._operation_allowed(profile, p["operation"])
        else:
            self._allowed(profile, p["recipients"], p["broadcast"])
        entries = self.store.deliveries(job["id"])
        for position, delivery in enumerate(entries):
            if delivery["status"] != "pending":
                continue
            now = utcnow()
            next_send = self.store.state(f"next_send:{profile}", 0)
            if next_send > now.timestamp():
                return
            daily_key = f"daily:{profile}:{now.date().isoformat()}"
            weight = self._message_weight(p.get("operation"))
            if self.store.state(daily_key, 0) + weight > self.settings.limits.daily_messages:
                job["next_run"] = (
                    (now + timedelta(days=1))
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    .timestamp()
                )
                with self.store.db:
                    self.store.put("jobs", job)
                return
            adapter = await self.adapter(profile)
            file_bytes = None
            validated_bytes: list[bytes] = []
            if "operation" in p:
                async with asyncio.timeout(self.settings.read_timeout_seconds):
                    from .runtime import fingerprint

                    if p["operation"]["kind"].startswith("media_"):
                        if self.media is None:
                            raise TeleloomError(
                                "capability_unavailable", "The media worker is unavailable."
                            )
                        validated_bytes = self.media.validate_sources(
                            profile, p["operation"], p["source_messages"]
                        )
                    elif p["operation"]["kind"].startswith(("group_", "account_")):
                        file_bytes = await self.management.validate(
                            profile,
                            p["operation"],
                            p["source_messages"],
                            delivery.get("receipts"),
                            require_source=False,
                        )
                    elif not delivery.get("receipts") and fingerprint(
                        await self._operation_sources(profile, p["operation"])
                    ) != fingerprint(p["source_messages"]):
                        raise TeleloomError(
                            "source_changed",
                            "A reviewed source was edited or replaced. Create and confirm a new preview.",
                        )
                    if p["operation"]["kind"] == "inline_callback":
                        state = await adapter.message_state(
                            p["operation"]["chat_id"],
                            "buttons",
                            p["operation"]["message_id"],
                            100,
                            "",
                            None,
                        )
                        if p["operation"]["callback"] not in state["items"]:
                            raise TeleloomError(
                                "source_changed", "The confirmed callback button changed."
                            )
                    if p["operation"]["kind"] == "scheduled_delete":
                        state = await adapter.message_state(
                            p["operation"]["chat_id"], "scheduled", None, 100, "", None
                        )
                        selected = {row["id"]: row for row in state["items"]}
                        if [selected.get(id_) for id_ in p["operation"]["message_ids"]] != p[
                            "operation"
                        ]["scheduled_messages"]:
                            raise TeleloomError(
                                "source_changed",
                                "A confirmed scheduled message changed or was already delivered/deleted.",
                            )
            current = self.store.get("jobs", job["id"])
            if not current or current["status"] not in {"queued", "running"}:
                return
            delivery["status"] = "sending"
            delivery["attempted_at"] = now.isoformat()
            with self.store.db:
                self.store.set_state(daily_key, self.store.state(daily_key, 0) + weight)
                self.store.set_state(
                    f"next_send:{profile}", now.timestamp() + self.settings.limits.interval_seconds
                )
                self.store.put_delivery(job["id"], position, delivery)
            try:
                async with asyncio.timeout(self.settings.read_timeout_seconds):
                    if "operation" in p:
                        if p["operation"]["kind"].startswith("media_") and self.media is not None:
                            result = await self.media.perform(
                                profile, p["operation"], delivery, validated_bytes
                            )
                        elif p["operation"]["kind"].startswith(("group_", "account_")):
                            result = await adapter.mutate_administration(
                                p["operation"],
                                delivery["random_id"],
                                delivery.get("receipts", []),
                                file_bytes,
                            )
                        else:
                            result = await adapter.mutate_message(
                                p["operation"], delivery["random_id"]
                            )
                        delivery["receipt"] = result
                        if result.get("continue"):
                            delivery.setdefault("receipts", []).append(result)
                        receipt = (result.get("message_ids") or [None])[0]
                    else:
                        receipt = await adapter.send(
                            delivery["chat_id"],
                            p["text"],
                            p["reply_to_message_id"],
                            delivery["random_id"],
                        )
                current = self.store.get("jobs", job["id"]) or current
                delivery["status"] = "sent"
                delivery["message_id"] = receipt
                if "operation" in p and result.get("continue"):
                    delivery["status"] = "pending"
                    if len(delivery["receipts"]) >= p["operation"]["max_requests"]:
                        delivery["status"] = "partial"
                        current["status"] = "failed"
                        current["error"] = {
                            "code": "mutation_budget_exhausted",
                            "message": "Telegram acknowledged bounded batches but has remaining work; inspect receipts before confirming a new plan.",
                        }
                elif "operation" in p and result.get("complete") is False:
                    delivery["status"] = "partial"
                    current["status"] = "failed"
                    current["error"] = {
                        "code": "mutation_partial",
                        "message": "Telegram acknowledged only part of the operation. Inspect receipts before confirming a new plan.",
                    }
            except asyncio.CancelledError:
                delivery["status"] = "unknown"
                delivery["error"] = "Send interrupted; inspect Telegram before any retry."
                current["status"] = "needs_review"
                with self.store.db:
                    self.store.put_delivery(job["id"], position, delivery)
                    self.store.put("jobs", current)
                raise
            except Exception as exc:
                error = exc if isinstance(exc, TeleloomError) else telegram_error(exc)
                delivery["error"] = {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
                current = self.store.get("jobs", job["id"]) or current
                if error.retry_after is not None:
                    delivery["status"] = "pending"
                    current["next_run"] = now.timestamp() + error.retry_after
                    with self.store.db:
                        self.store.set_state(
                            f"next_send:{profile}",
                            max(current["next_run"], self.store.state(f"next_send:{profile}")),
                        )
                elif error.code in {
                    "telegram_rejected",
                    "account_restricted",
                    "peer_unavailable",
                    "peer_identity_changed",
                    "stale_contacts",
                    "stale_folder",
                    "management_not_allowed",
                    "read_not_allowed",
                    "coverage_incomplete",
                    "folder_limit",
                    "folder_exists",
                    "folder_not_found",
                    "invalid_folder_order",
                    "shared_folder_restricted",
                    "unsupported_capability",
                }:
                    delivery["status"] = "partial" if delivery.get("receipts") else "failed"
                    current["status"] = (
                        "failed"
                        if current["status"] not in {"paused", "cancelled"}
                        else current["status"]
                    )
                else:
                    delivery["status"] = "unknown"
                    current["status"] = "needs_review"
                current["error"] = delivery["error"]
            current["progress"] = sum(entry["status"] == "sent" for entry in entries)
            with self.store.db:
                self.store.put_delivery(job["id"], position, delivery)
                self.store.put("jobs", current)
            return
        if any(entry["status"] in {"unknown", "sending"} for entry in entries):
            job["status"] = "needs_review"
        elif all(entry["status"] == "sent" for entry in entries):
            job["status"] = "completed"
        else:
            job["status"] = "failed"
        with self.store.db:
            self.store.put("jobs", job)
