import asyncio
from datetime import UTC, datetime

import pytest
from telethon import events, functions, types

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from tests.administration_fakes import TelegramSDK
from tests.fakes import data
from tests.media_fakes import encoded_image
from tests.telegram_fakes import CHAT, SDK, factory
from tests.test_folder_operations_parity import FoldersSDK
from tests.test_native_bot_local_reads import GROUP, NativeSDK
from tests.test_transport import client, running


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["digest", "inferred"])
async def test_nested_latest_message_projection_keeps_selected_content(
    tmp_path, monkeypatch, selection
):
    sdk = TelegramSDK()
    monkeypatch.setattr("teleloom.adapters.user_client", lambda *a, **kw: sdk)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", read_mode="selected", read_chats=[GROUP])},
    )
    args = {"profile_id": "personal", "operation": {"kind": "chat", "chat_id": GROUP}}
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        if selection == "inferred":
            selected = data(
                await mcp.call_tool(
                    "response_fields_select",
                    {
                        "tool_name": "administration_read",
                        "request": "Summarize the latest message with its source",
                    },
                )
            )
            assert selected["ok"], selected
            args["fields"] = selected["data"]["fields"]
        else:
            args["preset"] = selection
        result = data(await mcp.call_tool("administration_read", args))
        assert result["ok"], result
        item = result["data"]["item"]
        assert item["latest_message"]["text"] == "Latest selected message"
        assert item["latest_message"]["id"] == "88" and item["latest_message"]["chat_id"] == GROUP


@pytest.mark.asyncio
async def test_native_bot_collected_photo_is_listed(tmp_path, monkeypatch):
    class PhotoSDK(NativeSDK):
        async def __call__(self, request, *args, **kwargs):
            result = await super().__call__(request, *args, **kwargs)
            for message in result.messages:
                message.media = raw.media
            return result

        async def iter_download(self, *args, **kwargs):
            yield encoded_image()

        async def get_messages(self, peer, *, ids):
            return raw

    sdk = PhotoSDK()
    monkeypatch.setattr("teleloom.adapters.user_client", lambda *a, **kw: sdk)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"robot": Profile(kind="bot", bot_backend="mtproto", polling=True)},
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        assert data(await mcp.call_tool("inbox_get", {"profile_id": "robot"}))["ok"]
        raw = types.Message(
            id=11,
            peer_id=types.PeerChannel(123),
            date=datetime.now(UTC),
            message="Collected native photo",
            media=types.MessageMediaPhoto(
                photo=types.Photo(
                    id=33,
                    access_hash=44,
                    file_reference=b"",
                    date=datetime.now(UTC),
                    sizes=[types.PhotoSize("m", 30, 20, len(encoded_image()))],
                    dc_id=1,
                )
            ),
        )
        for builder, callback in sdk._event_builders:
            if isinstance(builder, events.NewMessage):
                await callback(events.NewMessage.Event(raw))
        photos = data(
            await mcp.call_tool(
                "photos_list",
                {
                    "profile_id": "robot",
                    "chat_id": GROUP,
                    "source": "messages",
                },
            )
        )
        assert photos["ok"], photos
        assert [item["id"] for item in photos["data"]["items"]] == ["11"]
        info = data(
            await mcp.call_tool(
                "media_info", {"profile_id": "robot", "chat_id": GROUP, "message_id": "11"}
            )
        )
        assert info["ok"] and info["data"]["source"] == "telegram"
        assert isinstance(sdk.calls[-1], functions.channels.GetMessagesRequest)
        sheet = await mcp.call_tool(
            "photo_sheet", {"profile_id": "robot", "chat_id": GROUP, "source": "messages"}
        )
        assert data(sheet)["ok"], data(sheet)
        assert sheet.content[1].type == "image"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "channel", "private"])
async def test_native_bot_collected_chat_preserves_kind_and_missing_metadata(
    tmp_path, monkeypatch, kind
):
    sdk = NativeSDK()
    monkeypatch.setattr("teleloom.adapters.user_client", lambda *a, **kw: sdk)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"robot": Profile(kind="bot", bot_backend="mtproto", polling=True)},
    )
    entity = (
        types.User(id=123, first_name="Observed", username="observed", bot=True, contact=True)
        if kind == "private"
        else types.Channel(
            id=123,
            title="Observed",
            photo=types.ChatPhotoEmpty(),
            date=datetime.now(UTC),
            megagroup=kind == "group",
            forum=kind == "group",
            username="observed",
        )
    )
    chat = "123" if kind == "private" else GROUP
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        assert data(await mcp.call_tool("inbox_get", {"profile_id": "robot"}))["ok"]
        for id_ in (1, 2):
            raw = types.Message(
                id=id_,
                peer_id=types.PeerUser(123) if kind == "private" else types.PeerChannel(123),
                date=datetime.now(UTC),
                message="Collected",
            )
            event = events.NewMessage.Event(raw)
            if id_ == 1:
                event._chat = entity
            for builder, callback in sdk._event_builders:
                if isinstance(builder, events.NewMessage):
                    await callback(event)
            rows = data(await mcp.call_tool("chats_list", {"profile_id": "robot", "kind": kind}))
            assert rows["ok"], rows
            assert len(rows["data"]["items"]) == 1
            item = rows["data"]["items"][0]
            assert (
                item["id"] == chat
                and item["title"] == "Observed"
                and item["username"] == "observed"
            )
            if kind == "private":
                assert item["is_contact"] and item["is_bot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_legacy_folder_reader_handles_saved_messages_and_reports_withheld_peers(
    tmp_path, selected
):
    profile = (
        Profile(kind="user", read_mode="selected", read_chats=["1"])
        if selected
        else Profile(kind="user")
    )
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, factory(FoldersSDK())) as app, client(app, settings) as mcp:
        legacy = data(await mcp.call_tool("folders_list", {"profile_id": "personal"}))
        assert legacy["ok"], legacy
        result = legacy["data"]
        assert result["items"][0]["pinned_chat_ids"] == ["1"]
        assert result["incomplete"] is selected
        if selected:
            assert result["coverage"]["withheld_peers"] > 0 and result["warnings"]


@pytest.mark.asyncio
async def test_parallel_downloads_respect_aggregate_private_disk_budget(tmp_path):
    class DownloadSDK(SDK):
        def __init__(self):
            super().__init__()
            self.downloads = 0
            self.rows = [
                types.Message(
                    id=10,
                    peer_id=types.PeerChannel(100),
                    date=datetime.now(UTC),
                    message="one byte",
                    media=types.MessageMediaDocument(
                        document=types.Document(
                            id=1,
                            access_hash=2,
                            file_reference=b"",
                            date=datetime.now(UTC),
                            mime_type="application/octet-stream",
                            size=1,
                            dc_id=1,
                            attributes=[types.DocumentAttributeFilename("tiny.bin")],
                        )
                    ),
                )
            ]

        async def iter_download(self, *args, **kwargs):
            self.downloads += 1
            await asyncio.sleep(0.01)
            yield b"x"

    sdk = DownloadSDK()
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        root = settings.data_dir / "media-downloads"
        root.mkdir()
        with (root / "existing.bin").open("wb") as stream:
            stream.truncate(499_999_999)
        results = [
            data(result)
            for result in await asyncio.gather(
                *[
                    mcp.call_tool(
                        "media_download",
                        {
                            "profile_id": "personal",
                            "chat_id": CHAT,
                            "message_id": "10",
                            "max_bytes": 1,
                        },
                    )
                    for _ in range(3)
                ]
            )
        ]
        assert sum(path.stat().st_size for path in root.iterdir()) <= 500_000_000
        assert sum(result["ok"] for result in results) == 1
        assert [result["error"]["code"] for result in results if not result["ok"]] == [
            "file_disk_limit",
            "file_disk_limit",
        ]
        assert sdk.downloads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["photo_open", "photo_sheet"])
async def test_photo_inspection_obeys_shared_private_disk_budget(tmp_path, tool):
    class PhotoSDK(SDK):
        def __init__(self):
            super().__init__()
            self.rows = [
                types.Message(
                    id=1,
                    peer_id=types.PeerChannel(100),
                    date=datetime.now(UTC),
                    message="Photo",
                    media=types.MessageMediaPhoto(
                        photo=types.Photo(
                            id=1,
                            access_hash=2,
                            file_reference=b"",
                            date=datetime.now(UTC),
                            sizes=[types.PhotoSize("m", 30, 20, len(encoded_image()))],
                            dc_id=1,
                        )
                    ),
                )
            ]

        async def iter_download(self, *args, **kwargs):
            raise AssertionError("Disk admission must precede photo transfer")
            yield b""

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(PhotoSDK())) as app, client(app, settings) as mcp:
        root = settings.data_dir / "media-downloads"
        root.mkdir()
        with (root / "existing.bin").open("wb") as stream:
            stream.truncate(500_000_000)
        args = {"profile_id": "personal", "chat_id": CHAT}
        args.update({"message_id": "1"} if tool == "photo_open" else {"source": "messages"})
        result = data(await mcp.call_tool(tool, args))
        assert not result["ok"], result
        assert result["error"]["code"] == (
            "file_disk_limit" if tool == "photo_open" else "photo_unavailable"
        )
        if tool == "photo_sheet":
            assert result["error"]["details"]["errors"][0]["error"] == "file_disk_limit"


@pytest.mark.asyncio
async def test_full_user_preserves_baseline_business_profile_fields(tmp_path, monkeypatch):
    class RichSDK(TelegramSDK):
        async def __call__(self, request):
            result = await super().__call__(request)
            if isinstance(request, functions.users.GetFullUserRequest):
                result.full_user.business_intro = types.BusinessIntro(
                    "Business title", "Business description"
                )
                result.full_user.private_forward_name = "Private forward label"
                result.full_user.pinned_msg_id = 88
                result.full_user.stargifts_count = 3
            return result

    sdk = RichSDK()
    sdk.user.lang_code = "en"
    sdk.user.close_friend = True
    sdk.user.usernames = [
        types.Username("inactive_alias", active=False),
        types.Username("active_alias", active=True),
    ]
    sdk.user.restriction_reason = [
        types.RestrictionReason("all", "terms", "Untrusted restriction text")
    ]
    monkeypatch.setattr("teleloom.adapters.user_client", lambda *a, **kw: sdk)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", read_mode="selected", read_chats=["8"])},
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "account_read",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "user", "user_id": "8"},
                },
            )
        )
        assert result["ok"], result
        item = result["data"]["item"]
        assert item["usernames"] == ["inactive_alias", "active_alias"]
        assert item["username_flags"] == [
            {"username": "inactive_alias", "active": False, "editable": False},
            {"username": "active_alias", "active": True, "editable": False},
        ]
        assert item["lang_code"] == "en" and item["flags"]["close_friend"]
        assert item["restriction_reasons"] == [
            {"platform": "all", "reason": "terms", "text": "Untrusted restriction text"}
        ]
        assert item["business_intro"] == {
            "title": "Business title",
            "description": "Business description",
        }
        assert item["private_forward_name"] == "Private forward label"
        assert item["pinned_msg_id"] == "88" and item["stargifts_count"] == 3
        assert item["untrusted"] and "access_hash" not in item and "phone" not in item
        projected = data(
            await mcp.call_tool(
                "account_read",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "user", "user_id": "8"},
                    "fields": ["business_intro"],
                },
            )
        )
        assert projected["ok"], projected
        shown = projected["data"]["item"]
        assert shown["id"] == "8" and shown["untrusted"]
        assert shown["business_intro"] == item["business_intro"]
        assert "first_name" not in shown and "restriction_reasons" not in shown
        assert projected["data"]["coverage"] == result["data"]["coverage"]
        selection = data(
            await mcp.call_tool(
                "response_fields_select", {"tool_name": "account_read", "request": "everything"}
            )
        )
        assert selection["ok"] and len(selection["data"]["fields"]) <= 64
        assert data(
            await mcp.call_tool(
                "account_read",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "user", "user_id": "8"},
                    "fields": selection["data"]["fields"],
                },
            )
        )["ok"]


@pytest.mark.asyncio
async def test_member_projection_retains_role_and_user_identity(tmp_path, monkeypatch):
    sdk = TelegramSDK()
    monkeypatch.setattr("teleloom.adapters.user_client", lambda *a, **kw: sdk)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", read_mode="selected", read_chats=[GROUP])},
    )
    args = {"profile_id": "personal", "operation": {"kind": "participants", "chat_id": GROUP}}
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        full = data(await mcp.call_tool("administration_read", args))
        shown = data(await mcp.call_tool("administration_read", {**args, "fields": ["user"]}))
        assert full["ok"] and shown["ok"], shown
        member = shown["data"]["items"][0]
        assert member["id"] == "8" and member["role"] == full["data"]["items"][0]["role"]
        assert member["user"]["id"] == "8" and member["user"]["untrusted"]
        assert "first_name" not in member["user"] and "rank" not in member
        for key in ("next_cursor", "coverage", "incomplete", "source", "untrusted"):
            assert shown["data"][key] == full["data"][key]
        selection = data(
            await mcp.call_tool(
                "response_fields_select",
                {"tool_name": "administration_read", "request": "everything"},
            )
        )
        assert selection["ok"] and len(selection["data"]["fields"]) <= 64
        assert data(
            await mcp.call_tool(
                "administration_read", {**args, "fields": selection["data"]["fields"]}
            )
        )["ok"]
