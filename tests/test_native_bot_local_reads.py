import tempfile
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot, methods
from aiogram import types as bot_types
from aiogram.client.session.base import BaseSession
from pytest import MonkeyPatch
from telethon import TelegramClient, errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from teleloom.models import Chat, Message
from teleloom.store import Store
from tests.fakes import data
from tests.test_transport import client, running

GROUP = "-1000000000123"
PROFILE = "robot"
NOW = datetime.now(UTC)
FORBIDDEN = (
    functions.messages.GetHistoryRequest,
    functions.messages.GetForumTopicsRequest,
    functions.messages.SearchRequest,
    functions.messages.GetRepliesRequest,
    functions.messages.GetDiscussionMessageRequest,
)


class NativeSDK(TelegramClient):
    def __init__(self):
        saved = StringSession()
        saved.auth_key = AuthKey(b"review native bot fake key".ljust(256, b"0"))
        super().__init__(saved, 1, "synthetic-api-hash")
        self.calls = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return types.User(id=7, first_name="Synthetic bot", bot=True)

    async def get_input_entity(self, target):
        assert target == int(GROUP)
        return types.InputPeerChannel(123, 999)

    async def get_entity(self, target):
        assert isinstance(target, types.InputPeerChannel)
        return types.Channel(
            id=123,
            title="Collected forum",
            date=NOW,
            photo=types.ChatPhotoEmpty(),
            megagroup=True,
            forum=True,
        )

    async def __call__(self, request, *args, **kwargs):
        bytes(request)
        self.calls.append(request)
        if isinstance(request, FORBIDDEN):
            # These are officially user-only methods. Bot local fallbacks must
            # never invoke them, even when native exact-ID reads are available.
            raise errors.BotMethodInvalidError(request)
        assert isinstance(request, functions.channels.GetMessagesRequest), type(request)
        return types.messages.Messages(
            messages=[
                types.Message(
                    id=item.id,
                    peer_id=types.PeerChannel(123),
                    date=NOW,
                    message=f"Exact native {item.id}",
                )
                for item in request.id
            ],
            chats=[],
            users=[],
            topics=[],
        )


class BotAPI(BaseSession):
    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        assert isinstance(method, methods.GetMe), type(method)
        return bot_types.User(id=7, is_bot=True, first_name="Synthetic bot")


def seed(root):
    store = Store(root)
    store.chat(PROFILE, Chat(id=GROUP, title="Collected", kind="group", forum=True))
    store.save_messages(
        [
            Message(
                profile_id=PROFILE,
                chat_id=GROUP,
                id=str(identifier),
                date=NOW,
                text=str(identifier),
                topic_id="5",
                pinned=identifier == 80,
                thread_root_id="88" if identifier == 91 else None,
                forwarded_from={"sender_id": GROUP, "message_id": "88", "automatic": True}
                if identifier == 90
                else None,
            )
            for identifier in (80, 88, 90, 91)
        ],
        pending_update_id=100,
    )
    with store.db:
        store.set_state(f"bot_offset:{PROFILE}", 101)
    store.close()


async def probe(backend):
    observations = []
    with tempfile.TemporaryDirectory() as temp, MonkeyPatch.context() as patch:
        root = Path(temp)
        sdk = NativeSDK()
        patch.setattr("teleloom.adapters.user_client", lambda *args, **kwargs: sdk)
        patch.setenv("TELELOOM_ROBOT_BOT_TOKEN", "123456:" + "A" * 35)
        patch.setattr("teleloom.adapters.Bot", lambda token, session: Bot(token, session=BotAPI()))
        settings = Settings(
            data_dir=root,
            profiles={
                PROFILE: Profile(
                    kind="bot",
                    bot_backend=backend,
                    polling=False,
                    identity={"id": "7"},
                    read_mode="selected",
                    read_chats=[GROUP],
                )
            },
        )
        seed(root)
        requests = [
            ("messages_get", {}),
            ("inbox_get", {}),
            ("context_get", {"message_id": "88", "context_size": 1}),
            ("topics_list", {}),
            ("topic_history", {"topic_id": "5"}),
            ("thread_get", {"root_message_id": "88"}),
            ("messages_pinned", {}),
            ("comments_get", {"message_id": "88"}),
        ]
        async with running(settings, make_adapter) as app, client(app, settings) as mcp:
            for name, extra in requests:
                arguments = {"profile_id": PROFILE, "chat_id": GROUP, **extra}
                if name == "inbox_get":
                    arguments = {"profile_id": PROFILE}
                before = len(sdk.calls)
                result = data(await mcp.call_tool(name, arguments))
                body = result.get("data", {})
                observation = {
                    "backend": backend,
                    "tool": name,
                    "ok": result["ok"],
                    "error": (result.get("error") or {}).get("code"),
                    "ids": [item.get("id") for item in body.get("items", [])],
                    "inbox_ids": [
                        item["id"]
                        for chat in body.get("chats", [])
                        for item in chat.get("messages", [])
                    ],
                    "source": body.get("source"),
                    "rpc": [type(request).__name__ for request in sdk.calls[before:]],
                }
                observations.append(observation)
            if backend == "mtproto":
                # Exact-ID native retrieval must remain available after local
                # method reuse; it is an intentional distinct workflow.
                before = len(sdk.calls)
                exact = data(
                    await mcp.call_tool(
                        "messages_get",
                        {"profile_id": PROFILE, "chat_id": GROUP, "message_ids": ["88"]},
                    )
                )
                assert exact["ok"], exact
                assert [item["id"] for item in exact["data"]["items"]] == ["88"]
                assert exact["data"]["source"] == "telegram"
                assert len(sdk.calls) == before + 1
                assert isinstance(sdk.calls[-1], functions.channels.GetMessagesRequest)
    return observations


async def test_both_bot_backends_keep_collected_reads_local_and_native_exact_ids_available():
    api = await probe("bot_api")
    native = await probe("mtproto")
    assert all(item["ok"] and not item["rpc"] for item in api), api
    assert all(item["ok"] and not item["rpc"] for item in native), native
    assert next(item for item in api if item["tool"] == "inbox_get")["inbox_ids"] == [
        "91",
        "90",
        "88",
        "80",
    ]
    assert [item["ids"] for item in native] == [item["ids"] for item in api]
    assert [item["inbox_ids"] for item in native] == [item["inbox_ids"] for item in api]
    assert [item["source"] for item in native] == [item["source"] for item in api]
