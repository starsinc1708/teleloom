from datetime import UTC, datetime, timedelta

import pytest
from aiogram import methods
from telethon import functions, types

from teleloom.adapters import UserAdapter, make_adapter
from teleloom.config import Profile, Settings
from teleloom.models import utcnow
from teleloom.store import Store
from tests import test_message_bot_sdk as bot_sdk_fixture
from tests import test_message_sdk as user_sdk_fixture
from tests.fakes import data
from tests.test_jobs import complete
from tests.test_transport import client, running

message_sdk = user_sdk_fixture.message_sdk
bot_message_sdk = bot_sdk_fixture.bot_message_sdk


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["user", "bot"])
@pytest.mark.parametrize("action", ["send", "reply", "edit"])
async def test_native_date_chip_send_reply_and_edit_are_confirmed_and_serializable(
    tmp_path, message_sdk, bot_message_sdk, backend, action
):
    profile = "personal" if backend == "user" else "helper"
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            profile: Profile(
                kind=backend,
                api_id=1,
                send_chats=["100"],
                mutation_chats=["100"],
                polling=backend == "bot",
            )
        },
    )
    timestamp = int((utcnow() + timedelta(days=1)).timestamp())
    entity = {
        "type": "date_time",
        "offset": 3,
        "length": 10,
        "unix_time": timestamp,
        "date_time_format": "Dt",
    }
    operation = {
        "kind": "edit" if action == "edit" else "send",
        "chat_id": "100",
        "text": "😀 06/10/2026",
        "entities": [entity],
        **({"message_id": "4"} if action == "edit" else {}),
        **({"reply_to_message_id": "4"} if action == "reply" else {}),
    }
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        preview = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": profile,
                    "operation": operation,
                },
            )
        )
        assert preview["ok"], preview
        plan = preview["data"]
        frozen = plan["preview"]["operation"]
        assert all(frozen["entities"][0][key] == value for key, value in entity.items())
        assert frozen["rendered"]["entities"][0]["_"] == "MessageEntityFormattedDate"
        result = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": profile,
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert result["ok"], result
        finished = await complete(mcp, profile, result["data"]["job_id"])
        assert finished["status"] == "completed", finished
        if backend == "user":
            method = (
                functions.messages.EditMessageRequest
                if action == "edit"
                else functions.messages.SendMessageRequest
            )
            request = next(r for r in message_sdk["requests"] if isinstance(r, method))
            native = request.entities[0]
            assert isinstance(native, types.MessageEntityFormattedDate)
            assert int(native.date.timestamp()) == timestamp
            assert native.long_date and native.short_time and native.offset == 3
            assert bytes(native).startswith(b"\xc7\xc7J\x90")
            assert request.message == operation["text"]
            if action == "reply":
                assert request.reply_to.reply_to_msg_id == 4
        else:
            method = methods.EditMessageText if action == "edit" else methods.SendMessage
            request = next(r for r in bot_message_sdk if isinstance(r, method))
            native = request.entities[0]
            assert native.type == "date_time"
            assert native.unix_time == timestamp and native.date_time_format == "Dt"
            assert native.offset == 3 and request.text == operation["text"]
            if action == "reply":
                assert request.reply_parameters.message_id == 4
            observed = data(
                await mcp.call_tool(
                    "messages_get",
                    {
                        "profile_id": profile,
                        "chat_id": "100",
                        "message_ids": ["101"],
                    },
                )
            )["data"]["items"][0]["entities"][0]
            assert observed["unix_time"] == timestamp and observed["date_time_format"] == "Dt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"unix_time": -1},
        {"unix_time": 4_000_000_000},
        {"date_time_format": "rDt"},
        {"date_time_format": "dD"},
        {"offset": 1},
        {"nested": True},
    ],
)
async def test_invalid_date_entities_are_rejected_before_telegram_connect(tmp_path, change):
    connected = []

    def factory(*args):
        connected.append(True)
        raise AssertionError("invalid entity must not connect")

    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(kind="user", api_id=1, send_chats=["100"]),
        },
    )
    entity = {
        "type": "date_time",
        "offset": 3,
        "length": 10,
        "unix_time": int(utcnow().timestamp()),
        "date_time_format": "Dt",
        **{key: value for key, value in change.items() if key != "nested"},
    }
    entities = [entity]
    if change.get("nested"):
        entities.append({"type": "bold", "offset": 3, "length": 10})
    async with running(settings, factory) as app, client(app, settings) as mcp:
        result = await mcp.call_tool(
            "message_operation_preview",
            {
                "profile_id": "personal",
                "operation": {
                    "kind": "send",
                    "chat_id": "100",
                    "text": "😀 06/10/2026",
                    "entities": entities,
                },
            },
        )
        assert result.isError, result
        assert not connected


def drafts_fixture(message_sdk):
    date = datetime.now(UTC)
    message_sdk["all_drafts"] = [
        types.UpdateDraftMessage(
            peer=types.PeerUser(peer),
            draft=types.DraftMessage(
                message=text,
                date=date,
                entities=[types.MessageEntityBold(0, len(text))],
                reply_to=types.MessageReplyHeader(
                    reply_to_msg_id=4,
                    reply_to_peer_id=types.PeerUser(300),
                    quote_text="Private original",
                ),
            ),
        )
        for peer, text in [(100, "First draft"), (200, "Second draft"), (300, "Hidden draft")]
    ]


@pytest.mark.asyncio
async def test_account_wide_drafts_have_scoped_bounded_stable_pages(
    tmp_path, message_sdk, monkeypatch
):
    drafts_fixture(message_sdk)
    profile = Profile(kind="user", api_id=1, read_mode="selected", read_chats=["100", "200"])
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": profile, "other": Profile(kind="user")}
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        first = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "kind": "drafts",
                    "limit": 1,
                },
            )
        )
        assert first["ok"], first
        page = first["data"]
        assert page["items"][0]["text"] == "First draft"
        assert page["items"][0]["kind"] == "draft" and "id" not in page["items"][0]
        assert page["items"][0]["entities"][0]["_"] == "MessageEntityBold"
        assert page["items"][0]["reply_quote"] is None
        assert "Private original" not in str(page) and "Hidden draft" not in str(page)
        assert page["next_cursor"] and page["incomplete"]
        assert page["coverage"]["observed_chats"] == 2
        cursor = page["next_cursor"]
        message_sdk["all_drafts"].clear()
        projected = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "kind": "drafts",
                    "limit": 1,
                    "cursor": cursor,
                    "fields": [],
                },
            )
        )
        assert projected["ok"], projected
        row = projected["data"]["items"][0]
        assert (
            row["profile_id"] == "personal" and row["chat_id"] == "200" and row["kind"] == "draft"
        )
        assert row["date"] and "id" not in row and "text" not in row and "entities" not in row
        second = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "kind": "drafts",
                    "limit": 1,
                    "cursor": cursor,
                },
            )
        )
        assert second["ok"], second
        assert second["data"]["items"][0]["text"] == "Second draft"
        assert second["data"]["next_cursor"] is None and not second["data"]["incomplete"]
        assert (
            sum(
                isinstance(r, functions.messages.GetAllDraftsRequest)
                for r in message_sdk["requests"]
            )
            == 1
        )
        other = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "other",
                    "kind": "drafts",
                    "cursor": cursor,
                },
            )
        )
        assert other["error"]["code"] == "invalid_cursor"
        profile.read_chats = ["100"]
        denied = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "kind": "drafts",
                    "cursor": cursor,
                },
            )
        )
        assert denied["error"]["code"] == "invalid_cursor"
        profile.read_chats = ["100", "200"]
        generation = profile.generation
        profile.generation = "another-generation"
        changed = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "kind": "drafts",
                    "cursor": cursor,
                },
            )
        )
        assert changed["error"]["code"] == "invalid_cursor"
        profile.generation = generation
    store = Store(tmp_path)
    stored = store.state(f"reading_snapshot:{page['snapshot_id']}")
    assert "Hidden draft" not in str(stored) and "Private original" not in str(stored)
    store.close()
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        restarted = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "kind": "drafts",
                    "cursor": cursor,
                },
            )
        )
        assert restarted["ok"] and restarted["data"]["items"][0]["text"] == "Second draft"
        profiles = data(await mcp.call_tool("profiles_list", {}))["data"]["profiles"]
        assert not next(p for p in profiles if p["id"] == "personal")["connected"]
        future = utcnow() + timedelta(minutes=16)
        monkeypatch.setattr("teleloom.runtime.utcnow", lambda: future)
        expired = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "kind": "drafts",
                    "cursor": cursor,
                },
            )
        )
        assert expired["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["empty_selected_user", "bot"])
async def test_account_drafts_empty_policy_or_unsupported_backend_never_connect(tmp_path, backend):
    connected = []

    def factory(*args):
        connected.append(True)
        raise AssertionError("request must not connect")

    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="bot" if backend == "bot" else "user",
                read_mode="selected",
                read_chats=[],
            )
        },
    )
    async with running(settings, factory) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "kind": "drafts",
                },
            )
        )
        if backend == "bot":
            assert result["error"]["code"] == "unsupported_capability"
        else:
            assert result["ok"] and result["data"]["items"] == []
            assert result["data"]["coverage"]["observed_chats"] == 0
        assert not connected


@pytest.mark.asyncio
async def test_history_to_exact_button_to_confirmed_callback_composition(tmp_path, message_sdk):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="user",
                api_id=1,
                mutation_chats=["100"],
            )
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        history = data(
            await mcp.call_tool(
                "messages_get",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "limit": 1,
                },
            )
        )
        message = history["data"]["items"][0]
        buttons = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "chat_id": message["chat_id"],
                    "message_id": message["id"],
                    "kind": "buttons",
                },
            )
        )
        button = buttons["data"]["items"][0]
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "inline_callback",
                        "chat_id": message["chat_id"],
                        "message_id": message["id"],
                        "button_index": button["index"],
                    },
                },
            )
        )["data"]
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        finished = await complete(mcp, "personal", job)
        assert finished["status"] == "completed", finished
        assert finished["deliveries"][0]["receipt"]["response"] == "Accepted"


@pytest.mark.asyncio
async def test_mtproto_bot_forward_supports_native_attribution_flags(tmp_path, message_sdk):
    message_sdk["owner_bot"] = True
    profile = Profile(
        kind="bot",
        bot_backend="mtproto",
        api_id=1,
        send_chats=["100"],
        read_mode="selected",
        read_chats=["100"],
    )
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    # This explicit backend fixture uses the real MTProto writer and SDK; the
    # management slice supplies production bot session provisioning/routing.
    async with running(settings, UserAdapter) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "forward",
                        "chat_id": "100",
                        "source_chat_id": "100",
                        "message_ids": ["4"],
                        "expand_album": False,
                        "drop_author": True,
                        "drop_media_captions": True,
                        "top_message_id": "4",
                    },
                },
            )
        )
        assert result["ok"], result
        plan = result["data"]
        arguments = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        profile.read_chats = []
        denied = data(await mcp.call_tool("delivery_execute", arguments))
        assert denied["error"]["code"] == "read_not_allowed"
        profile.read_chats = ["100"]
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                arguments,
            )
        )["data"]["job_id"]
        finished = await complete(mcp, "personal", job)
        assert finished["status"] == "completed", finished
        request = next(
            r
            for r in message_sdk["requests"]
            if isinstance(r, functions.messages.ForwardMessagesRequest)
        )
        assert request.drop_author and request.drop_media_captions
        assert request.top_msg_id == 4 and request.reply_to is None
        assert request.id == [4] and request.from_peer.user_id == request.to_peer.user_id == 100
        assert bytes(request)
        forbidden = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send",
                        "chat_id": "100",
                        "text": "Later",
                        "schedule_at": (utcnow() + timedelta(days=1)).isoformat(),
                    },
                },
            )
        )
        assert forbidden["error"]["code"] == "unsupported_capability"
        profile.bot_backend = "bot_api"
        changed = data(await mcp.call_tool("delivery_execute", arguments))
        assert changed["error"]["code"] == "account_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["user", "bot_api", "mtproto"])
async def test_ordinary_forward_reply_routing_is_rejected_before_connect(tmp_path, backend):
    connected = []

    def factory(*args):
        connected.append(True)
        raise AssertionError("unsupported routing must not connect")

    profile = Profile(
        kind="user" if backend == "user" else "bot",
        bot_backend="mtproto" if backend == "mtproto" else "bot_api",
        send_chats=["100"],
    )
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, factory) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "forward",
                        "chat_id": "100",
                        "source_chat_id": "100",
                        "message_ids": ["4"],
                        "reply_to_message_id": "4",
                    },
                },
            )
        )
        assert result["error"]["code"] == "platform_restriction"
        assert not connected


@pytest.mark.asyncio
async def test_scheduled_native_acceptance_is_not_confirmed_future_delivery(tmp_path, message_sdk):
    message_sdk["scheduled_acceptance"] = True
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="user",
                api_id=1,
                send_chats=["100"],
            )
        },
    )
    instant = (utcnow() + timedelta(days=1)).isoformat()
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send",
                        "chat_id": "100",
                        "text": "Later",
                        "schedule_at": instant,
                    },
                },
            )
        )["data"]
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        finished = await complete(mcp, "personal", job)
        receipt = finished["deliveries"][0]["receipt"]
        assert receipt["message_ids"] == ["301"]
        assert receipt["schedule_accepted"] is True
        assert datetime.fromisoformat(receipt["scheduled_for"]) == datetime.fromisoformat(instant)
        assert receipt["delivery_confirmed"] is False
