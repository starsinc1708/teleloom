from datetime import UTC, datetime

import pytest
from telethon import TelegramClient, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.test_transport import client, running


@pytest.fixture
def telegram_session(monkeypatch, tmp_path):
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(b"s" * 256)
    monkeypatch.setenv("TELELOOM_WORK_SESSION", session.save())
    monkeypatch.setenv("TELELOOM_WORK_API_HASH", "fake-api-hash")
    state = {"connections": 0, "clients": []}

    class ExternalTelegram(TelegramClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state["clients"].append(self)

        async def connect(self):
            state["connections"] += 1

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return types.User(id=1, first_name="Owner")

        async def get_entity(self, target):
            if target == "@observed":
                entity = types.User(
                    id=100, access_hash=333, first_name="Observed", username="observed"
                )
                self.session.process_entities([entity])
                self.session.set_update_state(
                    0,
                    types.updates.State(
                        pts=7, qts=0, date=datetime(2026, 1, 1, tzinfo=UTC), seq=9, unread_count=0
                    ),
                )
                return entity
            peer = self.session.get_input_entity(target)
            assert peer.user_id == 100 and peer.access_hash == 333
            assert self.session.get_update_state(0).pts == 7
            return types.User(id=100, access_hash=333, first_name="Observed")

    monkeypatch.setattr("teleloom.adapters.TelegramClient", ExternalTelegram)
    return state


def settings(directory):
    return Settings(
        data_dir=directory,
        profiles={"work": Profile(kind="user", api_id=123, generation="same-account")},
    )


@pytest.mark.asyncio
async def test_one_telegram_auth_session_is_locked_across_data_directories(
    tmp_path, telegram_session
):
    first, second = settings(tmp_path / "one"), settings(tmp_path / "two")
    async with running(first, make_adapter) as owner:
        async with client(owner, first) as session:
            result = data(
                await session.call_tool(
                    "chat_resolve", {"profile_id": "work", "target": "@observed"}
                )
            )
            assert result["ok"], result
        async with running(second, make_adapter) as other:
            async with client(other, second) as session:
                denied = data(
                    await session.call_tool(
                        "chat_resolve", {"profile_id": "work", "target": "@observed"}
                    )
                )
                assert denied["error"]["code"] == "session_in_use"
                assert telegram_session["connections"] == 1
    async with running(second, make_adapter) as other:
        async with client(other, second) as session:
            assert data(
                await session.call_tool(
                    "chat_resolve", {"profile_id": "work", "target": "@observed"}
                )
            )["ok"]


@pytest.mark.asyncio
async def test_sdk_entity_and_update_state_survive_restart_without_plaintext_auth_key(
    tmp_path, telegram_session
):
    config = settings(tmp_path)
    async with running(config, make_adapter) as owner:
        async with client(owner, config) as session:
            assert data(
                await session.call_tool(
                    "chat_resolve", {"profile_id": "work", "target": "@observed"}
                )
            )["ok"]
    async with running(config, make_adapter) as owner:
        async with client(owner, config) as session:
            result = data(
                await session.call_tool("chat_resolve", {"profile_id": "work", "target": "100"})
            )
            assert result["ok"], result
            assert result["data"]["id"] == "100"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert b"s" * 256 not in content
            assert b"fake-api-hash" not in content
