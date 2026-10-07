import asyncio
import contextlib
import io
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from telethon import TelegramClient, errors, events, functions, types, utils
from telethon.sessions import StringSession

from . import __version__
from .config import Profile
from .models import Chat, Message, TeleloomError, utcnow
from .secrets import Secrets
from .store import Store
from .telegram.evidence import _native_chat
from .telegram.evidence import bot_message as bot_message
from .telegram.evidence import telethon_message as telethon_message


class Adapter(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def diagnostic_read(self, chat: str) -> int: ...
    async def chats(self) -> list[Chat]: ...
    async def folders(self) -> list[dict[str, Any]]: ...
    async def folder_snapshot(self) -> dict[str, Any]: ...
    async def folder_limits(self) -> dict[str, Any]: ...
    async def contacts(self, view: str) -> dict[str, Any]: ...
    async def search_contacts(self, query: str) -> dict[str, Any]: ...
    async def blocked_contacts(self) -> dict[str, Any]: ...
    async def common_contact_chats(self, contact_id: str) -> dict[str, Any]: ...
    async def resolve(self, target: str) -> Chat: ...
    async def global_search_batch(
        self,
        *,
        query: str,
        since: datetime | None,
        until: datetime,
        offset: dict[str, Any] | None,
        kind: str | None,
    ) -> dict[str, Any]: ...
    async def public_chats(self, query: str, limit: int) -> list[Chat]: ...
    async def context_window(self, chat: str, target: int, size: int) -> dict[str, Any]: ...
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
    ) -> list[Message]: ...
    async def history_batch(
        self,
        chat: str,
        *,
        before: int | None,
        since: datetime | None,
        until: datetime | None,
        query: str | None = None,
    ) -> dict[str, Any]: ...
    async def topics(
        self, chat: str, *, offset: dict[str, Any] | None, limit: int
    ) -> dict[str, Any]: ...
    async def thread(
        self, chat: str, root: int, *, before: int | None, limit: int
    ) -> list[Message]: ...
    async def thread_batch(
        self, chat: str, root: int, *, before: int | None, limit: int
    ) -> dict[str, Any]: ...
    async def discussion(self, chat: str, post: int) -> dict[str, Any]: ...
    async def pinned(self, chat: str, *, before: int | None, limit: int) -> list[Message]: ...
    async def pinned_batch(
        self, chat: str, *, before: int | None, limit: int
    ) -> dict[str, Any]: ...
    async def download_attachment(
        self, chat: str, message_id: int, destination: Path, *, max_bytes: int
    ) -> dict[str, Any]: ...
    async def transcribe_audio(self, chat: str, message_id: int) -> dict[str, Any]: ...
    async def acknowledge(
        self, chat: str, through: int, update_watermark: int | None = None
    ) -> None: ...
    async def send(self, chat: str, text: str, reply_to: str | None, random_id: int) -> str: ...
    async def mutate_message(self, operation: dict[str, Any], random_id: int) -> dict[str, Any]: ...
    async def drafts(self) -> list[dict[str, Any]]: ...
    async def message_state(
        self,
        chat: str,
        kind: str,
        message_id: str | None,
        limit: int,
        query: str,
        bot_id: str | None,
    ) -> dict[str, Any]: ...
    async def administration_read(
        self, operation: dict[str, Any], offset: int
    ) -> dict[str, Any]: ...
    async def account_read(self, operation: dict[str, Any], offset: int) -> dict[str, Any]: ...
    async def mutate_administration(
        self,
        operation: dict[str, Any],
        random_id: int,
        receipts: list[dict[str, Any]],
        file_bytes: bytes | None = None,
    ) -> dict[str, Any]: ...
    async def download_avatar(
        self,
        chat: str,
        photo_id: str | None,
        destination: Path,
        *,
        max_bytes: int,
        message_id: str | None = None,
    ) -> dict[str, Any]: ...


def user_client(
    profile_id: str,
    profile: Profile,
    credentials: Secrets,
    *,
    new: bool = False,
    store: Store | None = None,
) -> TelegramClient:
    api_id = profile.api_id
    if not api_id:
        raw = credentials.require(profile_id, "api_id")
        try:
            api_id = int(raw)
        except ValueError:
            raise TeleloomError("credentials_invalid", "API ID must be an integer.") from None
    session = "" if new else credentials.require(profile_id, "session")
    proxy = credentials.get(profile_id, "proxy")
    kwargs: dict[str, Any] = {}
    if proxy:
        parsed = urlparse(proxy)
        if (
            parsed.scheme not in {"socks5", "socks4", "http"}
            or not parsed.hostname
            or not parsed.port
        ):
            raise TeleloomError(
                "invalid_proxy",
                "Proxy must use socks5://, socks4:// or http:// with host and port.",
            )
        kwargs["proxy"] = {
            "proxy_type": parsed.scheme,
            "addr": parsed.hostname,
            "port": parsed.port,
            "username": parsed.username,
            "password": parsed.password,
            "rdns": True,
        }
    from .session_state import SavedMetadataSession

    saved = (
        SavedMetadataSession(session, store, profile_id, profile.generation)
        if store is not None and not new
        else StringSession(session)
    )
    return TelegramClient(
        saved,
        api_id,
        credentials.require(profile_id, "api_hash"),
        device_model="teleloom",
        system_version="local",
        app_version=__version__,
        flood_sleep_threshold=0,
        request_retries=0,
        **kwargs,
    )


def telegram_error(exc: Exception) -> TeleloomError:
    if isinstance(exc, errors.FloodWaitError):
        return TeleloomError(
            "rate_limited", "Telegram requires waiting before retrying.", retry_after=exc.seconds
        )
    if isinstance(exc, TelegramRetryAfter):
        return TeleloomError(
            "rate_limited",
            "Telegram requires waiting before retrying.",
            retry_after=exc.retry_after,
        )
    if isinstance(
        exc,
        (
            errors.PeerFloodError,
            errors.UserDeactivatedBanError,
            errors.AuthKeyUnregisteredError,
            errors.SessionRevokedError,
            TelegramForbiddenError,
        ),
    ):
        return TeleloomError(
            "account_restricted",
            "Account or target is restricted; inspect Telegram before resuming.",
        )
    if isinstance(exc, TelegramConflictError):
        return TeleloomError("polling_conflict", "Another process is receiving this bot's updates.")
    if isinstance(exc, (errors.RPCError, TelegramAPIError)):
        return TeleloomError(
            "telegram_rejected",
            "Telegram rejected the operation; inspect account permissions and input.",
        )
    return TeleloomError(
        "connection_error", "Telegram connection failed; delivery outcome may be uncertain."
    )


def telegram_date_bound(value: datetime | None, *, upper: bool) -> datetime | None:
    """Use conservative integer-second SDK bounds; exact filtering stays local."""
    if value is None:
        return None
    seconds = math.ceil(value.timestamp()) if upper else math.floor(value.timestamp()) - 1
    return datetime.fromtimestamp(seconds, UTC)


def acknowledge_bot(
    store: Store, profile_id: str, chat: str, through: int, update_watermark: int | None
) -> None:
    if update_watermark is None:
        raise TeleloomError(
            "snapshot_required", "Bot acknowledgments require the reviewed inbox snapshot."
        )
    with store.db:
        store.db.execute(
            "DELETE FROM bot_pending WHERE profile=? AND chat=? AND CAST(id AS INTEGER)<=? AND update_id<=?",
            (profile_id, chat, through, update_watermark),
        )


def _unavailable_root_error(exc: errors.RPCError, chat: str, root: int) -> TeleloomError | None:
    if isinstance(exc, errors.MsgIdInvalidError):
        code, message, status = (
            "message_unavailable",
            "The requested reply root is deleted or unavailable.",
            "deleted_or_unavailable",
        )
    elif isinstance(exc, errors.ChannelPrivateError):
        code, message, status = (
            "peer_unavailable",
            "This profile cannot access the requested reply chat.",
            "unavailable",
        )
    elif exc.message == "TOPIC_ID_INVALID":
        # Telethon has no dedicated class for this documented Telegram error.
        code, message, status = (
            "topic_unavailable",
            "The requested forum topic is deleted or unavailable.",
            "deleted_or_unavailable",
        )
    else:
        return None
    return TeleloomError(
        code,
        message,
        details={"chat_id": chat, "root_message_id": str(root), "original_status": status},
    )


class UserAdapter:
    async def mutate_administration(
        self,
        operation: dict[str, Any],
        random_id: int,
        receipts: list[dict[str, Any]],
        file_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        from .account_operations import user_mutate

        return await user_mutate(self, operation, random_id, receipts, file_bytes)

    async def drafts(self) -> list[dict[str, Any]]:
        from .mutations import user_drafts

        return await user_drafts(self)

    async def mutate_message(self, operation: dict[str, Any], random_id: int) -> dict[str, Any]:
        from .mutations import user_mutate

        if operation["kind"].startswith("contacts_"):
            from .contacts import execute_contact_operation

            return await execute_contact_operation(self, operation, random_id)
        if operation["kind"].startswith("folder_"):
            from .folder_operations import execute_folder_operation

            return await execute_folder_operation(self, operation, random_id)
        return await user_mutate(self, operation, random_id)

    async def message_state(
        self,
        chat: str,
        kind: str,
        message_id: str | None,
        limit: int,
        query: str,
        bot_id: str | None,
    ) -> dict[str, Any]:
        from .mutations import user_message_state

        return await user_message_state(self, chat, kind, message_id, limit, query, bot_id)

    async def download_avatar(
        self,
        chat: str,
        photo_id: str | None,
        destination: Path,
        *,
        max_bytes: int,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        from .administration import download_avatar

        return await download_avatar(
            self, chat, photo_id, destination, max_bytes=max_bytes, message_id=message_id
        )

    async def administration_read(self, operation: dict[str, Any], offset: int) -> dict[str, Any]:
        from .administration import user_read

        return await user_read(self, operation, offset)

    async def account_read(self, operation: dict[str, Any], offset: int) -> dict[str, Any]:
        from .administration import user_account_read

        return await user_account_read(self, operation, offset)

    def __init__(
        self, profile_id: str, profile: Profile, store: Store, credentials: Secrets
    ) -> None:
        self.profile_id = profile_id
        self.profile = profile
        self.store = store
        self.client = user_client(profile_id, profile, credentials, store=store)
        self.session_lease: Any = None

    async def start(self) -> None:
        from .session_state import acquire_session

        self.session_lease = acquire_session(self.client.session)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            renewal = "bot --backend mtproto" if self.profile.kind == "bot" else "user"
            raise TeleloomError(
                "auth_required",
                f"Stop the daemon, then run teleloom auth {renewal} --profile {self.profile_id} --replace to renew this saved profile explicitly.",
            )
        user = await self.client.get_me()
        if user is None:
            raise TeleloomError("auth_required", "The saved profile needs explicit owner renewal.")
        if bool(getattr(user, "bot", False)) != (self.profile.kind == "bot"):
            raise TeleloomError(
                "account_changed", "Session account kind differs from the configured profile."
            )
        if self.profile.identity.get("id") and str(user.id) != self.profile.identity["id"]:
            raise TeleloomError(
                "account_changed",
                "Session identity differs from this configured profile. Authenticate explicitly with --replace.",
            )
        self.client.add_event_handler(self._message_event, events.NewMessage())
        self.client.add_event_handler(self._message_event, events.MessageEdited())
        self.client.add_event_handler(self._delete_event, events.MessageDeleted())
        if self.profile.sync_chats:
            with self.store.db:
                self.store.set_state(
                    f"gap:{self.profile_id}",
                    "Updates while the daemon was offline may be missing; synchronize for fresh coverage.",
                )

    async def _message_event(self, event: Any) -> None:
        from .events import ingest

        chat = str(event.chat_id)
        if (
            not self.profile.allows_read(chat)
            or (self.profile.kind == "bot" and not self.profile.polling)
            or self.store.state(f"profile_generation:{self.profile_id}", self.profile.generation)
            != self.profile.generation
        ):
            return
        message = telethon_message(self.profile_id, event.message)
        if chat in self.profile.event_chats:
            ingest(
                self.store,
                self.profile_id,
                self.profile,
                message,
                "edit" if isinstance(event, events.MessageEdited.Event) else "new",
            )
        if self.profile.kind == "bot":
            known = self.store.messages(self.profile_id, chat, ids=[int(message.id)], limit=1)
            if known and known[0] == message:
                return
            # Native updates use the existing local inbox watermark, not peer-specific Telegram pts.
            # Commit the event first; originals, pending state and watermark advance together.
            key = f"bot_offset:{self.profile_id}"
            with self.store.db:
                entity = getattr(event, "chat", None)
                known_chat = next(
                    (row for row in self.store.chats(self.profile_id) if row.id == chat), None
                )
                if entity is not None:
                    observed = _native_chat(entity)
                    known_chat = (
                        known_chat.model_copy(update=observed.model_dump(exclude_unset=True))
                        if known_chat
                        else observed
                    )
                self.store.chat(self.profile_id, known_chat or Chat(id=chat, title=chat))
                watermark = self.store.state(key, 0)
                self.store.save_messages(
                    [message], (key, watermark + 1), pending_update_id=watermark
                )
        elif chat in self.profile.sync_chats:
            self.store.save_messages([message])

    async def _delete_event(self, event: Any) -> None:
        chat = str(event.chat_id) if event.chat_id is not None else None
        if (
            (self.profile.kind == "bot" and not self.profile.polling)
            or self.store.state(f"profile_generation:{self.profile_id}", self.profile.generation)
            != self.profile.generation
            or (chat is not None and not self.profile.allows_read(chat))
        ):
            return
        if chat in self.profile.event_chats and self.profile.allows_read(chat):
            from .events import ingest

            for id_ in event.deleted_ids:
                ingest(
                    self.store,
                    self.profile_id,
                    self.profile,
                    Message(
                        profile_id=self.profile_id,
                        chat_id=chat,
                        id=str(id_),
                        date=utcnow(),
                        deleted=True,
                    ),
                    "delete",
                )
        if self.profile.kind == "bot" or chat is None or chat in self.profile.sync_chats:
            self.store.delete_messages(
                self.profile_id, chat, [str(id_) for id_ in event.deleted_ids]
            )

    async def close(self) -> None:
        await self.client.disconnect()
        if getattr(self, "session_lease", None) is not None:
            self.session_lease.release()
            self.session_lease = None

    async def transcribe_audio(self, chat: str, message_id: int) -> dict[str, Any]:
        self.profile.require_read(chat)
        peer = await self._input_peer(chat)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        buffered: list[Any] = []
        transcription_id = None

        async def update(raw: Any) -> None:
            if (
                not isinstance(raw, types.UpdateTranscribedAudio)
                or raw.msg_id != message_id
                or str(utils.get_peer_id(raw.peer)) != chat
            ):
                return
            if transcription_id is None:
                buffered[:] = [raw]
            elif raw.transcription_id == transcription_id and not raw.pending and not future.done():
                future.set_result(raw)

        self.client.add_event_handler(update, events.Raw(types=types.UpdateTranscribedAudio))
        try:
            try:
                result = await self.client(
                    functions.messages.TranscribeAudioRequest(peer=peer, msg_id=message_id)
                )
            except Exception as exc:
                code = {
                    "PremiumAccountRequiredError": "premium_or_trial_required",
                    "MsgVoiceTooLongError": "telegram_audio_too_long",
                    "MsgVoiceMissingError": "telegram_voice_required",
                    "TranscriptionFailedError": "telegram_transcription_failed",
                }.get(type(exc).__name__)
                if code:
                    raise TeleloomError(
                        code,
                        "Telegram account/audio transcription restriction.",
                        details={"restriction": code},
                    ) from None
                raise
            transcription_id = result.transcription_id
            trial = {
                "trial_remains_num": getattr(result, "trial_remains_num", None),
                "trial_remains_until_date": getattr(result, "trial_remains_until_date", None),
            }
            if trial["trial_remains_until_date"] is not None:
                value = trial["trial_remains_until_date"]
                trial["trial_remains_until_date"] = (
                    value.isoformat() if hasattr(value, "isoformat") else value
                )
            if result.pending:
                for raw in buffered:
                    await update(raw)
                result = await future
            return {"text": result.text, "transcription_id": str(result.transcription_id), **trial}
        finally:
            self.client.remove_event_handler(update)

    async def folders(self) -> list[dict[str, Any]]:
        snapshot = await self.folder_snapshot()
        result: list[dict[str, Any]] = []
        for folder in snapshot["items"]:
            if folder["type"] == "system":
                continue
            definition = folder["definition"]
            result.append(
                {
                    "id": folder["id"],
                    **{
                        key: definition[key]
                        for key in (
                            "title",
                            "included_chat_ids",
                            "pinned_chat_ids",
                            "excluded_chat_ids",
                        )
                    },
                    "rules": {key: True for key, enabled in definition["rules"].items() if enabled},
                }
            )
            if folder["type"] == "shared":
                result[-1]["shared"] = True
        return result

    async def contacts(self, view: str) -> dict[str, Any]:
        from .contacts import read_contacts

        return await read_contacts(self, view)

    async def folder_snapshot(self) -> dict[str, Any]:
        from .folder_operations import read_folder_snapshot

        return await read_folder_snapshot(self)

    async def folder_limits(self) -> dict[str, Any]:
        from .folder_operations import read_folder_limits

        return await read_folder_limits(self)

    async def search_contacts(self, query: str) -> dict[str, Any]:
        from .contacts import search_contacts

        return await search_contacts(self, query)

    async def blocked_contacts(self) -> dict[str, Any]:
        from .contacts import blocked_contacts

        return await blocked_contacts(self)

    async def common_contact_chats(self, contact_id: str) -> dict[str, Any]:
        from .contacts import common_contact_chats

        return await common_contact_chats(self, contact_id)

    async def topics(
        self, chat: str, *, offset: dict[str, Any] | None, limit: int
    ) -> dict[str, Any]:
        offset = offset or {}
        response = await self.client(
            functions.messages.GetForumTopicsRequest(
                peer=await self._input_peer(chat),
                offset_date=datetime.fromisoformat(offset["date"]) if offset.get("date") else None,
                offset_id=int(offset.get("id", 0)),
                offset_topic=int(offset.get("topic", 0)),
                limit=min(limit, 100),
            )
        )
        raw_topics = response.topics[:limit]
        items = []
        dates = {raw.id: raw.date for raw in response.messages if getattr(raw, "date", None)}
        for raw in raw_topics:
            deleted = isinstance(raw, types.ForumTopicDeleted)
            items.append(
                {
                    "id": str(raw.id),
                    "chat_id": chat,
                    "title": getattr(raw, "title", None),
                    "date": raw.date.astimezone(UTC).isoformat()
                    if getattr(raw, "date", None)
                    else None,
                    "top_message_id": str(raw.top_message)
                    if getattr(raw, "top_message", None)
                    else None,
                    "closed": bool(getattr(raw, "closed", False)) if not deleted else None,
                    "hidden": bool(getattr(raw, "hidden", False)) if not deleted else None,
                    "pinned": bool(getattr(raw, "pinned", False)) if not deleted else None,
                    "deleted": deleted,
                    "link": f"https://t.me/c/{chat.removeprefix('-100')}/{raw.id}",
                }
            )
        next_offset = None
        # Telegram may return a short page while more topics remain. Its count is
        # total, so later pages conservatively continue until an empty response.
        if raw_topics and (offset or response.count > len(raw_topics)):
            last = raw_topics[-1]
            last_id = getattr(last, "top_message", 0) or 0
            date = (
                getattr(last, "date", None)
                if getattr(response, "order_by_create_date", False)
                else dates.get(last_id) or getattr(last, "date", None)
            )
            next_offset = {
                "date": date.astimezone(UTC).isoformat() if date else None,
                "id": str(last_id),
                "topic": str(last.id),
            }
        return {"items": items, "next_offset": next_offset}

    async def thread(
        self, chat: str, root: int, *, before: int | None, limit: int
    ) -> list[Message]:
        return (await self.thread_batch(chat, root, before=before, limit=limit))["items"]

    async def thread_batch(
        self, chat: str, root: int, *, before: int | None, limit: int
    ) -> dict[str, Any]:
        try:
            response = await self.client(
                functions.messages.GetRepliesRequest(
                    peer=await self._input_peer(chat),
                    msg_id=root,
                    offset_id=before or 0,
                    offset_date=None,
                    add_offset=0,
                    limit=min(limit, 101),
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
        except errors.RPCError as exc:
            unavailable = _unavailable_root_error(exc, chat, root)
            if unavailable is None:
                raise
            raise unavailable from None
        entities = {utils.get_peer_id(entity): entity for entity in response.users + response.chats}
        chat_entity = entities.get(int(chat))
        cached = next((item for item in self.store.chats(self.profile_id) if item.id == chat), None)
        general = root == 1 and (
            bool(getattr(chat_entity, "forum", False)) or bool(cached and cached.forum)
        )
        result = []
        for raw in response.messages:
            if not getattr(raw, "date", None) or str(raw.chat_id) != chat:
                continue
            reply = getattr(raw, "reply_to", None)
            raw_root = getattr(reply, "reply_to_top_id", None) or getattr(
                reply, "reply_to_msg_id", None
            )
            in_general = general and not getattr(reply, "forum_topic", False)
            if raw.id != root and raw_root != root and not in_general:
                continue
            if before is not None and raw.id >= before:
                continue
            message = telethon_message(
                self.profile_id,
                raw,
                sender=entities.get(raw.sender_id),
                chat_entity=entities.get(raw.chat_id),
            )
            message.thread_root_id = str(root)
            if general:
                message.topic_id = "1"
            result.append(message)
        oldest = min((raw.id for raw in response.messages if getattr(raw, "id", 0) > 0), default=0)
        complete = not response.messages or 0 < oldest <= root
        if response.messages and (not oldest or before is not None and oldest >= before):
            raise TeleloomError(
                "pagination_stalled", "Telegram did not advance the bounded thread cursor."
            )
        return {
            "items": sorted(result, key=lambda row: int(row.id), reverse=True)[:limit],
            "next_before": None if complete else oldest,
            "complete": complete,
        }

    async def discussion(self, chat: str, post: int) -> dict[str, Any]:
        try:
            response = await self.client(
                functions.messages.GetDiscussionMessageRequest(
                    peer=await self._input_peer(chat), msg_id=post
                )
            )
        except errors.MsgIdInvalidError:
            return {"chat_id": None, "root_id": None, "original_status": "deleted_or_unavailable"}
        except errors.ChannelPrivateError:
            raise TeleloomError(
                "peer_unavailable", "This profile cannot access the discussion or original channel."
            ) from None
        original = next(
            (
                raw
                for raw in response.messages
                if str(getattr(raw, "chat_id", None)) == chat and raw.id == post
            ),
            None,
        )
        if original is None:
            try:
                original = await self.client.get_messages(await self._input_peer(chat), ids=post)
            except (errors.ChannelPrivateError, errors.MsgIdInvalidError):
                original = None
        roots = []
        for raw in response.messages:
            forward = getattr(raw, "fwd_from", None)
            source = getattr(forward, "from_id", None)
            if (
                str(getattr(raw, "chat_id", None)) != chat
                and source is not None
                and str(utils.get_peer_id(source)) == chat
                and getattr(forward, "channel_post", None) == post
            ):
                roots.append(raw)
        if not roots:
            return {
                "chat_id": None,
                "root_id": None,
                "original_status": "available" if original else "unavailable",
            }
        selected = min(roots, key=lambda raw: raw.id)
        album = getattr(selected, "grouped_id", None)
        if album:
            roots = [
                raw
                for raw in response.messages
                if str(getattr(raw, "chat_id", None)) == str(selected.chat_id)
                and getattr(raw, "grouped_id", None) == album
                and getattr(raw, "fwd_from", None)
                and getattr(raw.fwd_from, "from_id", None)
                and str(utils.get_peer_id(raw.fwd_from.from_id)) == chat
            ]
        root = min(roots, key=lambda raw: raw.id)
        # Prime the existing SDK's entity cache for discussion reads, including
        # non-member comments. Do not create another client or persist access hashes.
        self.client._mb_entity_cache.extend(response.users, response.chats)
        return {
            "chat_id": str(root.chat_id),
            "root_id": str(root.id),
            "original_status": "available" if original else "unavailable",
        }

    async def pinned(self, chat: str, *, before: int | None, limit: int) -> list[Message]:
        return (await self.pinned_batch(chat, before=before, limit=limit))["items"]

    async def pinned_batch(self, chat: str, *, before: int | None, limit: int) -> dict[str, Any]:
        response = await self.client(
            functions.messages.SearchRequest(
                peer=await self._input_peer(chat),
                q="",
                filter=types.InputMessagesFilterPinned(),
                min_date=None,
                max_date=None,
                offset_id=before or 0,
                add_offset=0,
                limit=min(limit, 101),
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        entities = {utils.get_peer_id(entity): entity for entity in response.users + response.chats}
        result = []
        for raw in response.messages:
            if (
                getattr(raw, "date", None)
                and str(raw.chat_id) == chat
                and (before is None or raw.id < before)
            ):
                message = telethon_message(
                    self.profile_id,
                    raw,
                    sender=entities.get(raw.sender_id),
                    chat_entity=entities.get(raw.chat_id),
                )
                message.pinned = True
                result.append(message)
        oldest = min((raw.id for raw in response.messages if getattr(raw, "id", 0) > 0), default=0)
        complete = not response.messages or oldest == 1
        if response.messages and (not oldest or before is not None and oldest >= before):
            raise TeleloomError(
                "pagination_stalled", "Telegram did not advance the bounded pinned cursor."
            )
        return {
            "items": sorted(result, key=lambda row: int(row.id), reverse=True)[:limit],
            "next_before": None if complete else oldest,
            "complete": complete,
        }

    async def download_attachment(
        self, chat: str, message_id: int, destination: Path, *, max_bytes: int
    ) -> dict[str, Any]:
        raw = await self.client.get_messages(await self._input_peer(chat), ids=message_id)
        if raw is None or str(raw.chat_id) != chat:
            raise TeleloomError(
                "message_unavailable", "The selected message is deleted or inaccessible."
            )
        if not raw.media or not raw.file:
            raise TeleloomError(
                "attachment_unavailable", "The selected message has no downloadable attachment."
            )
        size = raw.file.size
        if size is not None and size > max_bytes:
            raise TeleloomError(
                "attachment_too_large", "Attachment exceeds the selected byte budget."
            )
        stream = self.client.iter_download(
            raw.media,
            request_size=65536,
            chunk_size=65536,
            limit=max_bytes // 65536 + 1,
        )
        total = 0
        created = False
        try:
            with destination.open("xb") as output:
                created = True
                async for chunk in stream:
                    if total + len(chunk) > max_bytes:
                        raise TeleloomError(
                            "attachment_too_large", "Attachment exceeds the selected byte budget."
                        )
                    output.write(chunk)
                    total += len(chunk)
            if size is not None and total != size:
                raise TeleloomError(
                    "attachment_incomplete",
                    "The selected attachment could not be downloaded completely.",
                )
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if close:
                await close()
        return {"size": total, "name": raw.file.name, "mime_type": raw.file.mime_type}

    async def chats(self) -> list[Chat]:
        result = []
        defaults: dict[str, Any] = {}
        now = utcnow()
        # An unspecified folder includes the archive (folder_id=1) as well.
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            settings = getattr(dialog.dialog, "notify_settings", None)
            mute_until = getattr(settings, "mute_until", None)
            if settings is not None and mute_until is None:
                category = (
                    "groups" if dialog.is_group else "broadcasts" if dialog.is_channel else "users"
                )
                if category not in defaults:
                    notify_peer = {
                        "groups": types.InputNotifyChats,
                        "broadcasts": types.InputNotifyBroadcasts,
                        "users": types.InputNotifyUsers,
                    }[category]()
                    defaults[category] = await self.client(
                        functions.account.GetNotifySettingsRequest(peer=notify_peer)
                    )
                mute_until = getattr(defaults[category], "mute_until", None)
            if isinstance(mute_until, datetime):
                muted: bool | None = mute_until.astimezone(UTC) > now
            elif mute_until is not None:
                muted = int(mute_until) > now.timestamp()
            else:
                muted = False if settings is not None else None
            chat = Chat(
                id=str(dialog.id),
                title=dialog.name or str(dialog.id),
                kind="group" if dialog.is_group else "channel" if dialog.is_channel else "private",
                username=getattr(entity, "username", None),
                unread_count=dialog.unread_count,
                read_inbox_max_id=str(dialog.dialog.read_inbox_max_id),
                top_message_id=str(dialog.dialog.top_message),
                is_contact=bool(getattr(entity, "contact", False))
                if isinstance(entity, types.User)
                else None,
                is_bot=bool(getattr(entity, "bot", False))
                if isinstance(entity, types.User)
                else None,
                muted=muted,
                archived=getattr(dialog.dialog, "folder_id", None) == 1,
                unread_mark=bool(getattr(dialog.dialog, "unread_mark", False)),
                unread_mentions_count=getattr(dialog.dialog, "unread_mentions_count", 0) or 0,
                forum=bool(getattr(entity, "forum", False)),
            )
            if self.profile.allows_read(chat.id):
                self.store.chat(self.profile_id, chat)
            result.append(chat)
        return result

    async def _input_peer(self, chat: str) -> Any:
        try:
            return await self.client.get_input_entity(int(chat))
        except ValueError:
            pass
        # StringSession persists authorization, not entity access hashes. Recover
        # them inside this profile's SDK session without putting them in our index.
        cached = next((item for item in self.store.chats(self.profile_id) if item.id == chat), None)
        if cached and cached.username:
            try:
                entity = await self.client.get_entity("@" + cached.username)
            except (ValueError, errors.UsernameNotOccupiedError):
                pass
            else:
                if str(utils.get_peer_id(entity)) != chat:
                    raise TeleloomError(
                        "peer_identity_changed",
                        "The cached username now belongs to another Telegram ID. Refresh the chat before preparing a new plan.",
                    )
                return await self.client.get_input_entity(entity)
        if self.profile.kind == "bot":
            raise TeleloomError(
                "peer_unavailable",
                "This bot has no exact peer identity in its saved session. Resolve the peer's current username first.",
            )
        async for dialog in self.client.iter_dialogs():
            if str(dialog.id) == chat:
                return await self.client.get_input_entity(dialog.entity)
        raise TeleloomError(
            "peer_unavailable",
            "This profile cannot resolve the Telegram ID. Refresh chats or resolve its current @username before preparing a new plan.",
        )

    async def resolve(self, target: str) -> Chat:
        cached = self.store.chats(self.profile_id)
        exact = [
            chat
            for chat in cached
            if chat.id == target
            or (chat.username and "@" + chat.username.lower() == target.lower())
            or chat.title == target
        ]
        if len(exact) > 1:
            raise TeleloomError(
                "ambiguous_chat",
                "Several chats have this exact title. Use an ID or @username.",
                details={"candidates": [chat.model_dump() for chat in exact]},
            )
        if exact:
            peer = await self._input_peer(exact[0].id)
            current = _native_chat(await self.client.get_entity(peer))
            if current.id != exact[0].id:
                raise TeleloomError(
                    "peer_identity_changed",
                    "The resolved peer changed Telegram identity. Refresh the chat before preparing a new plan.",
                )
            chat = exact[0].model_copy(update=current.model_dump(exclude_unset=True))
            self.store.chat(self.profile_id, chat)
            return chat
        if not target.lstrip("-").isdigit() and not target.startswith("@"):
            await self.chats()
            exact = [chat for chat in self.store.chats(self.profile_id) if chat.title == target]
            if len(exact) == 1:
                return exact[0]
            raise TeleloomError(
                "ambiguous_chat" if exact else "chat_not_found",
                "Use an exact ID or @username.",
                details={"candidates": [chat.model_dump() for chat in exact]},
            )
        value = await self._input_peer(target) if target.lstrip("-").isdigit() else target
        entity = await self.client.get_entity(value)
        chat = _native_chat(entity)
        self.store.chat(self.profile_id, chat)
        return chat

    async def public_chats(self, query: str, limit: int) -> list[Chat]:
        response = await self.client(functions.contacts.SearchRequest(q=query, limit=limit))
        matched = {utils.get_peer_id(peer) for peer in response.my_results + response.results}
        result = [
            _native_chat(entity)
            for entity in response.chats + response.users
            if utils.get_peer_id(entity) in matched
        ]
        for chat in result:
            self.store.chat(self.profile_id, chat)
        return result

    async def context_window(self, chat: str, target: int, size: int) -> dict[str, Any]:
        central = await self.history(
            chat, before=None, since=None, until=None, limit=1, ids=[target]
        )
        if not central:
            raise TeleloomError(
                "message_unavailable",
                "The selected central message is deleted or unavailable to this profile.",
            )
        older = (
            await self.history(chat, before=target, since=None, until=None, limit=size + 1)
            if size
            else []
        )
        newer = []
        if size:
            peer = await self._input_peer(chat)
            async for raw in self.client.iter_messages(
                peer, min_id=target, reverse=True, limit=size + 1
            ):
                if raw is not None and getattr(raw, "date", None) and raw.id > target:
                    newer.append(telethon_message(self.profile_id, raw))
                if len(newer) == size + 1:
                    break
        return {
            "items": [central[0], *older[:size], *newer[:size]],
            "has_older": len(older) > size,
            "has_newer": len(newer) > size,
        }

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
        kwargs: dict[str, Any] = {
            # Apply the accepted-message budget after date filtering, including lookahead.
            "limit": None,
            "offset_id": before or 0,
            "offset_date": telegram_date_bound(until, upper=True),
            "search": query,
        }
        if ids:
            kwargs = {"ids": ids}
        result = []
        try:
            peer = await self._input_peer(chat)
            async for raw in self.client.iter_messages(peer, **kwargs):
                if raw is None or not getattr(raw, "date", None):
                    continue
                if since and raw.date < since:
                    if not ids:
                        break
                    continue
                if until and raw.date >= until:
                    continue
                result.append(telethon_message(self.profile_id, raw))
                if not ids and len(result) >= limit:
                    break
        except errors.RPCError as exc:
            unavailable = (
                _unavailable_root_error(exc, chat, ids[0]) if ids and len(ids) == 1 else None
            )
            if unavailable is None:
                raise
            raise unavailable from None
        return result

    async def diagnostic_read(self, chat: str) -> int:
        if self.profile.kind != "user":
            raise TeleloomError("unsupported_capability", "Live history requires a user profile.")
        try:
            # Exact saved peer only: diagnostics never enumerate dialogs or resolve usernames.
            peer = self.client.session.get_input_entity(int(chat))
            if str(utils.get_peer_id(peer)) != chat:
                raise ValueError
        except ValueError:
            raise TeleloomError(
                "peer_unavailable", "The exact peer is absent from saved metadata."
            ) from None
        response = await self.client(
            functions.messages.GetHistoryRequest(
                peer=peer,
                offset_id=0,
                offset_date=None,
                add_offset=0,
                limit=1,
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        return sum(
            1
            for raw in response.messages[:1]
            if getattr(raw, "date", None) and str(raw.chat_id) == chat
        )

    async def history_batch(
        self,
        chat: str,
        *,
        before: int | None,
        since: datetime | None,
        until: datetime | None,
        query: str | None = None,
    ) -> dict[str, Any]:
        peer = await self._input_peer(chat)
        if query:
            request = functions.messages.SearchRequest(
                peer=peer,
                q=query,
                filter=types.InputMessagesFilterEmpty(),
                min_date=telegram_date_bound(since, upper=False),
                max_date=telegram_date_bound(until, upper=True),
                offset_id=before or 0,
                add_offset=0,
                limit=100,
                max_id=0,
                min_id=0,
                hash=0,
            )
        else:
            request = functions.messages.GetHistoryRequest(
                peer=peer,
                offset_id=before or 0,
                offset_date=telegram_date_bound(until, upper=True),
                add_offset=0,
                limit=100,
                max_id=0,
                min_id=0,
                hash=0,
            )
        response = await self.client(request)
        entities = {utils.get_peer_id(entity): entity for entity in response.users + response.chats}
        raw_rows = [raw for raw in response.messages if getattr(raw, "id", 0) > 0]
        accepted = [
            telethon_message(
                self.profile_id,
                raw,
                sender=entities.get(raw.sender_id),
                chat_entity=entities.get(raw.chat_id),
            )
            for raw in raw_rows
            if getattr(raw, "date", None)
            and str(raw.chat_id) == chat
            and (before is None or raw.id < before)
            and (since is None or raw.date >= since)
            and (until is None or raw.date < until)
        ]
        oldest = min((raw.id for raw in raw_rows), default=0)
        reached_start = bool(
            since and any(getattr(raw, "date", None) and raw.date < since for raw in raw_rows)
        )
        # Telegram may return a short slice despite older visible messages
        # remaining (for example hidden posts). Only an empty raw response or
        # the inclusive start/ID boundary proves exhaustion.
        complete = not response.messages or reached_start or oldest == 1
        if not raw_rows and response.messages:
            raise TeleloomError(
                "pagination_stalled",
                "Telegram returned no usable IDs for the bounded history cursor.",
            )
        if raw_rows and before is not None and oldest >= before and not complete:
            raise TeleloomError(
                "pagination_stalled", "Telegram did not advance the bounded history cursor."
            )
        return {
            "items": accepted,
            "next_before": None if complete else oldest,
            "complete": complete,
        }

    async def global_search_batch(
        self,
        *,
        query: str,
        since: datetime | None,
        until: datetime,
        offset: dict[str, Any] | None,
        kind: str | None,
    ) -> dict[str, Any]:
        offset = offset or {}
        response = await self.client(
            functions.messages.SearchGlobalRequest(
                q=query,
                filter=types.InputMessagesFilterEmpty(),
                min_date=telegram_date_bound(since, upper=False),
                max_date=telegram_date_bound(until, upper=True),
                offset_rate=offset.get("rate", 0),
                offset_peer=await self._input_peer(offset["chat_id"])
                if offset.get("chat_id")
                else types.InputPeerEmpty(),
                offset_id=offset.get("id", 0),
                limit=100,
                broadcasts_only=kind == "channel" or None,
                groups_only=kind == "group" or None,
                users_only=kind == "private" or None,
            )
        )
        available = {
            utils.get_peer_id(entity): entity for entity in response.users + response.chats
        }
        raw_rows = [
            row
            for row in response.messages
            if getattr(row, "id", 0) > 0 and getattr(row, "peer_id", None)
        ]
        if not raw_rows and response.messages:
            raise TeleloomError(
                "pagination_stalled", "Global search returned no usable peer/message cursor."
            )
        result = [
            telethon_message(
                self.profile_id,
                row,
                sender=available.get(row.sender_id),
                chat_entity=available.get(row.chat_id),
            )
            for row in raw_rows
            if getattr(row, "date", None)
            and (since is None or row.date >= since)
            and row.date < until
        ]
        next_offset = None
        if raw_rows:
            last = raw_rows[-1]
            rate = getattr(response, "next_rate", None)
            if rate is None and getattr(last, "date", None):
                rate = int(last.date.timestamp())
            if rate is None:
                raise TeleloomError(
                    "pagination_stalled", "Global search supplied no continuation rate or date."
                )
            next_offset = {"rate": rate, "chat_id": str(last.chat_id), "id": last.id}
            if next_offset == offset:
                raise TeleloomError(
                    "pagination_stalled", "Global search did not advance its cursor."
                )
        return {"items": result, "next_offset": next_offset, "complete": not response.messages}

    async def acknowledge(
        self, chat: str, through: int, update_watermark: int | None = None
    ) -> None:
        if self.profile.kind == "bot":
            acknowledge_bot(self.store, self.profile_id, chat, through, update_watermark)
            return
        await self.client.send_read_acknowledge(await self._input_peer(chat), max_id=through)

    async def send(self, chat: str, text: str, reply_to: str | None, random_id: int) -> str:
        peer = await self._input_peer(chat)
        reply = None
        if reply_to:
            reply = types.InputReplyToMessage(reply_to_msg_id=int(reply_to))
        updates = await self.client(
            functions.messages.SendMessageRequest(
                peer=peer, message=text, random_id=random_id, reply_to=reply
            )
        )
        if getattr(updates, "id", None):
            return str(updates.id)
        for update in getattr(updates, "updates", []):
            if getattr(update, "random_id", None) == random_id and hasattr(update, "id"):
                return str(update.id)
            if hasattr(update, "message") and getattr(update.message, "out", False):
                return str(update.message.id)
        raise TeleloomError(
            "delivery_unknown", "Telegram accepted a send without an identifiable receipt."
        )


class _LimitedWriter(io.BufferedWriter):
    def __init__(self, raw: io.FileIO, max_bytes: int) -> None:
        super().__init__(raw)
        self.max_bytes = max_bytes
        self.total = 0

    def write(self, data: Any) -> int:
        if self.total + len(data) > self.max_bytes:
            raise TeleloomError(
                "attachment_too_large", "Attachment exceeds the selected byte budget."
            )
        count = super().write(data)
        self.total += count
        return count


class BotAdapter:
    async def diagnostic_read(self, chat: str) -> int:
        raise TeleloomError("unsupported_capability", "Bot history consists of saved updates.")

    async def mutate_administration(
        self,
        operation: dict[str, Any],
        random_id: int,
        receipts: list[dict[str, Any]],
        file_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        from .account_operations import bot_mutate

        return await bot_mutate(self, operation, random_id, receipts, file_bytes)

    async def drafts(self) -> list[dict[str, Any]]:
        raise TeleloomError(
            "unsupported_capability",
            "Account drafts are not provided by the current aiogram Bot API backend.",
        )

    async def mutate_message(self, operation: dict[str, Any], random_id: int) -> dict[str, Any]:
        from .mutations import bot_mutate

        return await bot_mutate(self, operation, random_id)

    async def message_state(
        self,
        chat: str,
        kind: str,
        message_id: str | None,
        limit: int,
        query: str,
        bot_id: str | None,
    ) -> dict[str, Any]:
        raise TeleloomError(
            "unsupported_capability",
            "This reader is not provided by the aiogram Bot API backend; saved-update readers remain available.",
        )

    async def download_avatar(
        self,
        chat: str,
        photo_id: str | None,
        destination: Path,
        *,
        max_bytes: int,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        from .administration import download_avatar

        return await download_avatar(
            self, chat, photo_id, destination, max_bytes=max_bytes, message_id=message_id
        )

    async def administration_read(self, operation: dict[str, Any], offset: int) -> dict[str, Any]:
        from .administration import bot_read

        return await bot_read(self, operation, offset)

    async def account_read(self, operation: dict[str, Any], offset: int) -> dict[str, Any]:
        from .administration import bot_account_read

        return await bot_account_read(self, operation, offset)

    def __init__(
        self, profile_id: str, profile: Profile, store: Store, credentials: Secrets
    ) -> None:
        self.profile_id = profile_id
        self.profile = profile
        self.store = store
        proxy = credentials.get(profile_id, "proxy")
        self.bot = Bot(
            credentials.require(profile_id, "bot_token"), session=AiohttpSession(proxy=proxy)
        )
        self.poller: asyncio.Task[None] | None = None

    async def start(self) -> None:
        user = await self.bot.get_me()
        if self.profile.identity.get("id") and str(user.id) != self.profile.identity["id"]:
            raise TeleloomError(
                "account_changed",
                "Bot token identity differs from this configured profile. Authenticate explicitly with --replace.",
            )
        if self.profile.polling:
            webhook = await self.bot.get_webhook_info()
            if webhook.url:
                raise TeleloomError(
                    "webhook_conflict",
                    "This bot already has a webhook. Use another bot or disable polling here.",
                )
            self.poller = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        key = f"bot_offset:{self.profile_id}"
        while True:
            try:
                offset = self.store.state(key, 0)
                updates = await self.bot.get_updates(
                    offset=offset,
                    timeout=20,
                    allowed_updates=[
                        "message",
                        "edited_message",
                        "channel_post",
                        "edited_channel_post",
                        "message_reaction_count",
                    ],
                )
                for update in updates:
                    raw = (
                        update.message
                        or update.edited_message
                        or update.channel_post
                        or update.edited_channel_post
                    )
                    if raw and self.profile.allows_read(str(raw.chat.id)):
                        chat = Chat(
                            id=str(raw.chat.id),
                            title=raw.chat.title or raw.chat.first_name or str(raw.chat.id),
                            kind="group"
                            if raw.chat.type in {"group", "supergroup"}
                            else raw.chat.type,
                            username=raw.chat.username,
                            forum=bool(getattr(raw.chat, "is_forum", False)),
                        )
                        self.store.chat(self.profile_id, chat)
                    messages = (
                        self._observed_bot_messages(raw)
                        if raw and self.profile.allows_read(str(raw.chat.id))
                        else []
                    )
                    reaction_update = getattr(update, "message_reaction_count", None)
                    if reaction_update and self.profile.allows_read(str(reaction_update.chat.id)):
                        known = self.store.messages(
                            self.profile_id,
                            str(reaction_update.chat.id),
                            ids=[reaction_update.message_id],
                            limit=1,
                        )
                        if known:
                            known[0].reactions = [
                                {
                                    **item.type.model_dump(mode="json", exclude_none=True),
                                    "count": item.total_count,
                                }
                                for item in reaction_update.reactions
                            ]
                            messages.extend(known)
                    if raw:
                        from .events import ingest

                        ingest(
                            self.store,
                            self.profile_id,
                            self.profile,
                            bot_message(self.profile_id, raw),
                            "edit"
                            if update.edited_message or update.edited_channel_post
                            else "new",
                        )
                    # Journal first: a crash can replay a deduplicated event, never skip it
                    # by advancing the Telegram offset before durable event ingestion.
                    self.store.save_messages(
                        messages,
                        (key, update.update_id + 1),
                        pending_update_id=update.update_id,
                    )
                with self.store.db:
                    self.store.set_state(
                        f"polling:{self.profile_id}",
                        {"status": "running", "last_poll": utcnow().isoformat()},
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = telegram_error(exc)
                with self.store.db:
                    self.store.set_state(
                        f"polling:{self.profile_id}",
                        {"status": "error", "code": error.code, "message": error.message},
                    )
                if error.code in {"polling_conflict", "account_restricted", "telegram_rejected"}:
                    return
                await asyncio.sleep(error.retry_after or 5)

    def _observed_bot_messages(self, raw: Any) -> list[Message]:
        message = bot_message(self.profile_id, raw)
        previous = self.store.messages(
            self.profile_id, message.chat_id, ids=[int(message.id)], limit=1
        )
        if previous:
            # Message/edited_message do not contain these separately observed
            # Bot API fields. Preserve their evidence until a newer update arrives.
            message.pinned = previous[0].pinned
            message.reactions = previous[0].reactions
        if message.reply_to_message_id and message.topic_id is None and not message.reply_external:
            parent = self.store.messages(
                self.profile_id, message.chat_id, ids=[int(message.reply_to_message_id)], limit=1
            )
            if parent and parent[0].thread_root_id:
                message.thread_root_id = parent[0].thread_root_id
        if message.topic_id:
            key = f"bot_topics:{self.profile_id}:{message.chat_id}"
            topics = self.store.state(key, {})
            item = topics.setdefault(
                message.topic_id,
                {"id": message.topic_id, "chat_id": message.chat_id, "title": None},
            )
            item["top_message_id"] = message.id
            item["date"] = message.date.isoformat()
            created = getattr(raw, "forum_topic_created", None)
            edited = getattr(raw, "forum_topic_edited", None)
            if created:
                item["title"] = created.name
                item["closed"] = False
            if edited and edited.name:
                item["title"] = edited.name
            if getattr(raw, "forum_topic_closed", None):
                item["closed"] = True
            if getattr(raw, "forum_topic_reopened", None):
                item["closed"] = False
            with self.store.db:
                self.store.set_state(key, topics)
        result = [message]
        pinned_raw = getattr(raw, "pinned_message", None)
        if pinned_raw and hasattr(pinned_raw, "text"):
            pinned = bot_message(self.profile_id, pinned_raw)
            previous_pin = self.store.messages(
                self.profile_id, pinned.chat_id, ids=[int(pinned.id)], limit=1
            )
            if previous_pin:
                pinned.reactions = previous_pin[0].reactions
            pinned.pinned = True
            result.append(pinned)
        return result

    async def close(self) -> None:
        if self.poller:
            self.poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.poller
        await self.bot.session.close()

    async def transcribe_audio(self, chat: str, message_id: int) -> dict[str, Any]:
        raise TeleloomError(
            "capability_unavailable",
            "Telegram transcribeAudio is user-only. Select an owner-enabled local or external provider.",
        )

    async def chats(self) -> list[Chat]:
        return self.store.chats(self.profile_id)

    async def folders(self) -> list[dict[str, Any]]:
        raise TeleloomError("unsupported_capability", "Telegram folders require a user profile.")

    async def global_search_batch(
        self,
        *,
        query: str,
        since: datetime | None,
        until: datetime,
        offset: dict[str, Any] | None,
        kind: str | None,
    ) -> dict[str, Any]:
        raise TeleloomError(
            "unsupported_capability", "Telegram global search requires a user profile."
        )

    async def public_chats(self, query: str, limit: int) -> list[Chat]:
        raise TeleloomError(
            "unsupported_capability", "Telegram public peer search requires a user profile."
        )

    async def context_window(self, chat: str, target: int, size: int) -> dict[str, Any]:
        central = self.store.messages(self.profile_id, chat, ids=[target], limit=1)
        if not central:
            raise TeleloomError(
                "message_unavailable", "This central message is not in the bot's saved updates."
            )
        older = (
            self.store.messages(self.profile_id, chat, before=target, limit=size + 1)
            if size
            else []
        )
        newer = (
            [
                Message.model_validate_json(row[0])
                for row in self.store.db.execute(
                    "SELECT data FROM messages WHERE profile=? AND chat=? AND deleted=0 AND CAST(id AS INTEGER)>? ORDER BY CAST(id AS INTEGER) LIMIT ?",
                    (self.profile_id, chat, target, size + 1),
                )
            ]
            if size
            else []
        )
        return {
            "items": [central[0], *older[:size], *newer[:size]],
            "has_older": len(older) > size,
            "has_newer": len(newer) > size,
        }

    async def folder_snapshot(self) -> dict[str, Any]:
        raise TeleloomError("unsupported_capability", "Telegram dialog filters are user-only.")

    async def folder_limits(self) -> dict[str, Any]:
        raise TeleloomError("unsupported_capability", "Telegram dialog filters are user-only.")

    async def contacts(self, view: str) -> dict[str, Any]:
        raise TeleloomError(
            "unsupported_capability", "Telegram address-book methods are user-only."
        )

    async def search_contacts(self, query: str) -> dict[str, Any]:
        raise TeleloomError("unsupported_capability", "Telegram contact search is user-only.")

    async def blocked_contacts(self) -> dict[str, Any]:
        raise TeleloomError(
            "unsupported_capability", "Telegram blocked-peer methods are user-only."
        )

    async def common_contact_chats(self, contact_id: str) -> dict[str, Any]:
        raise TeleloomError("unsupported_capability", "Telegram common-chat lookup is user-only.")

    async def resolve(self, target: str) -> Chat:
        exact = [
            chat
            for chat in self.store.chats(self.profile_id)
            if chat.id == target
            or chat.title == target
            or (chat.username and "@" + chat.username.lower() == target.lower())
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise TeleloomError(
                "ambiguous_chat",
                "Use an exact ID or @username.",
                details={"candidates": [c.model_dump() for c in exact]},
            )
        if not target.startswith("@") and not target.lstrip("-").isdigit():
            raise TeleloomError(
                "chat_not_found", "Bots know only observed chats; use a known ID or @username."
            )
        raw = await self.bot.get_chat(target)
        chat = Chat(
            id=str(raw.id),
            title=raw.title or raw.first_name or target,
            kind="group" if raw.type in {"group", "supergroup"} else raw.type,
            username=raw.username,
            forum=bool(getattr(raw, "is_forum", False)),
        )
        if self.profile.allows_read(chat.id):
            self.store.chat(self.profile_id, chat)
        return chat

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
        return self.store.messages(
            self.profile_id,
            chat,
            before=before,
            since=since.isoformat() if since else None,
            until=until.isoformat() if until else None,
            limit=limit,
            query=query,
            ids=ids,
        )

    async def history_batch(
        self,
        chat: str,
        *,
        before: int | None,
        since: datetime | None,
        until: datetime | None,
        query: str | None = None,
    ) -> dict[str, Any]:
        rows = await self.history(
            chat, before=before, since=since, until=until, query=query, limit=101
        )
        complete = len(rows) <= 100
        selected = rows[:100]
        return {
            "items": selected,
            "next_before": None if complete else int(selected[-1].id),
            "complete": complete,
        }

    async def topics(
        self, chat: str, *, offset: dict[str, Any] | None, limit: int
    ) -> dict[str, Any]:
        observed = self.store.state(f"bot_topics:{self.profile_id}:{chat}", {})
        rows = self.store.db.execute(
            "SELECT DISTINCT json_extract(data,'$.topic_id') FROM messages "
            "WHERE profile=? AND chat=? AND deleted=0 AND json_extract(data,'$.topic_id') IS NOT NULL",
            (self.profile_id, chat),
        )
        for row in rows:
            observed.setdefault(row[0], {"id": row[0], "chat_id": chat, "title": None})
        ordered = sorted(observed.values(), key=lambda item: int(item["id"]))
        after = int((offset or {}).get("topic", 0))
        remaining = [item for item in ordered if int(item["id"]) > after]
        selected = remaining[:limit]
        return {
            "items": selected,
            "next_offset": {"topic": selected[-1]["id"]} if len(remaining) > limit else None,
        }

    async def thread(
        self, chat: str, root: int, *, before: int | None, limit: int
    ) -> list[Message]:
        rows = self.store.db.execute(
            "SELECT data FROM messages WHERE profile=? AND chat=? AND deleted=0 "
            "AND (json_extract(data,'$.thread_root_id')=? OR json_extract(data,'$.topic_id')=? "
            "OR id=?) AND (? IS NULL OR CAST(id AS INTEGER)<?) ORDER BY CAST(id AS INTEGER) DESC LIMIT ?",
            (self.profile_id, chat, str(root), str(root), str(root), before, before, limit),
        )
        return [Message.model_validate_json(row[0]) for row in rows]

    async def discussion(self, chat: str, post: int) -> dict[str, Any]:
        rows = self.store.db.execute(
            "SELECT data FROM messages WHERE profile=? AND deleted=0 "
            "AND json_extract(data,'$.forwarded_from.sender_id')=? "
            "AND json_extract(data,'$.forwarded_from.message_id')=? "
            "AND json_extract(data,'$.forwarded_from.automatic')=1 "
            "ORDER BY CAST(id AS INTEGER) LIMIT 1",
            (self.profile_id, chat, str(post)),
        ).fetchall()
        if not rows:
            raise TeleloomError(
                "unsupported_capability",
                "Bots cannot resolve historical discussion roots; only observed automatic-forward updates can map comments.",
            )
        root = Message.model_validate_json(rows[0][0])
        return {
            "chat_id": root.chat_id,
            "root_id": root.id,
            "original_status": "observed_updates_only",
        }

    async def thread_batch(
        self, chat: str, root: int, *, before: int | None, limit: int
    ) -> dict[str, Any]:
        rows = await self.thread(chat, root, before=before, limit=limit + 1)
        selected = rows[:limit]
        return {
            "items": selected,
            "next_before": int(selected[-1].id) if len(rows) > limit else None,
            "complete": len(rows) <= limit,
        }

    async def pinned(self, chat: str, *, before: int | None, limit: int) -> list[Message]:
        rows = self.store.db.execute(
            "SELECT data FROM messages WHERE profile=? AND chat=? AND deleted=0 "
            "AND json_extract(data,'$.pinned')=1 AND (? IS NULL OR CAST(id AS INTEGER)<?) "
            "ORDER BY CAST(id AS INTEGER) DESC LIMIT ?",
            (self.profile_id, chat, before, before, limit),
        )
        return [Message.model_validate_json(row[0]) for row in rows]

    async def pinned_batch(self, chat: str, *, before: int | None, limit: int) -> dict[str, Any]:
        rows = await self.pinned(chat, before=before, limit=limit + 1)
        selected = rows[:limit]
        return {
            "items": selected,
            "next_before": int(selected[-1].id) if len(rows) > limit else None,
            "complete": len(rows) <= limit,
        }

    async def download_attachment(
        self, chat: str, message_id: int, destination: Path, *, max_bytes: int
    ) -> dict[str, Any]:
        selected = self.store.messages(self.profile_id, chat, ids=[message_id], limit=1)
        if not selected:
            raise TeleloomError(
                "message_unavailable",
                "Bots can download only attachments from their saved updates.",
            )
        media = selected[0].media or {}
        if not media.get("file_id"):
            raise TeleloomError(
                "attachment_unavailable",
                "The selected saved update has no downloadable attachment.",
            )
        if media.get("size") is not None and media["size"] > max_bytes:
            raise TeleloomError(
                "attachment_too_large", "Attachment exceeds the selected byte budget."
            )
        remote = await self.bot.get_file(media["file_id"])
        if remote.file_size is not None and remote.file_size > max_bytes:
            raise TeleloomError(
                "attachment_too_large", "Attachment exceeds the selected byte budget."
            )
        if not remote.file_path:
            raise TeleloomError(
                "attachment_unavailable",
                "Telegram did not provide a path for the selected attachment.",
            )
        created = False
        try:
            output = _LimitedWriter(io.FileIO(destination, "x"), max_bytes)
            created = True
            with output:
                await self.bot.download_file(remote.file_path, destination=output, chunk_size=65536)
                total = output.total
            expected = remote.file_size if remote.file_size is not None else media.get("size")
            if expected is not None and total != expected:
                raise TeleloomError(
                    "attachment_incomplete",
                    "The selected attachment could not be downloaded completely.",
                )
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        return {"size": total, "name": media.get("name"), "mime_type": media.get("mime_type")}

    async def acknowledge(
        self, chat: str, through: int, update_watermark: int | None = None
    ) -> None:
        acknowledge_bot(self.store, self.profile_id, chat, through, update_watermark)

    async def send(self, chat: str, text: str, reply_to: str | None, random_id: int) -> str:
        kwargs: dict[str, Any] = {}
        if reply_to:
            from aiogram.types import ReplyParameters

            kwargs["reply_parameters"] = ReplyParameters(message_id=int(reply_to))
        raw = await self.bot.send_message(int(chat), text, **kwargs)
        message = bot_message(self.profile_id, raw)
        message.outgoing = True
        self.store.save_messages([message])
        return str(raw.message_id)


def make_adapter(profile_id: str, profile: Profile, store: Store, credentials: Secrets) -> Adapter:
    if profile.kind == "bot" and profile.bot_backend == "mtproto":
        from .bot_mtproto import MTProtoBotAdapter

        return MTProtoBotAdapter(profile_id, profile, store, credentials)
    return (
        UserAdapter(profile_id, profile, store, credentials)
        if profile.kind == "user"
        else BotAdapter(profile_id, profile, store, credentials)
    )
