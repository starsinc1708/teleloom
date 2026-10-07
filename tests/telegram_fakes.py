"""Shared fake Telegram SDK records/lifecycle; every store and owner stays per test."""

from datetime import UTC, datetime, timedelta

from telethon import TelegramClient, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from teleloom.adapters import BotAdapter, UserAdapter
from teleloom.config import Profile
from teleloom.store import Store

NOW = datetime(2026, 10, 5, tzinfo=UTC)
CHAT = "-1000000000100"


class SDK(TelegramClient):
    """Fake external SDK; exercises the real adapter, store and MCP transport."""

    def __init__(self):
        saved = StringSession()
        saved.auth_key = AuthKey(b"r" * 256)
        super().__init__(saved, 1, "synthetic-api-hash")
        self.rows = []
        self.dialogs = []
        self.calls = []
        self.response = None

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return types.User(id=1, first_name="Owner")

    async def get_input_entity(self, value):
        return types.InputPeerChannel(100, 123456789)

    async def get_entity(self, value):
        return types.Channel(
            id=100, title="Selected channel", date=NOW, photo=types.ChatPhotoEmpty(), broadcast=True
        )

    async def iter_messages(self, peer, **kwargs):
        self.calls.append(kwargs)
        for raw in self.rows:
            if kwargs.get("ids") and raw.id not in kwargs["ids"]:
                continue
            if isinstance(kwargs.get("filter"), types.InputMessagesFilterPinned) and not raw.pinned:
                continue
            if not kwargs.get("offset_id") or raw.id < kwargs["offset_id"]:
                yield raw

    async def get_messages(self, peer, *, ids):
        self.calls.append({"selected_id": ids})
        return next((raw for raw in self.rows if raw.id == ids), None)

    async def iter_dialogs(self, **kwargs):
        assert not kwargs, "Archived dialogs must stay in the evaluated snapshot"
        for dialog in self.dialogs:
            yield dialog

    async def __call__(self, request, *args, **kwargs):
        self.calls.append(request)
        assert isinstance(
            request,
            (
                functions.messages.GetDialogFiltersRequest,
                functions.account.GetNotifySettingsRequest,
                functions.messages.GetForumTopicsRequest,
                functions.messages.GetRepliesRequest,
                functions.messages.GetDiscussionMessageRequest,
                functions.messages.GetHistoryRequest,
                functions.messages.SearchRequest,
            ),
        ), "Only read requests are allowed"
        return self.response


def factory(sdk):
    def make(profile_id, profile, store, credentials):
        adapter = object.__new__(UserAdapter)
        adapter.profile_id = profile_id
        adapter.profile = profile
        adapter.store = store
        adapter.client = sdk
        return adapter

    return make


def adapter_for(sdk, tmp_path):
    store = Store(tmp_path)
    return factory(sdk)("personal", Profile(kind="user"), store, None)


def topic(id_, top):
    return types.ForumTopic(
        id=id_,
        date=NOW - timedelta(days=1),
        peer=types.PeerChannel(100),
        title=f"Topic {id_}",
        icon_color=0,
        top_message=top,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=types.PeerUser(7),
        notify_settings=types.PeerNotifySettings(),
    )


def bot_adapter_for(tmp_path):
    adapter = object.__new__(BotAdapter)
    adapter.profile_id = "helper"
    adapter.profile = Profile(kind="bot")
    adapter.store = Store(tmp_path)
    return adapter


def bot_raw(**kwargs):
    from aiogram.types import Message as BotMessage

    values = {
        "message_id": 11,
        "date": NOW,
        "chat": {"id": int(CHAT), "type": "supergroup", "title": "Forum", "is_forum": True},
        "from": {"id": 7, "is_bot": False, "first_name": "Ada", "username": "ada"},
    }
    values.update(kwargs)
    return BotMessage.model_validate(values)
