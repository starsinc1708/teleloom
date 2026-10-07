from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon import TelegramClient, errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.fixture
def telegram_peers(monkeypatch):
    state = {
        "username_id": 123,
        "username_missing": False,
        "lookup_error": False,
        "dialog": False,
        "sent": [],
        "clients": [],
        "acknowledged": [],
        "expire_peer": False,
    }

    class TelegramPeers(TelegramClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state["clients"].append(self)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return types.User(id=1, first_name="Test owner")

        async def get_input_entity(self, peer):
            if state["expire_peer"] and isinstance(peer, int):
                raise ValueError("Synthetic evicted peer")
            return await super().get_input_entity(peer)

        async def iter_dialogs(self):
            if state["dialog"]:
                yield SimpleNamespace(
                    id=123,
                    entity=types.User(id=123, access_hash=987, first_name="Original target"),
                )

        async def __call__(self, request, *args, **kwargs):
            user = types.User(
                id=state["username_id"],
                access_hash=987,
                first_name="Test target",
                username="test_target",
            )
            if isinstance(request, functions.contacts.ResolveUsernameRequest):
                if state["lookup_error"]:
                    raise ConnectionError("Synthetic lost peer lookup response")
                if state["username_missing"]:
                    raise errors.UsernameNotOccupiedError(request)
                result = types.contacts.ResolvedPeer(
                    peer=types.PeerUser(user.id), users=[user], chats=[]
                )
            elif isinstance(request, functions.users.GetUsersRequest):
                result = [
                    user if item.access_hash == 987 else types.UserEmpty(id=item.user_id)
                    for item in request.id
                ]
            elif isinstance(request, functions.messages.GetHistoryRequest):
                assert request.peer.user_id == 123
                result = types.messages.Messages(messages=[], users=[], chats=[], topics=[])
            elif isinstance(request, functions.messages.GetMessagesRequest):
                result = types.messages.Messages(
                    messages=[
                        types.Message(
                            id=5,
                            peer_id=types.PeerUser(123),
                            date=datetime(2025, 1, 1, tzinfo=UTC),
                            message="Reviewed checkpoint",
                        )
                    ],
                    users=[user],
                    chats=[],
                    topics=[],
                )
            elif isinstance(request, functions.messages.ReadHistoryRequest):
                state["acknowledged"].append((request.peer.user_id, request.max_id))
                result = types.messages.AffectedMessages(pts=1, pts_count=1)
            elif isinstance(request, functions.messages.SendMessageRequest):
                state["sent"].append(request.peer.user_id)
                result = types.UpdateShortSentMessage(
                    id=10, pts=1, pts_count=1, date=datetime(2025, 1, 1, tzinfo=UTC), out=True
                )
            else:
                raise AssertionError(type(request).__name__)
            self.session.process_entities(result)
            return result

    session = telegram_peers_session()
    monkeypatch.setenv("TELELOOM_PERSONAL_SESSION", session.save())
    monkeypatch.setenv("TELELOOM_PERSONAL_API_HASH", "synthetic-api-hash")
    monkeypatch.setattr("teleloom.adapters.TelegramClient", TelegramPeers)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("removed_username", [False, True])
async def test_cached_peer_is_restored_for_read_preview_and_delivery_after_restart(
    tmp_path, telegram_peers, removed_username
):
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", api_id=1, send_chats=["123"])},
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        resolved = data(
            await mcp.call_tool(
                "chat_resolve", {"profile_id": "personal", "target": "@test_target"}
            )
        )
        assert resolved["ok"]
        assert resolved["data"]["id"] == "123"

    telegram_peers["username_missing"] = removed_username
    telegram_peers["dialog"] = removed_username
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        history = data(
            await mcp.call_tool("messages_get", {"profile_id": "personal", "chat_id": "123"})
        )
        assert history["ok"], history["error"]
        preview = data(
            await mcp.call_tool(
                "delivery_preview",
                {"profile_id": "personal", "recipients": ["123"], "text": "Synthetic test"},
            )
        )
        assert preview["ok"], preview["error"]
        executed = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": preview["data"]["plan_id"],
                    "plan_hash": preview["data"]["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        finished = await complete(mcp, "personal", executed["data"]["job_id"])
        assert finished["status"] == "completed"
        assert finished["deliveries"][0]["message_id"] == "10"
    assert len(telegram_peers["clients"]) == 2
    assert telegram_peers["sent"] == [123]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["changed", "missing", "network"])
async def test_cold_plan_does_not_send_to_an_unresolvable_peer(tmp_path, telegram_peers, failure):
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", api_id=1, send_chats=["123"])},
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        await mcp.call_tool("chat_resolve", {"profile_id": "personal", "target": "@test_target"})
        preview = data(
            await mcp.call_tool(
                "delivery_preview",
                {"profile_id": "personal", "recipients": ["123"], "text": "Synthetic test"},
            )
        )["data"]
    telegram_peers["username_id"] = 124 if failure == "changed" else 123
    telegram_peers["username_missing"] = failure == "missing"
    telegram_peers["lookup_error"] = failure == "network"
    telegram_peers["expire_peer"] = True
    expected = {
        "changed": "peer_identity_changed",
        "missing": "peer_unavailable",
        "network": "connection_error",
    }[failure]
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        resolved = data(
            await mcp.call_tool("chat_resolve", {"profile_id": "personal", "target": "123"})
        )
        assert resolved["error"]["code"] == expected
        executed = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": preview["plan_id"],
                    "plan_hash": preview["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        finished = await complete(mcp, "personal", executed["data"]["job_id"])
        assert finished["status"] == ("needs_review" if failure == "network" else "failed")
        assert finished["deliveries"][0]["status"] == (
            "unknown" if failure == "network" else "failed"
        )
        assert finished["deliveries"][0]["error"]["code"] == expected
    assert telegram_peers["sent"] == []


@pytest.mark.asyncio
async def test_numeric_resolution_can_restore_peer_from_dialog_without_cached_username(
    tmp_path, telegram_peers
):
    telegram_peers["dialog"] = True
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user", api_id=1)})
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        resolved = data(
            await mcp.call_tool("chat_resolve", {"profile_id": "personal", "target": "123"})
        )
        assert resolved["ok"], resolved["error"]
        assert resolved["data"]["id"] == "123"


@pytest.mark.asyncio
async def test_peer_restoration_does_not_use_another_profiles_cached_username(
    tmp_path, telegram_peers, monkeypatch
):
    other_session = telegram_peers_session()
    other_session.auth_key = AuthKey(b"\x02" * 256)
    monkeypatch.setenv("TELELOOM_OTHER_SESSION", other_session.save())
    monkeypatch.setenv("TELELOOM_OTHER_API_HASH", "synthetic-api-hash")
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(kind="user", api_id=1),
            "other": Profile(kind="user", api_id=1),
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        resolved = data(
            await mcp.call_tool(
                "chat_resolve", {"profile_id": "personal", "target": "@test_target"}
            )
        )
        assert resolved["ok"]
        isolated = data(
            await mcp.call_tool("messages_get", {"profile_id": "other", "chat_id": "123"})
        )
        assert isolated["error"]["code"] == "peer_unavailable"
    assert telegram_peers["sent"] == []


def telegram_peers_session():
    session = StringSession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(b"\x01" * 256)
    return session


@pytest.mark.asyncio
async def test_explicit_ack_restores_cold_peer_and_uses_exact_checkpoint(tmp_path, telegram_peers):
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", api_id=1, mutation_chats=["123"])},
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        await mcp.call_tool("chat_resolve", {"profile_id": "personal", "target": "@test_target"})
    assert telegram_peers["acknowledged"] == []
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        preview = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "read_ack", "chat_id": "123", "through_message_id": "5"},
                },
            )
        )["data"]
        ack = data(
            await mcp.call_tool(
                "inbox_ack",
                {
                    "profile_id": "personal",
                    "chat_id": "123",
                    "through_message_id": "5",
                    "plan_id": preview["plan_id"],
                    "plan_hash": preview["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert ack["ok"], ack["error"]
        assert (await complete(mcp, "personal", ack["data"]["job_id"]))["status"] == "completed"
    assert telegram_peers["acknowledged"] == [(123, 5)]
    assert telegram_peers["sent"] == []
