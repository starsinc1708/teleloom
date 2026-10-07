import asyncio
import base64
import contextlib
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .adapters import Adapter, make_adapter, telegram_error
from .config import Profile, Settings
from .models import Message, Page, TeleloomError, iso, utcnow
from .secrets import Secrets
from .store import Store

AdapterFactory = Callable[[str, Profile, Store, Secrets], Adapter]


def number(value: str, *, positive: bool = False) -> int:
    if not value.lstrip("-").isdigit() or len(value) > 20:
        raise TeleloomError(
            "invalid_id", "Use a numeric Telegram ID from chat_resolve or messages_get."
        )
    result = int(value)
    if str(result) != value or result == 0 or not -(2**63) <= result < 2**63:
        raise TeleloomError("invalid_id", "Use the canonical nonzero numeric Telegram ID.")
    if positive and result <= 0:
        raise TeleloomError("invalid_id", "Message ID must be positive.")
    return result


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def cursor_encode(scope: Any, position: int) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"scope": fingerprint(scope), "before": position}).encode()
    ).decode()


def cursor_decode(cursor: str | None, scope: Any) -> int | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 512:
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(cursor))
        if (
            data["scope"] != fingerprint(scope)
            or not isinstance(data["before"], int)
            or data["before"] <= 0
        ):
            raise ValueError
        return int(data["before"])
    except (ValueError, KeyError, TypeError):
        raise TeleloomError(
            "invalid_cursor", "Cursor does not belong to this profile, chat or query."
        ) from None


def dates(since: datetime | None, until: datetime | None) -> tuple[str | None, str | None]:
    start, end = iso(since) if since else None, iso(until) if until else None
    if since and until and since >= until:
        raise TeleloomError("invalid_range", "Start must be earlier than end (end is exclusive).")
    return start, end


class Runtime:
    def __init__(self, settings: Settings, adapter_factory: AdapterFactory = make_adapter) -> None:
        self.settings = settings
        self.store = Store(settings.data_dir)
        for name, profile in settings.profiles.items():
            self.store.bind_profile(name, profile.generation)
        self.credentials = Secrets()
        self.factory = adapter_factory
        self.owner_id = uuid.uuid4().hex
        self.adapters: dict[str, Adapter] = {}
        self.cleanups: dict[str, asyncio.Task[None]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.profile_errors: dict[str, str] = {}
        self.worker: asyncio.Task[None] | None = None
        self.startups: list[asyncio.Task[None]] = []
        self.tick_lock = asyncio.Lock()
        from .jobs import Jobs

        self.jobs = Jobs(settings, self.store, self.adapter)
        from .attachments import AttachmentManager

        self.attachments = AttachmentManager(settings, self.store, self.adapter, self.jobs)
        self.jobs.attachments = self.attachments
        from .events import EventManager
        from .transcription import TranscriptionManager

        self.events = EventManager(self.jobs)
        self.transcription = TranscriptionManager(self.jobs, self.attachments, self.credentials)
        self.jobs.events = self.events
        self.jobs.transcription = self.transcription
        from .media import MediaManager

        self.media = MediaManager(self)
        self.jobs.media = self.media
        from .analysis import Analysis

        self.analysis = Analysis(settings, self.store, self.credentials)
        from .contacts import Contacts

        self.contacts = Contacts(self)
        from .folder_operations import FolderOperations

        self.folder_operations = FolderOperations(self)
        from .administration import Administration

        self.administration = Administration(self)
        from .account_operations import Management

        self.management = Management(self)
        self.jobs.management = self.management

    async def start(self) -> None:
        if self.worker is not None:
            return

        async def connect(name: str) -> None:
            try:
                await self.adapter(name)
            except Exception as exc:
                self.profile_errors[name] = (
                    exc.code if isinstance(exc, TeleloomError) else "connection_error"
                )

        for name, profile in self.settings.profiles.items():
            if profile.polling or profile.sync_chats or profile.event_chats:
                self.startups.append(asyncio.create_task(connect(name)))
        self.worker = asyncio.create_task(self._work())

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        for task in self.startups:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for cleanup in self.cleanups.values():
            with contextlib.suppress(Exception):
                await asyncio.shield(cleanup)
        for adapter in self.adapters.values():
            with contextlib.suppress(Exception):
                await adapter.close()
        await self.attachments.close()
        self.store.close()

    async def _work(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(0.25)

    async def tick(self) -> None:
        await self.jobs.tick()

    async def adapter(self, profile_id: str) -> Adapter:
        profile = self.settings.profile(profile_id)
        async with self.locks.setdefault(profile_id, asyncio.Lock()):
            cleanup = self.cleanups.get(profile_id)
            if cleanup is not None:
                if not cleanup.done():
                    raise TeleloomError(
                        "profile_closing",
                        "The previous connection is still closing; retry after it finishes.",
                        retry_after=1,
                    )
                if cleanup.cancelled() or cleanup.exception() is not None:
                    raise TeleloomError(
                        "cleanup_failed", "Connection cleanup failed; stop and restart the daemon."
                    )
                del self.cleanups[profile_id]
            if profile_id not in self.adapters:
                adapter = None
                try:
                    adapter = self.factory(profile_id, profile, self.store, self.credentials)
                    await asyncio.wait_for(adapter.start(), timeout=30)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        self.profile_errors[profile_id] = (
                            exc.code if isinstance(exc, TeleloomError) else telegram_error(exc).code
                        )
                    if adapter is not None:
                        # Teardown may shield its own network tasks. Keep ownership until
                        # it finishes rather than cancelling it and opening another session.
                        self.cleanups[profile_id] = asyncio.create_task(adapter.close())
                    if isinstance(exc, TimeoutError):
                        self.profile_errors[profile_id] = "connection_timeout"
                        raise TeleloomError(
                            "connection_timeout",
                            "Connecting to Telegram timed out; retry after connection cleanup.",
                            retry_after=1,
                        ) from None
                    raise
                self.adapters[profile_id] = adapter
                self.profile_errors.pop(profile_id, None)
        return self.adapters[profile_id]

    async def invoke(self, operation: Callable[[], Any]) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await operation()
            if not result.pop("_frozen", False):
                for profile_id in self.settings.profiles:
                    self.transcription.enrich(profile_id, result)
            return result
        except TeleloomError:
            raise
        except Exception as exc:
            raise telegram_error(exc) from None

    def profiles(self) -> dict[str, Any]:
        from .account_operations import capabilities as management_capabilities
        from .diagnostics import failure

        return {
            "tool_exposure": {
                "mode": self.settings.exposure_mode,
                "selected_tools": sorted(self.settings.exposed_tools)
                if self.settings.exposure_mode == "selected"
                else [],
            },
            "profiles": [
                {
                    "id": name,
                    "kind": p.kind,
                    "backend": "mtproto" if p.kind == "user" else p.bot_backend,
                    "identity": {
                        k: v for k, v in p.identity.items() if k in {"id", "username", "name"}
                    },
                    "connected": name in self.adapters,
                    "error": failure(self.profile_errors[name], "connect", p)["code"]
                    if name in self.profile_errors
                    else None,
                    "grants": p.grants(),
                    "capabilities": {
                        "read_policy": {"mode": p.read_mode, "chat_ids": p.read_chats},
                        "telegram_history": p.kind == "user",
                        "explicit_message_lookup": "telegram"
                        if p.kind == "user" or p.bot_backend == "mtproto"
                        else "saved_updates_only",
                        "telegram_global_search": p.kind == "user",
                        "exact_sender_search": "bounded_telegram_scan"
                        if p.kind == "user"
                        else "saved_updates_only",
                        "local_retrieval": "selected_chat_fts",
                        "public_chat_search": p.kind == "user",
                        "message_context": "telegram" if p.kind == "user" else "saved_updates_only",
                        "rich_evidence": True,
                        "telegram_unread": p.kind == "user",
                        "local_inbox": p.kind == "bot",
                        "polling_enabled": p.polling,
                        "folder_membership": p.kind == "user",
                        "topics": "telegram" if p.kind == "user" else "saved_updates_only",
                        "threads": "telegram" if p.kind == "user" else "saved_updates_only",
                        "channel_comments": p.kind == "user",
                        "pinned_posts": "telegram" if p.kind == "user" else "saved_updates_only",
                        "selected_attachments": True,
                        "attachment_processing": "local_optional_engines",
                        "events": bool(p.event_chats) and (p.kind == "user" or p.polling),
                        "event_delivery": "local_pull",
                        "transcription": bool(p.transcription_chats),
                        "transcription_providers": ["local", "openai", "groq"]
                        + (["telegram"] if p.kind == "user" else []),
                        "transcription_engines": self.transcription.capabilities(
                            name, include_credential_status=False
                        ),
                        "media": {
                            "bare_upload": p.kind == "user" or p.bot_backend == "mtproto",
                            "gif_search": p.kind == "user",
                            "inline_images": True,
                            "photo_sheets": True,
                            "file_roots_configured": bool(p.file_roots),
                            "confirmed_uploads": True,
                            "scheduled_send": p.kind == "user",
                            "sticker_sets": p.kind == "user",
                            "photos": "native_and_saved_updates"
                            if p.kind == "user" or p.bot_backend == "mtproto"
                            else "current_group_avatar_and_user_photos_and_saved_updates",
                            "premium": "server_enforced; inspect account_read(kind=me)",
                        },
                        "send": bool(p.send_chats),
                        "broadcast": bool(p.broadcast_chats),
                        "management": management_capabilities(p),
                    },
                    "polling": self.store.state(f"polling:{name}"),
                }
                for name, p in self.settings.profiles.items()
            ],
        }

    async def drafts(self, profile_id: str, limit: int, cursor: str | None) -> dict[str, Any]:
        from .rich_reads import scoped_evidence

        profile = self.settings.profile(profile_id)
        if profile.kind != "user":
            raise TeleloomError(
                "unsupported_capability",
                "Account drafts are not provided by the current aiogram Bot API backend.",
            )
        snapshot = None
        if not cursor:
            items = (
                await (await self.adapter(profile_id)).drafts()
                if profile.read_mode == "all" or profile.read_chats
                else []
            )
            items = [item for item in items if profile.allows_read(item["chat_id"])]
            snapshot = {
                "items": scoped_evidence(profile, profile_id, {"items": items})["items"],
                "snapshot_at": utcnow().isoformat(),
                "source": "telegram_drafts",
                "incomplete": False,
            }
        return self.snapshot_page(profile_id, ["drafts"], cursor, limit, snapshot)

    async def folders(self, profile_id: str) -> dict[str, Any]:
        if self.settings.profile(profile_id).kind != "user":
            raise TeleloomError(
                "unsupported_capability", "Telegram folders require a user profile."
            )
        profile = self.settings.profile(profile_id)
        folders = await (await self.adapter(profile_id)).folders()
        withheld = 0
        if profile.read_mode == "selected":
            withheld = sum(
                not profile.allows_read(peer)
                for folder in folders
                for key in ("included_chat_ids", "pinned_chat_ids", "excluded_chat_ids")
                for peer in folder.get(key, [])
            )
            folders = [
                {
                    **folder,
                    **{
                        key: [peer for peer in folder.get(key, []) if profile.allows_read(peer)]
                        for key in ("included_chat_ids", "pinned_chat_ids", "excluded_chat_ids")
                    },
                }
                for folder in folders
            ]
        return {
            "items": folders,
            "source": "telegram",
            "incomplete": bool(withheld),
            "coverage": {"snapshot_at": utcnow().isoformat(), "withheld_peers": withheld},
            "warnings": ["Folder peer metadata outside the owner's read scope was withheld."]
            if withheld
            else [],
        }

    def snapshot_page(
        self,
        profile_id: str,
        scope: Any,
        cursor: str | None,
        limit: int,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = self.settings.profile(profile_id)
        scope = [*scope, profile.read_policy()]
        if cursor:
            snapshot_id, sep, position = cursor.partition(".")
            stored = self.store.state(f"reading_snapshot:{snapshot_id}")
            if (
                not sep
                or not stored
                or stored["profile_id"] != profile_id
                or stored["generation"] != profile.generation
                or stored["scope"] != scope
                or stored["expires_at"] <= utcnow().timestamp()
            ):
                raise TeleloomError(
                    "invalid_cursor", "Snapshot expired or belongs to another account or query."
                )
            snapshot = stored
            offset = (
                cursor_decode(position, [profile_id, profile.generation, scope, snapshot_id]) or 1
            ) - 1
        else:
            assert snapshot is not None
            snapshot_id = uuid.uuid4().hex
            snapshot = {
                **snapshot,
                "profile_id": profile_id,
                "generation": profile.generation,
                "scope": scope,
                "expires_at": utcnow().timestamp() + 900,
            }
            offset = 0
            with self.store.db:
                self.store.db.execute(
                    "DELETE FROM state WHERE key LIKE 'reading_snapshot:%' AND json_extract(data,'$.expires_at')<=?",
                    (utcnow().timestamp(),),
                )
                self.store.set_state(f"reading_snapshot:{snapshot_id}", snapshot)
        items = snapshot["items"]
        return {
            **{k: v for k, v in snapshot.items() if k not in {"scope", "expires_at", "generation"}},
            "snapshot_id": snapshot_id,
            "items": items[offset : offset + limit],
            "incomplete": bool(snapshot.get("incomplete")) or offset + limit < len(items),
            "next_cursor": snapshot_id
            + "."
            + cursor_encode(
                [profile_id, profile.generation, scope, snapshot_id], offset + limit + 1
            )
            if offset + limit < len(items)
            else None,
            "coverage": {
                "observed_topics" if scope[0] == "topics" else "observed_chats": len(items),
                "snapshot_at": snapshot["snapshot_at"],
            },
        }

    async def selection(
        self,
        profile_id: str,
        folder_id: str | None,
        chat_ids: list[str] | None,
        kind: str | None = None,
        *,
        max_chats: int | None = 200,
    ) -> dict[str, Any]:
        from .folders import membership

        profile = self.settings.profile(profile_id)
        if (folder_id is None) == (chat_ids is None):
            raise TeleloomError(
                "invalid_selection", "Provide either folder_id or a nonempty chat_ids list."
            )
        unavailable: list[dict[str, Any]] = []
        if folder_id is not None:
            number(folder_id, positive=True)
            if profile.kind != "user":
                raise TeleloomError(
                    "unsupported_capability", "Telegram folders require a user profile."
                )
            adapter = await self.adapter(profile_id)
            folders = await adapter.folders()
            folder = next((f for f in folders if f["id"] == folder_id), None)
            if folder is None:
                raise TeleloomError("folder_not_found", "Folder ID not found for this profile.")
            members, unavailable = membership(folder, await adapter.chats())
        else:
            if not chat_ids or len(chat_ids) > 200 or len(set(chat_ids)) != len(chat_ids):
                raise TeleloomError("invalid_selection", "Provide 1–200 unique canonical chat IDs.")
            for id_ in chat_ids:
                number(id_)
                profile.require_read(id_)
            adapter = await self.adapter(profile_id)
            members = []
            for id_ in chat_ids:
                try:
                    chat = await adapter.resolve(id_)
                    if chat.id != id_:
                        raise TeleloomError(
                            "peer_identity_changed", "Resolved chat identity changed."
                        )
                    members.append(chat)
                except Exception as exc:
                    error = exc if isinstance(exc, TeleloomError) else telegram_error(exc)
                    if error.retry_after is not None:
                        raise error from None
                    unavailable.append(
                        {"chat_id": id_, "error": {"code": error.code, "message": error.message}}
                    )
        members = [chat for chat in members if profile.allows_read(chat.id)]
        unavailable = [item for item in unavailable if profile.allows_read(item["chat_id"])]
        if kind:
            members = [
                c for c in members if c.kind == kind or kind == "group" and c.kind == "supergroup"
            ]
        if max_chats is not None and len(members) > max_chats:
            raise TeleloomError(
                "selection_limit", "Select at most 200 chats; use a narrower folder or chat_ids."
            )
        return {
            "profile_id": profile_id,
            "generation": profile.generation,
            "folder_id": folder_id,
            "items": [c.model_dump(mode="json") for c in members],
            "unavailable": unavailable,
            "snapshot_at": utcnow().isoformat(),
            "source": "telegram" if profile.kind == "user" else "bot_updates",
            "incomplete": bool(unavailable) or profile.kind == "bot",
        }

    async def folder_members(
        self,
        profile_id: str,
        folder_id: str,
        cursor: str | None,
        limit: int,
        kind: str | None = None,
    ) -> dict[str, Any]:
        scope = ["folder", folder_id, kind]
        snapshot = (
            None
            if cursor
            else await self.selection(profile_id, folder_id, None, kind, max_chats=None)
        )
        return self.snapshot_page(profile_id, scope, cursor, limit, snapshot)

    async def chat_list(
        self,
        profile_id: str,
        cursor: str | None,
        limit: int,
        folder_id: str | None = None,
        kind: str | None = None,
        *,
        unread_only: bool = False,
        unmuted_only: bool = False,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        filtered = unread_only or unmuted_only or archived is not None
        if folder_id is not None and not filtered:
            return await self.folder_members(profile_id, folder_id, cursor, limit, kind)
        if kind is not None or filtered:
            snapshot: dict[str, Any] | None = None
            if not cursor:
                profile = self.settings.profile(profile_id)
                if folder_id is not None:
                    snapshot = await self.selection(
                        profile_id, folder_id, None, kind, max_chats=None
                    )
                else:
                    chats = await (await self.adapter(profile_id)).chats()
                    snapshot = {
                        "items": [
                            c.model_dump(mode="json")
                            for c in chats
                            if profile.allows_read(c.id)
                            and (
                                not kind
                                or c.kind == kind
                                or kind == "group"
                                and c.kind == "supergroup"
                            )
                        ],
                        "snapshot_at": utcnow().isoformat(),
                        "source": "telegram" if profile.kind == "user" else "bot_updates",
                        "incomplete": profile.kind == "bot",
                    }
                snapshot["items"] = [
                    item
                    for item in snapshot["items"]
                    if (
                        not unread_only
                        or (
                            item["unread_count"] > 0
                            if profile.kind == "user"
                            else bool(
                                self.store.messages(
                                    profile_id,
                                    item["id"],
                                    limit=1,
                                    incoming_only=True,
                                    unprocessed_only=True,
                                )
                            )
                        )
                    )
                    and (not unmuted_only or item["muted"] is False)
                    and (archived is None or item["archived"] is archived)
                ]
            return self.snapshot_page(
                profile_id,
                ["chats", folder_id, kind, unread_only, unmuted_only, archived],
                cursor,
                limit,
                snapshot,
            )
        profile = self.settings.profile(profile_id)
        if cursor:
            snapshot_id, separator, position = cursor.partition(".")
            snapshot = self.store.state(f"chats_snapshot:{snapshot_id}")
            if (
                not separator
                or not snapshot
                or snapshot["profile"] != profile_id
                or snapshot["generation"] != profile.generation
                or snapshot.get("read_policy", ["all", []]) != profile.read_policy()
                or snapshot["expires_at"] <= utcnow().timestamp()
            ):
                raise TeleloomError(
                    "invalid_cursor",
                    "Dialog snapshot expired or belongs to another account. Start from the first page.",
                )
            offset = (
                cursor_decode(position, [profile_id, profile.generation, "chats", snapshot_id]) or 1
            ) - 1
        else:
            snapshot_id = uuid.uuid4().hex
            chats = await (await self.adapter(profile_id)).chats()
            snapshot = {
                "profile": profile_id,
                "generation": profile.generation,
                "read_policy": profile.read_policy(),
                "items": [c.model_dump() for c in chats if profile.allows_read(c.id)],
                "snapshot_at": utcnow().isoformat(),
                "expires_at": utcnow().timestamp() + 900,
            }
            offset = 0
            with self.store.db:
                self.store.db.execute(
                    "DELETE FROM state WHERE key LIKE 'chats_snapshot:%' AND json_extract(data,'$.expires_at')<=?",
                    (utcnow().timestamp(),),
                )
                self.store.set_state(f"chats_snapshot:{snapshot_id}", snapshot)
        items = snapshot["items"]
        return {
            "items": items[offset : offset + limit],
            "next_cursor": snapshot_id
            + "."
            + cursor_encode(
                [profile_id, profile.generation, "chats", snapshot_id], offset + limit + 1
            )
            if len(items) > offset + limit
            else None,
            "source": "telegram"
            if self.settings.profile(profile_id).kind == "user"
            else "bot_updates",
            "incomplete": self.settings.profile(profile_id).kind == "bot",
            "coverage": {"observed_chats": len(items), "snapshot_at": snapshot["snapshot_at"]},
        }

    async def resolve(self, profile_id: str, target: str) -> dict[str, Any]:
        target = self.contacts.exact_alias(profile_id, target) or target
        profile = self.settings.profile(profile_id)
        if target.lstrip("-").isdigit():
            profile.require_read(target)
        if profile.read_mode == "selected" and not target.lstrip("-").isdigit():
            candidates = [
                chat
                for chat in await (await self.adapter(profile_id)).chats()
                if profile.allows_read(chat.id)
                and (chat.title == target or chat.username and "@" + chat.username == target)
            ]
            if len(candidates) != 1:
                raise TeleloomError(
                    "read_not_allowed",
                    "Resolve an exact allowed identity or configure it using the owner CLI.",
                )
            return candidates[0].model_dump()
        result = await (await self.adapter(profile_id)).resolve(target)
        profile.require_read(result.id)
        return result.model_dump()

    async def chat_search(
        self, profile_id: str, query: str, scope: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        profile = self.settings.profile(profile_id)
        if not query.strip() or len(query) > 1024:
            raise TeleloomError(
                "invalid_query",
                "Provide 1–1024 query characters including non-whitespace text.",
            )
        if scope == "public" and profile.kind != "user":
            raise TeleloomError(
                "unsupported_capability", "Telegram public peer search requires a user profile."
            )
        policy: list[Any] = profile.read_policy()
        snapshot = None
        if not cursor:
            adapter = await self.adapter(profile_id)
            if scope == "public" and policy[0] == "all":
                chats = await adapter.public_chats(query, 100)
            elif scope == "public":
                chats = [await adapter.resolve(chat) for chat in policy[1]]
            else:
                chats = await adapter.chats()
            matches = [
                chat
                for chat in chats
                if (policy[0] == "all" or chat.id in policy[1])
                and (
                    scope == "public"
                    and policy[0] == "all"
                    or query.casefold() in chat.title.casefold()
                    or query.lstrip("@").casefold() in (chat.username or "").casefold()
                    or query == chat.id
                )
            ]
            snapshot = {
                "items": [chat.model_dump(mode="json") for chat in matches],
                "snapshot_at": iso(utcnow()),
                "source": "bot_updates" if profile.kind == "bot" else "telegram",
                "incomplete": profile.kind == "bot" or scope == "public" and len(chats) >= 100,
                "search_scope": scope,
                "warnings": [
                    "Public search exposes at most 100 Telegram candidates; narrow the query if its cap is reached."
                ]
                if scope == "public"
                else [],
            }
        return self.snapshot_page(
            profile_id, ["chat_search", query, scope, policy], cursor, limit, snapshot
        )

    async def message_context(
        self,
        profile_id: str,
        chat_id: str,
        message_id: str,
        size: int,
        include_replies: bool,
        max_reply_chats: int,
    ) -> dict[str, Any]:
        profile = self.settings.profile(profile_id)
        number(chat_id)
        target = number(message_id, positive=True)
        selected = profile.read_mode == "selected"
        allowed = profile.read_chats
        profile.require_read(chat_id)
        adapter = await self.adapter(profile_id)
        window = await adapter.context_window(chat_id, target, size)
        rows = window["items"]
        shown: list[Message] = []
        characters = 0
        for row in rows:
            if shown and characters + len(row.text) > 32000:
                break
            shown.append(row)
            characters += len(row.text)
        warnings = []
        incomplete = len(shown) < len(rows) or profile.kind == "bot"
        replies = []
        missing = []
        if include_replies:
            references: dict[str, dict[int, list[str]]] = {}
            for row in shown:
                if row.reply_to_message_id:
                    if row.reply_external and not row.reply_to_chat_id:
                        incomplete = True
                        warnings.append(
                            "The external reply's source chat is unavailable; its content was not fetched."
                        )
                        continue
                    peer = row.reply_to_chat_id or row.chat_id
                    if selected and peer not in allowed:
                        incomplete = True
                        warnings.append(
                            "A reply target is outside the allowed read scope; its content was not fetched."
                        )
                        continue
                    references.setdefault(peer, {}).setdefault(
                        int(row.reply_to_message_id), []
                    ).append(row.id)
            for index, (peer, ids) in enumerate(references.items()):
                if index >= max_reply_chats:
                    incomplete = True
                    warnings.append("Reply context reached the explicit chat budget.")
                    break
                try:
                    fetched = await adapter.history(
                        peer, before=None, since=None, until=None, limit=len(ids), ids=list(ids)
                    )
                except Exception as exc:
                    error = exc if isinstance(exc, TeleloomError) else telegram_error(exc)
                    missing.append(
                        {"chat_id": peer, "message_ids": list(map(str, ids)), "error": error.code}
                    )
                    incomplete = True
                    continue
                for row in fetched:
                    if characters + len(row.text) > 32000:
                        incomplete = True
                        warnings.append("Reply context reached the response text budget.")
                        break
                    characters += len(row.text)
                    replies.append(
                        {**row.model_dump(mode="json"), "referenced_by": ids[int(row.id)]}
                    )
                missing_ids = [
                    str(id_) for id_ in ids if str(id_) not in {row.id for row in fetched}
                ]
                if missing_ids:
                    missing.append(
                        {
                            "chat_id": peer,
                            "message_ids": missing_ids,
                            "error": "deleted_or_unavailable",
                        }
                    )
                    incomplete = True
        result = Page(
            items=sorted(shown, key=lambda row: int(row.id)),
            source="bot_updates" if profile.kind == "bot" else "telegram",
            incomplete=incomplete,
            warnings=list(dict.fromkeys(warnings)),
            coverage={
                "central_message_id": message_id,
                "context_size": size,
                "returned": len(shown),
                "has_older": window["has_older"],
                "has_newer": window["has_newer"],
                "missing_replies": missing,
            },
        ).model_dump(mode="json")
        result["items"] = [{**row, "is_target": row["id"] == message_id} for row in result["items"]]
        result["reply_context"] = replies
        return result

    async def topics(
        self,
        profile_id: str,
        chat_id: str,
        cursor: str | None,
        limit: int,
        query: str | None = None,
    ) -> dict[str, Any]:
        number(chat_id)
        profile = self.settings.profile(profile_id)
        profile.require_read(chat_id)
        if query is not None and not query.strip():
            raise TeleloomError("invalid_query", "Topic title search needs nonempty text.")
        scope = ["topics", chat_id, query]
        snapshot = None
        if not cursor:
            adapter = await self.adapter(profile_id)
            items: list[dict[str, Any]] = []
            offset = None
            for _ in range(10):
                page = await adapter.topics(chat_id, offset=offset, limit=100)
                items.extend(page["items"])
                following = page.get("next_offset")
                if not following or following == offset:
                    offset = following
                    break
                offset = following
            unique = {item["id"]: item for item in items}
            snapshot = {
                "items": [
                    item
                    for item in unique.values()
                    if query is None or query.casefold() in (item.get("title") or "").casefold()
                ],
                "snapshot_at": utcnow().isoformat(),
                "source": "telegram" if profile.kind == "user" else "bot_updates",
                "incomplete": bool(offset) or profile.kind == "bot",
                "warnings": ["Saved bot updates only; topic discovery is incomplete."]
                if profile.kind == "bot"
                else ["Topic discovery stopped at the 1000-topic budget."]
                if offset
                else [],
            }
        return self.snapshot_page(profile_id, scope, cursor, limit, snapshot)

    async def thread_page(
        self,
        profile_id: str,
        chat_id: str,
        identifier: str | None,
        mode: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        number(chat_id)
        root = number(identifier, positive=True) if identifier else None
        profile = self.settings.profile(profile_id)
        profile.require_read(chat_id)
        adapter = await self.adapter(profile_id)
        scope: list[Any] = [
            profile_id,
            profile.generation,
            chat_id,
            identifier,
            mode,
            profile.read_policy(),
        ]
        mapping = None
        mapping_id = None
        message_cursor = cursor
        if mode == "comments":
            if cursor:
                mapping_id, sep, message_cursor = cursor.partition(".")
                saved = self.store.state(f"discussion_snapshot:{mapping_id}")
                if (
                    not sep
                    or not saved
                    or saved["scope"] != scope
                    or saved["expires_at"] <= utcnow().timestamp()
                ):
                    raise TeleloomError(
                        "invalid_cursor", "Discussion cursor expired or belongs to another scope."
                    )
                mapping = saved["mapping"]
            else:
                assert root is not None
                mapping = await adapter.discussion(chat_id, root)
                if mapping.get("chat_id"):
                    profile.require_read(mapping["chat_id"])
                mapping_id = uuid.uuid4().hex
                with self.store.db:
                    self.store.db.execute(
                        "DELETE FROM state WHERE key LIKE 'discussion_snapshot:%' AND json_extract(data,'$.expires_at')<=?",
                        (utcnow().timestamp(),),
                    )
                    self.store.set_state(
                        f"discussion_snapshot:{mapping_id}",
                        {
                            "scope": scope,
                            "mapping": mapping,
                            "expires_at": utcnow().timestamp() + 900,
                        },
                    )
            scope = [*scope, mapping]
        before = cursor_decode(message_cursor, scope)
        read_chat = mapping.get("chat_id") if mapping else chat_id
        if read_chat:
            profile.require_read(read_chat)
        read_root = mapping.get("root_id") if mapping else identifier
        batch: dict[str, Any] | None = None
        if mode == "pinned":
            read_batch = getattr(adapter, "pinned_batch", None)
            if read_batch is not None:
                batch = await read_batch(chat_id, before=before, limit=limit + 1)
                rows = batch["items"]
            else:
                rows = await adapter.pinned(chat_id, before=before, limit=limit + 1)
        elif read_chat and read_root:
            read_batch = getattr(adapter, "thread_batch", None)
            if read_batch is not None:
                batch = await read_batch(read_chat, int(read_root), before=before, limit=limit + 1)
                rows = batch["items"]
            else:
                rows = await adapter.thread(
                    read_chat, int(read_root), before=before, limit=limit + 1
                )
        else:
            rows = []
        rows = sorted(rows, key=lambda m: int(m.id), reverse=True)
        selected: list[Message] = []
        characters = 0
        for row in rows[:limit]:
            if selected and characters + len(row.text) > 32000:
                break
            selected.append(row)
            characters += len(row.text)
        remaining = len(rows) > len(selected)
        more = remaining or bool(batch and not batch["complete"])
        position: int | None
        if remaining and selected:
            position = int(selected[-1].id)
        elif batch:
            position = batch.get("next_before")
        else:
            position = None
        next_cursor = None
        if more:
            if (
                not isinstance(position, int)
                or position <= 0
                or before is not None
                and position >= before
            ):
                raise TeleloomError(
                    "pagination_stalled", "The reply or pinned-post cursor did not advance."
                )
            next_cursor = cursor_encode(scope, position)
        if mapping_id and next_cursor:
            next_cursor = mapping_id + "." + next_cursor
        local = profile.kind == "bot"
        result = Page(
            items=selected,
            next_cursor=next_cursor,
            source="bot_updates" if local else "telegram",
            incomplete=more or local,
            coverage={
                "chat_id": read_chat,
                "root_message_id": read_root,
                "returned": len(selected),
                "mode": mode,
            },
            warnings=[
                "Only saved bot updates are available; missing replies and deletions are unknown."
            ]
            if local
            else [],
        ).model_dump(mode="json")
        if mapping:
            result["discussion"] = mapping
        elif root is not None:
            try:
                original = (
                    self.store.messages(profile_id, chat_id, ids=[root], limit=1)
                    if local
                    else await adapter.history(
                        chat_id, before=None, since=None, until=None, limit=1, ids=[root]
                    )
                )
            except TeleloomError as exc:
                if exc.code not in {"message_unavailable", "topic_unavailable", "peer_unavailable"}:
                    raise
                result["original_status"] = exc.details.get("original_status", "unavailable")
                result["incomplete"] = True
                result["warnings"].append(
                    "The original is unavailable; fetched replies are retained."
                )
            else:
                result["original_status"] = "available" if original else "deleted_or_unavailable"
        return result

    async def history(
        self,
        profile_id: str,
        chat_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
        query: str | None = None,
        source: str = "live",
        message_ids: list[str] | None = None,
        sender_id: str | None = None,
        max_requests: int = 3,
    ) -> dict[str, Any]:
        profile = self.settings.profile(profile_id)
        number(chat_id)
        profile.require_read(chat_id)
        if sender_id is not None:
            number(sender_id)
            return await self.sender_search(
                profile_id,
                chat_id,
                sender_id,
                query,
                since,
                until,
                cursor,
                limit,
                source,
                max_requests,
            )
        if query is not None and not query.strip():
            raise TeleloomError("invalid_query", "Search query must contain non-whitespace text.")
        start, end = dates(since, until)
        ids = [number(id_, positive=True) for id_ in message_ids or []]
        if len(ids) > 100:
            raise TeleloomError("invalid_limit", "At most 100 explicit message IDs are supported.")
        scope = [
            profile_id,
            profile.generation,
            chat_id,
            start,
            end,
            query,
            source,
            ids,
            profile.read_policy(),
        ]
        before = cursor_decode(cursor, scope)
        local = source == "index" or (
            profile.kind == "bot" and not (profile.bot_backend == "mtproto" and ids)
        )
        if local:
            rows = self.store.messages(
                profile_id,
                chat_id,
                before=before,
                since=start,
                until=end,
                query=query,
                limit=limit + 1,
                ids=ids or None,
            )
        else:
            rows = await (await self.adapter(profile_id)).history(
                chat_id,
                before=before,
                since=since,
                until=until,
                query=query,
                limit=limit + 1,
                ids=ids or None,
            )
        if ids:
            rows.sort(key=lambda row: int(row.id), reverse=True)
        selected: list[Message] = []
        characters = 0
        for row in rows[:limit]:
            if selected and characters + len(row.text) > 32000:
                break
            selected.append(row)
            characters += len(row.text)
        more = not ids and len(rows) > len(selected)
        warnings = []
        truncated = len(selected) < len(rows) and bool(ids)
        if truncated:
            warnings.append("Explicit IDs exceeded the response budget; request fewer IDs.")
        coverage: dict[str, Any] = {
            "requested_since": start,
            "requested_until": end,
            "returned": len(selected),
            "ordering": "message_id_descending",
        }
        if ids:
            coverage["missing_message_ids"] = [
                str(id_) for id_ in ids if str(id_) not in {m.id for m in selected}
            ]
        if local:
            coverage["sync"] = self.store.state(f"sync:{profile_id}:{chat_id}")
            warnings.append(
                "Local snapshot; messages not collected or later deleted while offline may be missing/stale."
            )
            if gap := self.store.state(f"gap:{profile_id}"):
                warnings.append(gap)
        return Page(
            items=selected,
            next_cursor=cursor_encode(scope, int(selected[-1].id)) if more and selected else None,
            source="bot_updates"
            if profile.kind == "bot" and local
            else "local_index"
            if local
            else "telegram",
            coverage=coverage,
            incomplete=more or local or truncated,
            warnings=warnings,
        ).model_dump(mode="json")

    async def sender_search(
        self,
        profile_id: str,
        chat_id: str,
        sender_id: str,
        query: str | None,
        since: datetime | None,
        until: datetime | None,
        cursor: str | None,
        limit: int,
        source: str,
        max_requests: int,
    ) -> dict[str, Any]:
        profile = self.settings.profile(profile_id)
        if query and (not query.strip() or len(query) > 1024):
            raise TeleloomError("invalid_query", "Provide at most 1024 query characters.")
        start, end = dates(since, until)
        local = source == "index" or profile.kind == "bot"
        scope = [
            profile_id,
            profile.generation,
            profile.read_policy(),
            chat_id,
            sender_id,
            query,
            start,
            end,
            source,
            limit,
            max_requests,
        ]
        # ponytail: per-profile search lock; split per cursor if contention matters.
        async with self.locks.setdefault("sender:" + profile_id, asyncio.Lock()):
            saved = self.store.state(f"sender_search_cursor:{cursor}") if cursor else None
            if cursor and (
                not saved or saved["scope"] != scope or saved["expires_at"] <= utcnow().timestamp()
            ):
                raise TeleloomError(
                    "invalid_cursor",
                    "Sender cursor expired or belongs to another query, account, policy or budget.",
                )
            if saved and "result" in saved:
                return saved["result"]
            state = saved or {
                "scope": scope,
                "items": [],
                "before": None,
                "complete": False,
                "until": end or iso(utcnow()),
                "unknown_sender": 0,
                "expires_at": utcnow().timestamp() + 900,
            }
            rows = [Message.model_validate(item) for item in state["items"]]
            before, complete = state["before"], state["complete"]
            requests = unknown = 0
            partial_error = None
            warnings = (
                ["Local snapshot; uncollected or offline-deleted messages may be missing/stale."]
                if local
                else []
            )
            while len(rows) <= limit and not complete and requests < max_requests:
                requests += 1
                batch: dict[str, Any]
                try:
                    if local:
                        candidates = self.store.messages(
                            profile_id,
                            chat_id,
                            before=before,
                            since=start,
                            until=state["until"],
                            query=query or None,
                            limit=101,
                        )
                        complete = len(candidates) <= 100
                        candidates = candidates[:100]
                        batch = {
                            "items": candidates,
                            "complete": complete,
                            "next_before": int(candidates[-1].id) if candidates else None,
                        }
                    else:
                        adapter = await self.adapter(profile_id)
                        batch = await adapter.history_batch(
                            chat_id,
                            before=before,
                            since=since,
                            until=datetime.fromisoformat(state["until"]),
                            query=query or None,
                        )
                    complete, before = batch["complete"], batch["next_before"]
                    for row in batch["items"]:
                        if row.sender_id is None:
                            unknown += 1
                        elif row.sender_id == sender_id:
                            rows.append(row)
                except Exception as exc:
                    error = exc if isinstance(exc, TeleloomError) else telegram_error(exc)
                    if not rows and requests == 1:
                        raise error from None
                    partial_error = {
                        "code": error.code,
                        "message": error.message,
                        "retry_after": error.retry_after,
                    }
                    warnings.append(
                        "Scan interrupted; continue explicitly after the reported error."
                    )
                    break
            selected: list[Message] = []
            characters = 0
            for row in rows[:limit]:
                if selected and characters + len(row.text) > 32000:
                    break
                selected.append(row)
                characters += len(row.text)
            pending = rows[len(selected) :]
            more = bool(pending) or not complete
            next_cursor = uuid.uuid4().hex if more else None
            unknown_total = state["unknown_sender"] + unknown
            if unknown_total:
                warnings.append(
                    "Messages with unavailable sender identity were skipped; absence is not proven."
                )
            if profile.kind == "bot":
                warnings.append(
                    "Bot search covers saved observations only, including MTProto bots."
                )
            result = Page(
                items=selected,
                next_cursor=next_cursor,
                source="bot_updates"
                if profile.kind == "bot"
                else "local_index"
                if local
                else "telegram",
                incomplete=more or local or bool(unknown_total) or partial_error is not None,
                warnings=warnings,
                coverage={
                    "sender_id": sender_id,
                    "sender_filter": "exact_id",
                    "requested_since": start,
                    "requested_until": end,
                    "effective_until": state["until"],
                    "returned": len(selected),
                    "ordering": "message_id_descending",
                    "scan_complete": complete,
                    "unknown_sender": unknown_total,
                    "requests": requests,
                    "max_requests": max_requests,
                    "partial_error": partial_error,
                    **(
                        {
                            "sync": self.store.state(f"sync:{profile_id}:{chat_id}"),
                            "gap": self.store.state(f"gap:{profile_id}"),
                        }
                        if local
                        else {}
                    ),
                },
            ).model_dump(mode="json")
            with self.store.db:
                self.store.db.execute(
                    "DELETE FROM state WHERE key LIKE 'sender_search_cursor:%' AND json_extract(data,'$.expires_at')<=?",
                    (utcnow().timestamp(),),
                )
                if next_cursor:
                    self.store.set_state(
                        f"sender_search_cursor:{next_cursor}",
                        {
                            **state,
                            "items": [row.model_dump(mode="json") for row in pending],
                            "before": before,
                            "complete": complete,
                            "unknown_sender": unknown_total,
                        },
                    )
                if cursor:
                    self.store.set_state(
                        f"sender_search_cursor:{cursor}", {**state, "result": result}
                    )
            return result

    async def global_search(
        self,
        profile_id: str,
        query: str,
        *,
        since: datetime | None,
        until: datetime | None,
        cursor: str | None,
        limit: int,
        max_requests: int,
        kind: str | None,
        sender_id: str | None = None,
    ) -> dict[str, Any]:
        if sender_id is not None:
            raise TeleloomError(
                "unsupported_capability",
                "Exact sender search requires one explicit chat via messages_search.",
            )
        profile = self.settings.profile(profile_id)
        if profile.kind != "user":
            raise TeleloomError(
                "unsupported_capability",
                "Telegram global search requires a user profile; use saved bot updates per chat.",
            )
        if not query.strip() or len(query) > 1024:
            raise TeleloomError(
                "invalid_query",
                "Provide 1–1024 query characters including non-whitespace text.",
            )
        start, end = dates(since, until)
        policy: list[Any] = profile.read_policy()
        scope = [
            profile_id,
            profile.generation,
            query,
            start,
            end,
            kind,
            policy,
            limit,
            max_requests,
        ]
        # One cursor consumes a frozen buffer once, and retains its response for
        # replay/restart. No access hashes or SDK sessions enter this local state.
        # ponytail: one search lock per profile; split by cursor if contention matters.
        async with self.locks.setdefault("global:" + profile_id, asyncio.Lock()):
            saved = self.store.state(f"global_search_cursor:{cursor}") if cursor else None
            if cursor and (
                not saved or saved["scope"] != scope or saved["expires_at"] <= utcnow().timestamp()
            ):
                raise TeleloomError(
                    "invalid_cursor",
                    "Global search cursor expired or belongs to another account, query, policy or budget.",
                )
            if saved and "result" in saved:
                return saved["result"]
            state = saved or {
                "scope": scope,
                "items": [],
                "offset": None,
                "complete": False,
                "until": end or iso(utcnow()),
                "snapshot_at": iso(utcnow()),
                "expires_at": utcnow().timestamp() + 900,
            }
            rows = [Message.model_validate(item) for item in state["items"]]
            offset = state["offset"]
            complete = state["complete"]
            requests = 0
            warnings = [
                "Telegram chooses search candidates; stream exhaustion does not prove its search index complete."
            ]
            partial_error = None
            selected_chats = policy[1] if policy[0] == "selected" else None
            while len(rows) <= limit and not complete and requests < max_requests:
                index = (offset or {}).get("chat_index", 0)
                if selected_chats is not None and index >= len(selected_chats):
                    complete = True
                    break
                adapter = await self.adapter(profile_id)
                requests += 1
                batch: dict[str, Any]
                try:
                    if selected_chats is not None:
                        chat = selected_chats[index]
                        number(chat)
                        if kind and (await adapter.resolve(chat)).kind != kind:
                            batch = {"items": [], "complete": True, "next_before": None}
                        else:
                            batch = await adapter.history_batch(
                                chat,
                                before=(offset or {}).get("before"),
                                since=since,
                                until=datetime.fromisoformat(state["until"]),
                                query=query,
                            )
                        offset = (
                            {"chat_index": index + 1, "before": None}
                            if batch["complete"]
                            else {"chat_index": index, "before": batch["next_before"]}
                        )
                        complete = batch["complete"] and index + 1 == len(selected_chats)
                    else:
                        batch = await adapter.global_search_batch(
                            query=query,
                            since=since,
                            until=datetime.fromisoformat(state["until"]),
                            offset=offset,
                            kind=kind,
                        )
                        offset, complete = batch["next_offset"], batch["complete"]
                    known = {(row.chat_id, row.id) for row in rows}
                    for item in batch["items"]:
                        if (item.chat_id, item.id) not in known:
                            rows.append(item)
                            known.add((item.chat_id, item.id))
                except Exception as exc:
                    error = exc if isinstance(exc, TeleloomError) else telegram_error(exc)
                    if not rows:
                        raise error from None
                    partial_error = {
                        "code": error.code,
                        "message": error.message,
                        "retry_after": error.retry_after,
                    }
                    warnings.append(
                        "A Telegram request failed; fetched evidence is retained. Continue explicitly or retry after the reported delay."
                    )
                    break
            selected: list[Message] = []
            characters = 0
            for row in rows[:limit]:
                if selected and characters + len(row.text) > 32000:
                    break
                selected.append(row)
                characters += len(row.text)
            pending = rows[len(selected) :]
            more = bool(pending) or not complete
            next_cursor = uuid.uuid4().hex if more else None
            result = Page(
                items=selected,
                next_cursor=next_cursor,
                source="telegram",
                incomplete=more,
                warnings=warnings,
                coverage={
                    "requested_since": start,
                    "requested_until": end,
                    "effective_until": state["until"],
                    "snapshot_at": state["snapshot_at"],
                    "returned": len(selected),
                    "requests": requests,
                    "max_requests": max_requests,
                    "ordering": "chat_then_message_id_descending"
                    if selected_chats is not None
                    else "telegram_search_order",
                    "scope": "allowed_chats" if selected_chats is not None else "telegram_global",
                    "partial_error": partial_error,
                },
            ).model_dump(mode="json")
            with self.store.db:
                self.store.db.execute(
                    "DELETE FROM state WHERE key LIKE 'global_search_cursor:%' AND json_extract(data,'$.expires_at')<=?",
                    (utcnow().timestamp(),),
                )
                if next_cursor:
                    self.store.set_state(
                        f"global_search_cursor:{next_cursor}",
                        {
                            **state,
                            "items": [row.model_dump(mode="json") for row in pending],
                            "offset": offset,
                            "complete": complete,
                        },
                    )
                if cursor:
                    self.store.set_state(
                        f"global_search_cursor:{cursor}", {**state, "result": result}
                    )
            return result

    async def inbox(self, profile_id: str, limit: int, per_chat_limit: int) -> dict[str, Any]:
        profile = self.settings.profile(profile_id)
        adapter = await self.adapter(profile_id)
        result = []
        incomplete = profile.kind == "bot"
        characters = 0
        for chat in await adapter.chats():
            if not profile.allows_read(chat.id):
                continue
            if profile.kind == "user" and not chat.unread_count:
                continue
            checkpoint = int(chat.read_inbox_max_id) if profile.kind == "user" else 0
            rows = (
                self.store.messages(
                    profile_id,
                    chat.id,
                    limit=per_chat_limit + 1,
                    incoming_only=True,
                    unprocessed_only=True,
                )
                if profile.kind == "bot"
                else await adapter.history(
                    chat.id, before=None, since=None, until=None, limit=per_chat_limit + 1
                )
            )
            unread = [
                row
                for row in rows
                if int(row.id) > checkpoint and not row.outgoing and not row.deleted
            ]
            if not unread and profile.kind == "bot":
                continue
            more = len(unread) > per_chat_limit or (
                profile.kind == "user" and chat.unread_count > len(unread)
            )
            incomplete = incomplete or more
            shown = []
            for message in unread[:per_chat_limit]:
                if characters + len(message.text) > 32000:
                    more = True
                    incomplete = True
                    break
                shown.append(message.model_dump(mode="json"))
                characters += len(message.text)
            snapshot_id = None
            if profile.kind == "bot":
                snapshot_id = uuid.uuid4().hex
                with self.store.db:
                    self.store.db.execute(
                        "DELETE FROM state WHERE key LIKE 'inbox_snapshot:%' AND json_extract(data,'$.expires_at')<=?",
                        (utcnow().timestamp(),),
                    )
                    self.store.set_state(
                        f"inbox_snapshot:{snapshot_id}",
                        {
                            "profile": profile_id,
                            "generation": profile.generation,
                            "chat": chat.id,
                            "ids": [m["id"] for m in shown],
                            "expires_at": utcnow().timestamp() + 900,
                            "watermark": self.store.state(f"bot_offset:{profile_id}", 0) - 1,
                        },
                    )
            result.append(
                {
                    "chat": chat.model_dump(),
                    "messages": shown,
                    "snapshot_id": snapshot_id,
                    "ack_through": unread[0].id if unread and not more else None,
                    "incomplete": more,
                    "ack_warning": "Acknowledging a message also acknowledges older messages, including any omitted ones.",
                }
            )
            if len(result) >= limit:
                incomplete = True
                break
        return {
            "source": "telegram_unread" if profile.kind == "user" else "local_unprocessed",
            "snapshot_at": utcnow().isoformat(),
            "chats": result,
            "incomplete": incomplete,
        }

    async def acknowledge(
        self, profile_id: str, chat_id: str, through: str, snapshot_id: str | None = None
    ) -> dict[str, Any]:
        number(chat_id)
        id_ = number(through, positive=True)
        profile = self.settings.profile(profile_id)
        profile.require_read(chat_id)
        watermark = None
        if profile.kind == "bot":
            snapshot = self.store.state(f"inbox_snapshot:{snapshot_id}") if snapshot_id else None
            if (
                not snapshot
                or snapshot["profile"] != profile_id
                or snapshot["generation"] != profile.generation
                or snapshot["chat"] != chat_id
                or snapshot["expires_at"] <= utcnow().timestamp()
                or through not in snapshot["ids"]
            ):
                raise TeleloomError(
                    "snapshot_required",
                    "Provide the current reviewed bot inbox snapshot_id and a message shown in it.",
                )
            watermark = snapshot["watermark"]
        adapter = await self.adapter(profile_id)
        rows = await adapter.history(
            chat_id, before=None, since=None, until=None, limit=1, ids=[id_]
        )
        if not rows:
            raise TeleloomError(
                "message_not_found", "Cannot acknowledge an unknown message checkpoint."
            )
        await adapter.acknowledge(chat_id, id_, update_watermark=watermark)
        return {
            "chat_id": chat_id,
            "through_message_id": through,
            "source": "telegram_unread"
            if self.settings.profile(profile_id).kind == "user"
            else "local_unprocessed",
        }
