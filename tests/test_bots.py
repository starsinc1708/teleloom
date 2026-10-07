import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot as AiogramBot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetChat, GetMe, GetUpdates, GetWebhookInfo
from aiogram.types import Chat, Message, Update, User, WebhookInfo

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.test_transport import client, running


class BotAPI(BaseSession):
    def __init__(self, webhook=False):
        super().__init__()
        self.webhook = webhook
        self.requests = []
        self.polls = 0

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, GetWebhookInfo):
            return WebhookInfo(
                url="https://existing.invalid/hook" if self.webhook else "",
                has_custom_certificate=False,
                pending_update_count=0,
            )
        if isinstance(method, GetChat):
            return Chat(id=100, type="private", first_name="Owner")
        if isinstance(method, GetUpdates):
            self.polls += 1
            if self.polls > 1:
                raise TelegramConflictError(method, "Another consumer")
            return [
                Update(
                    update_id=9,
                    message=Message(
                        message_id=4,
                        date=datetime.now(UTC),
                        chat=Chat(id=100, type="private", first_name="Owner"),
                        from_user=User(id=42, is_bot=False, first_name="Owner"),
                        text="Observed message",
                    ),
                )
            ]
        raise AssertionError(type(method).__name__)


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_before_ack", [False, True])
async def test_edit_of_acknowledged_bot_message_becomes_unprocessed_again(
    monkeypatch, tmp_path, edit_before_ack
):
    from teleloom.adapters import make_adapter

    edit = asyncio.Event()

    class EditingBotAPI(BotAPI):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetUpdates) and self.polls == 1:
                self.requests.append(method)
                self.polls += 1
                await edit.wait()
                return [
                    Update(
                        update_id=10,
                        edited_message=Message(
                            message_id=4,
                            date=datetime.now(UTC),
                            edit_date=int(datetime.now(UTC).timestamp()),
                            chat=Chat(id=100, type="private", first_name="Owner"),
                            from_user=User(id=42, is_bot=False, first_name="Owner"),
                            text="Changed decision",
                        ),
                    )
                ]
            return await super().make_request(bot, method, timeout)

    api = EditingBotAPI()
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr(
        "teleloom.adapters.Bot", lambda token, session: AiogramBot(token, session=api)
    )
    settings = Settings(data_dir=tmp_path, profiles={"helper": Profile(kind="bot", polling=True)})
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        await asyncio.sleep(0.05)
        initial = data(await session.call_tool("inbox_get", {"profile_id": "helper"}))["data"]
        if edit_before_ack:
            edit.set()
            await asyncio.sleep(0.05)
        ack = data(
            await session.call_tool(
                "inbox_ack",
                {
                    "profile_id": "helper",
                    "chat_id": "100",
                    "through_message_id": "4",
                    "snapshot_id": initial["chats"][0]["snapshot_id"],
                },
            )
        )
        assert ack["ok"]
        edit.set()
        await asyncio.sleep(0.05)
        inbox = data(await session.call_tool("inbox_get", {"profile_id": "helper"}))["data"]
        assert inbox["chats"][0]["messages"][0]["text"] == "Changed decision"
        await session.call_tool(
            "inbox_ack",
            {
                "profile_id": "helper",
                "chat_id": "100",
                "through_message_id": "4",
                "snapshot_id": inbox["chats"][0]["snapshot_id"],
            },
        )
        assert not data(await session.call_tool("inbox_get", {"profile_id": "helper"}))["data"][
            "chats"
        ]


@pytest.mark.asyncio
async def test_bot_updates_persist_before_offset_and_local_ack(monkeypatch, tmp_path):
    api = BotAPI()
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr(
        "teleloom.adapters.Bot", lambda token, session: AiogramBot(token, session=api)
    )
    settings = Settings(data_dir=tmp_path, profiles={"helper": Profile(kind="bot", polling=True)})
    async with running(
        settings, __import__("teleloom.adapters", fromlist=["make_adapter"]).make_adapter
    ) as app:
        async with client(app, settings) as session:
            await asyncio.sleep(0.05)
            inbox = data(await session.call_tool("inbox_get", {"profile_id": "helper"}))["data"]
            assert inbox["source"] == "local_unprocessed"
            assert inbox["chats"][0]["messages"][0]["text"] == "Observed message"
            polls = [r for r in api.requests if isinstance(r, GetUpdates)]
            assert polls[1].offset == 10
            ack = data(
                await session.call_tool(
                    "inbox_ack",
                    {
                        "profile_id": "helper",
                        "chat_id": "100",
                        "through_message_id": "4",
                        "snapshot_id": inbox["chats"][0]["snapshot_id"],
                    },
                )
            )
            assert ack["ok"]
            assert not data(await session.call_tool("inbox_get", {"profile_id": "helper"}))["data"][
                "chats"
            ]
            profiles = data(await session.call_tool("profiles_list", {}))["data"]["profiles"]
            assert profiles[0]["polling"]["code"] == "polling_conflict"


@pytest.mark.asyncio
async def test_existing_webhook_is_reported_without_removal(monkeypatch, tmp_path):
    api = BotAPI(webhook=True)
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr(
        "teleloom.adapters.Bot", lambda token, session: AiogramBot(token, session=api)
    )
    settings = Settings(data_dir=tmp_path, profiles={"helper": Profile(kind="bot", polling=True)})
    async with running(
        settings, __import__("teleloom.adapters", fromlist=["make_adapter"]).make_adapter
    ) as app:
        async with client(app, settings) as session:
            await asyncio.sleep(0.05)
            profiles = data(await session.call_tool("profiles_list", {}))["data"]["profiles"]
            assert profiles[0]["error"] == "webhook_conflict"
    assert [type(request).__name__ for request in api.requests] == ["GetMe", "GetWebhookInfo"]


@pytest.mark.asyncio
async def test_bot_proxy_support_is_installed_and_configured(monkeypatch, tmp_path):
    from teleloom.adapters import make_adapter

    api = BotAPI()
    proxies = []

    def external_api(token, session):
        proxies.append(session.proxy)
        return AiogramBot(token, session=api)

    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setenv("TELELOOM_HELPER_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setattr("teleloom.adapters.Bot", external_api)
    settings = Settings(data_dir=tmp_path, profiles={"helper": Profile(kind="bot")})
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        history = data(
            await session.call_tool("chat_resolve", {"profile_id": "helper", "target": "100"})
        )
        assert history["ok"], history
    assert proxies == ["socks5://127.0.0.1:1080"]


@pytest.mark.asyncio
async def test_mismatched_bot_identity_is_rejected_and_diagnostic_is_retained(
    monkeypatch, tmp_path
):
    from teleloom.adapters import make_adapter

    api = BotAPI()
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr(
        "teleloom.adapters.Bot", lambda token, session: AiogramBot(token, session=api)
    )
    settings = Settings(
        data_dir=tmp_path, profiles={"helper": Profile(kind="bot", identity={"id": "999"})}
    )
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        rejected = data(await session.call_tool("chats_list", {"profile_id": "helper"}))
        assert rejected["error"]["code"] == "account_changed"
        profiles = data(await session.call_tool("profiles_list", {}))["data"]["profiles"]
        assert not profiles[0]["connected"]
        assert profiles[0]["error"] == "account_changed"


@pytest.mark.asyncio
async def test_expired_bot_snapshot_rejects_ack_without_clearing_pending_updates(
    monkeypatch, tmp_path
):
    from teleloom.adapters import make_adapter

    api = BotAPI()
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr(
        "teleloom.adapters.Bot", lambda token, session: AiogramBot(token, session=api)
    )
    now = datetime(2025, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("teleloom.runtime.utcnow", lambda: now)
    settings = Settings(data_dir=tmp_path, profiles={"helper": Profile(kind="bot", polling=True)})
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        reviewed = data(await session.call_tool("inbox_get", {"profile_id": "helper"}))["data"]
        assert reviewed["chats"][0]["messages"][0]["id"] == "4"
        now += timedelta(minutes=15)
        rejected = data(
            await session.call_tool(
                "inbox_ack",
                {
                    "profile_id": "helper",
                    "chat_id": "100",
                    "through_message_id": "4",
                    "snapshot_id": reviewed["chats"][0]["snapshot_id"],
                },
            )
        )
        assert rejected["error"]["code"] == "snapshot_required"
        remaining = data(await session.call_tool("inbox_get", {"profile_id": "helper"}))["data"]
        assert remaining["chats"][0]["messages"] == reviewed["chats"][0]["messages"]
