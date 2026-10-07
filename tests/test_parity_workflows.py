from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon import functions, types
from telethon.crypto import AuthKey

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.telegram_fakes import CHAT, SDK, factory
from tests.test_jobs import complete
from tests.test_message_sdk import message_sdk as message_sdk
from tests.test_transport import client, running


@pytest.mark.asyncio
async def test_profile_discovery_describes_backends_grants_and_actual_exposure(
    tmp_path, monkeypatch
):
    def no_keyring():
        raise AssertionError("Profile discovery must not probe OS credentials")

    monkeypatch.setattr("teleloom.secrets.Secrets.backend", no_keyring)
    settings = Settings(
        data_dir=tmp_path,
        exposure_mode="read-only",
        profiles={
            "personal": Profile(kind="user", read_mode="selected", read_chats=[CHAT]),
            "api": Profile(kind="bot"),
            "native": Profile(kind="bot", bot_backend="mtproto"),
        },
    )
    async with running(settings) as app, client(app, settings) as mcp:
        names = {tool.name for tool in (await mcp.list_tools()).tools}
        result = data(await mcp.call_tool("profiles_list", {}))["data"]
        assert result["tool_exposure"]["mode"] == "read-only"
        assert "delivery_execute" not in names and "message_operation_preview" not in names
        assert {"media_download", "photo_open", "photo_sheet", "media_cleanup"} <= names
        profiles = {p["id"]: p for p in result["profiles"]}
        assert profiles["personal"]["backend"] == "mtproto"
        assert profiles["api"]["backend"] == "bot_api"
        assert profiles["native"]["backend"] == "mtproto"
        assert profiles["personal"]["capabilities"]["telegram_history"] is True
        assert profiles["native"]["capabilities"]["telegram_history"] is False
        assert profiles["api"]["capabilities"]["explicit_message_lookup"] == "saved_updates_only"
        assert profiles["native"]["capabilities"]["explicit_message_lookup"] == "telegram"
        assert profiles["personal"]["capabilities"]["media"]["file_roots_configured"] is False
        assert (
            profiles["personal"]["capabilities"]["transcription_engines"][
                "automatic_model_download"
            ]
            is False
        )
        assert all(not p["connected"] for p in profiles.values())
        assert "auth_key" not in str(result) and "api_hash" not in str(result)


@pytest.mark.asyncio
async def test_account_listing_composes_exact_profiles_with_current_native_self_status(
    tmp_path, monkeypatch
):
    class SelfSDK(SDK):
        def __init__(self, identity, bot):
            super().__init__()
            self.identity, self.bot = identity, bot
            self.session.auth_key = AuthKey(bytes([identity]) * 256)
            self.session.set_dc(1, "149.154.167.51", 443)

        async def get_me(self):
            self.calls.append("get_me")
            return types.User(
                id=self.identity,
                first_name="Current " + str(self.identity),
                status=types.UserStatusRecently(),
                bot=self.bot,
                phone="PRIVATE_PHONE",
            )

    sdks = {1: SelfSDK(1, False), 2: SelfSDK(2, True)}
    monkeypatch.setattr(
        "teleloom.adapters.TelegramClient", lambda session, api_id, *a, **kw: sdks[api_id]
    )
    for name in ("personal", "native"):
        monkeypatch.setenv(
            f"TELELOOM_{name.upper()}_SESSION", sdks[1 if name == "personal" else 2].session.save()
        )
        monkeypatch.setenv(f"TELELOOM_{name.upper()}_API_HASH", "synthetic-api-hash")
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(kind="user", api_id=1, identity={"id": "1"}),
            "native": Profile(kind="bot", bot_backend="mtproto", api_id=2, identity={"id": "2"}),
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        profiles = data(await mcp.call_tool("profiles_list", {}))["data"]["profiles"]
        assert [p["id"] for p in profiles] == ["personal", "native"]
        assert not any(sdk.calls for sdk in sdks.values())
        for index, profile in enumerate(profiles, 1):
            result = data(
                await mcp.call_tool(
                    "account_read", {"profile_id": profile["id"], "operation": {"kind": "me"}}
                )
            )
            assert result["ok"], result
            me = result["data"]["item"]
            assert me["id"] == str(index) and me["first_name"] == "Current " + str(index)
            assert me["status"]["kind"] == "UserStatusRecently"
            assert "PRIVATE_PHONE" not in str(result)
        assert all(sdk.calls for sdk in sdks.values())


@pytest.mark.asyncio
async def test_filtered_dialogs_compose_bounded_about_and_rich_history_pin_reads(tmp_path):
    class DialogSDK(SDK):
        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            if isinstance(request, functions.messages.SearchRequest):
                assert isinstance(request.filter, types.InputMessagesFilterPinned)
                return types.messages.Messages(messages=self.rows, chats=[], users=[], topics=[])
            assert isinstance(request, functions.channels.GetFullChannelRequest)
            return types.messages.ChatFull(
                full_chat=types.ChannelFull(
                    id=100,
                    about="Selected description",
                    read_inbox_max_id=0,
                    read_outbox_max_id=0,
                    unread_count=1,
                    chat_photo=types.PhotoEmpty(0),
                    notify_settings=types.PeerNotifySettings(),
                    bot_info=[],
                    pts=0,
                ),
                chats=[],
                users=[],
            )

    sdk = DialogSDK()
    entity = await sdk.get_entity(None)
    sdk.dialogs = [
        SimpleNamespace(
            id=int(CHAT),
            entity=entity,
            name="Selected",
            unread_count=1,
            is_group=False,
            is_channel=True,
            date=datetime(2026, 10, 5, tzinfo=UTC),
            dialog=SimpleNamespace(
                notify_settings=types.PeerNotifySettings(mute_until=0),
                folder_id=1,
                top_message=10,
                read_inbox_max_id=9,
                unread_mark=False,
                unread_mentions_count=0,
            ),
        )
    ]
    sdk.rows = [
        types.Message(
            id=10,
            peer_id=types.PeerChannel(100),
            date=datetime(2026, 10, 5, tzinfo=UTC),
            message="Original custom emoji",
            pinned=True,
            entities=[types.MessageEntityCustomEmoji(0, 1, 123)],
            reply_to=types.MessageReplyHeader(reply_to_msg_id=9, quote_text="Original quote"),
        )
    ]
    profile = Profile(kind="user", read_mode="selected", read_chats=[CHAT])
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        listed = data(
            await mcp.call_tool(
                "chats_list",
                {
                    "profile_id": "personal",
                    "unread_only": True,
                    "unmuted_only": True,
                    "archived": True,
                    "limit": 1,
                },
            )
        )
        assert listed["ok"], listed
        items = listed["data"]["items"]
        assert [item["id"] for item in items] == [CHAT]
        about = data(
            await mcp.call_tool(
                "administration_read",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "chat",
                        "chat_id": items[0]["id"],
                        "include_dialog": False,
                    },
                },
            )
        )
        assert about["ok"], about
        assert about["data"]["item"]["about"] == "Selected description"
        assert (
            sum(isinstance(req, functions.channels.GetFullChannelRequest) for req in sdk.calls) == 1
        )
        for tool in ("messages_get", "messages_pinned"):
            result = data(await mcp.call_tool(tool, {"profile_id": "personal", "chat_id": CHAT}))
            assert result["ok"], result
            item = result["data"]["items"][0]
            assert item["text"] == "Original custom emoji"
            assert item["entities"][0]["document_id"] == "123"
            assert item["reply_quote"]["text"] == "Original quote"
        profile.read_chats = []
        denied = data(
            await mcp.call_tool(
                "administration_read",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "chat", "chat_id": CHAT, "include_dialog": False},
                },
            )
        )
        assert denied["error"]["code"] == "read_not_allowed"


@pytest.mark.asyncio
async def test_native_bot_factory_forwards_confirmed_attribution_and_topic_options(
    tmp_path, monkeypatch, message_sdk
):
    from teleloom import adapters

    base = adapters.TelegramClient
    date = datetime.now(UTC)
    message_sdk["owner_bot"] = True

    class NativeSDK(base):
        async def __call__(self, request, *args, **kwargs):
            if isinstance(request, functions.messages.GetMessagesRequest):
                message_sdk["requests"].append(request)
                return types.messages.Messages(
                    messages=[
                        types.Message(
                            id=item.id,
                            peer_id=types.PeerUser(100),
                            date=date,
                            message="Exact reviewed source",
                            out=True,
                        )
                        for item in request.id
                    ],
                    chats=[],
                    users=[],
                    topics=[],
                )
            return await super().__call__(request, *args, **kwargs)

    monkeypatch.setattr(adapters, "TelegramClient", NativeSDK)
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="bot",
                bot_backend="mtproto",
                api_id=1,
                send_chats=["100"],
                read_mode="selected",
                read_chats=["100"],
            )
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        preview = data(
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
        assert preview["ok"], preview
        plan = preview["data"]
        arguments = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        result = data(await mcp.call_tool("delivery_execute", arguments))
        assert result["ok"], result
        finished = await complete(mcp, "personal", result["data"]["job_id"])
        assert finished["status"] == "completed", finished
        writes = [
            r
            for r in message_sdk["requests"]
            if isinstance(r, functions.messages.ForwardMessagesRequest)
        ]
        assert len(writes) == 1 and writes[0].drop_author and writes[0].drop_media_captions
        assert writes[0].top_msg_id == 4 and writes[0].reply_to is None
        assert any(
            isinstance(r, functions.messages.GetMessagesRequest) for r in message_sdk["requests"]
        )
        duplicate = data(await mcp.call_tool("delivery_execute", arguments))["data"]
        assert duplicate["existing"] and duplicate["job_id"] == result["data"]["job_id"]
