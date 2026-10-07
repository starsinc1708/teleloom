"""Explicitly provisioned MTProto bot; credentials/session stay under the local owner."""

from datetime import datetime
from typing import Any

from telethon import functions, types, utils

from .adapters import BotAdapter, UserAdapter
from .models import Chat, Message, TeleloomError, utcnow
from .telegram.evidence import telethon_message


class MTProtoBotAdapter(UserAdapter):
    # Bot acknowledgment consumes only reviewed local update watermarks.
    acknowledge = BotAdapter.acknowledge
    # User history/search/forum RPCs are forbidden for bots. Keep collected reads local.
    context_window = BotAdapter.context_window
    topics = BotAdapter.topics
    thread = BotAdapter.thread
    thread_batch = BotAdapter.thread_batch
    pinned = BotAdapter.pinned
    pinned_batch = BotAdapter.pinned_batch
    discussion = BotAdapter.discussion

    async def start(self) -> None:
        await super().start()
        if self.profile.polling:
            with self.store.db:
                self.store.set_state(
                    f"polling:{self.profile_id}",
                    {
                        "status": "running",
                        "source": "mtproto_updates",
                        "connected_at": utcnow().isoformat(),
                    },
                )

    async def chats(self) -> list[Chat]:
        # Telegram getDialogs is user-only. Bot peers are collected updates and exact resolutions.
        return self.store.chats(self.profile_id)

    async def folders(self) -> list[dict[str, Any]]:
        raise TeleloomError(
            "platform_restriction", "Telegram dialog folders require a user account."
        )

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
        if not ids:
            raise TeleloomError(
                "platform_restriction",
                "Telegram getHistory is user-only. MTProto bots can fetch explicit message_ids and read saved updates.",
            )
        peer = await self._input_peer(chat)
        request = (
            functions.channels.GetMessagesRequest(
                channel=peer, id=[types.InputMessageID(x) for x in ids]
            )
            if isinstance(peer, types.InputPeerChannel)
            else functions.messages.GetMessagesRequest(id=[types.InputMessageID(x) for x in ids])
        )
        response = await self.client(request)
        entities = {utils.get_peer_id(x): x for x in response.users + response.chats}
        result = []
        for raw in response.messages:
            if not getattr(raw, "date", None) or str(raw.chat_id) != chat or raw.id not in ids:
                continue
            if (
                (before is not None and raw.id >= before)
                or (since and raw.date < since)
                or (until and raw.date >= until)
            ):
                continue
            result.append(
                telethon_message(
                    self.profile_id,
                    raw,
                    sender=entities.get(raw.sender_id),
                    chat_entity=entities.get(raw.chat_id),
                )
            )
        return sorted(result, key=lambda x: int(x.id), reverse=True)[:limit]

    async def history_batch(
        self,
        chat: str,
        *,
        before: int | None,
        since: datetime | None,
        until: datetime | None,
        query: str | None = None,
    ) -> dict[str, Any]:
        raise TeleloomError(
            "platform_restriction",
            "Telegram getHistory is user-only; synchronize collected bot updates instead.",
        )

    async def administration_read(self, operation: dict[str, Any], offset: int) -> dict[str, Any]:
        if operation["kind"] in {"common_chats", "audit"}:
            raise TeleloomError(
                "platform_restriction",
                "Telegram common-chat and audit readers require a user account.",
            )
        return await super().administration_read(operation, offset)
