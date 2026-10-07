"""Public media display contract, with real owner/state/process seams."""

import json
from datetime import UTC, datetime

import pytest
from telethon import functions, types

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.media_fakes import MediaSDK, encoded_image
from tests.telegram_fakes import CHAT, SDK, factory
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.mark.asyncio
async def test_bot_avatar_pixels_named_sticker_set_and_platform_limits_use_real_api_methods(
    tmp_path, monkeypatch
):
    from aiogram import Bot, methods
    from aiogram import types as bot_types
    from aiogram.client.session.base import BaseSession

    from teleloom.adapters import make_adapter

    observed = []

    class API(BaseSession):
        async def close(self):
            pass

        async def stream_content(self, url, **kwargs):
            assert url.endswith("/photos/selected.png")
            yield encoded_image()

        async def make_request(self, bot, method, timeout=None):
            observed.append(method)
            self.prepare_value(method.model_dump(warnings=False), bot=bot, files={})
            if isinstance(method, methods.GetMe):
                return bot_types.User(id=123456, is_bot=True, first_name="Bot")
            if isinstance(method, methods.GetChat):
                assert method.chat_id == int(CHAT)
                return bot_types.ChatFullInfo(
                    id=int(CHAT),
                    type="supergroup",
                    title="Group",
                    accent_color_id=0,
                    max_reaction_count=1,
                    accepted_gift_types=bot_types.AcceptedGiftTypes(
                        unlimited_gifts=False,
                        limited_gifts=False,
                        unique_gifts=False,
                        premium_subscription=False,
                        gifts_from_channels=False,
                    ),
                    photo=bot_types.ChatPhoto(
                        small_file_id="small",
                        small_file_unique_id="small-id",
                        big_file_id="selected",
                        big_file_unique_id="avatar-id",
                    ),
                )
            if isinstance(method, methods.GetFile):
                assert method.file_id == "selected"
                return bot_types.File(
                    file_id="selected",
                    file_unique_id="avatar-id",
                    file_path="photos/selected.png",
                    file_size=len(encoded_image()),
                )
            if isinstance(method, methods.GetStickerSet):
                assert method.name == "exact_set"
                return bot_types.StickerSet(
                    name="exact_set",
                    title="Original set title",
                    sticker_type="regular",
                    stickers=[
                        bot_types.Sticker(
                            file_id="private-document",
                            file_unique_id="stable-sticker-id",
                            type="regular",
                            width=512,
                            height=256,
                            is_animated=False,
                            is_video=False,
                            emoji="🙂",
                        )
                    ],
                )
            raise AssertionError(type(method).__name__)

    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr("teleloom.adapters.Bot", lambda token, session: Bot(token, session=API()))
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "helper": Profile(
                kind="bot",
                polling=False,
                send_chats=[CHAT],
                read_mode="selected",
                read_chats=[CHAT],
            )
        },
    )
    async with running(settings, make_adapter) as owner, client(owner, settings) as mcp:
        listing = data(
            await mcp.call_tool("photos_list", {"profile_id": "helper", "chat_id": CHAT})
        )
        assert listing["ok"], listing
        assert listing["data"]["items"] == [{"id": "avatar-id", "is_current": True}]
        assert listing["data"]["incomplete"] and listing["data"]["history_available"] is False
        opened = await mcp.call_tool(
            "photo_open", {"profile_id": "helper", "chat_id": CHAT, "photo_id": "avatar-id"}
        )
        assert data(opened)["ok"], data(opened)
        assert any(item.type == "image" for item in opened.content)
        named = data(
            await mcp.call_tool("stickers_list", {"profile_id": "helper", "set_name": "exact_set"})
        )
        assert named["data"]["items"][0]["stickers"][0]["id"] == "stable-sticker-id"
        assert "private-document" not in str(named)
        before = len(observed)
        bare = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "helper",
                    "operation": {"kind": "upload_file", "source_path": str(tmp_path / "unused")},
                },
            )
        )
        assert bare["error"]["code"] == "backend_required" and len(observed) == before
        provider = data(
            await mcp.call_tool("gifs_search", {"profile_id": "helper", "query": "cats"})
        )
        assert provider["error"]["code"] == "unsupported_capability" and len(observed) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", ["42", CHAT])
async def test_avatar_list_open_and_sheet_use_user_or_chat_photo_sdk_paths(tmp_path, chat):
    class AvatarSDK(SDK):
        async def get_input_entity(self, value):
            return (
                types.InputPeerUser(42, 123) if chat == "42" else types.InputPeerChannel(100, 123)
            )

        async def get_entity(self, value):
            if chat == "42":
                return types.User(
                    id=42,
                    first_name="Avatar owner",
                    photo=types.UserProfilePhoto(photo_id=111, dc_id=4),
                )
            return types.Channel(
                id=100,
                title="Group",
                date=datetime.now(UTC),
                photo=types.ChatPhoto(photo_id=111, dc_id=4),
                megagroup=True,
            )

        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            photo = types.Photo(
                id=111,
                access_hash=777,
                file_reference=b"PRIVATE",
                date=datetime.now(UTC),
                sizes=[types.PhotoSize("x", 30, 20, len(encoded_image()))],
                dc_id=4,
            )
            if chat == "42":
                assert isinstance(request, functions.photos.GetUserPhotosRequest)
                return types.photos.Photos(photos=[photo], users=[])
            assert isinstance(request, functions.messages.SearchRequest)
            assert isinstance(request.filter, types.InputMessagesFilterChatPhotos)
            return types.messages.Messages(
                messages=[
                    types.MessageService(
                        id=20,
                        peer_id=types.PeerChannel(100),
                        date=datetime.now(UTC),
                        action=types.MessageActionChatEditPhoto(photo),
                    )
                ],
                topics=[],
                chats=[],
                users=[],
            )

        async def download_media(self, photo, *, file):
            assert isinstance(photo, types.Photo) and photo.id == 111
            file.write(encoded_image())

    sdk = AvatarSDK()
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", read_mode="selected", read_chats=[chat])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        listing = data(
            await mcp.call_tool(
                "photos_list", {"profile_id": "personal", "chat_id": chat, "source": "avatars"}
            )
        )
        assert listing["ok"], listing
        assert [item["id"] for item in listing["data"]["items"]] == ["111"]
        assert "PRIVATE" not in str(listing) and "777" not in str(listing)
        opened = await mcp.call_tool(
            "photo_open", {"profile_id": "personal", "chat_id": chat, "photo_id": "111"}
        )
        assert data(opened)["ok"], data(opened)
        assert any(item.type == "image" for item in opened.content)
        sheet = await mcp.call_tool(
            "photo_sheet", {"profile_id": "personal", "chat_id": chat, "source": "avatars"}
        )
        assert data(sheet)["ok"] and any(item.type == "image" for item in sheet.content)
        before = len(sdk.calls)
        denied = data(
            await mcp.call_tool("photos_list", {"profile_id": "personal", "chat_id": "43"})
        )
        assert denied["error"]["code"] == "read_not_allowed" and len(sdk.calls) == before


@pytest.mark.asyncio
async def test_installed_sticker_sets_and_inline_gif_search_have_real_sdk_rpc_shapes_and_send_handles(
    tmp_path,
):
    from types import SimpleNamespace

    class DiscoverySDK(MediaSDK):
        async def get_input_entity(self, value):
            return (
                types.InputPeerUser(50, 123)
                if value == "gif"
                else types.InputPeerChannel(100, 123456789)
            )

        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            if isinstance(request, functions.messages.GetAllStickersRequest):
                return types.messages.AllStickers(
                    hash=0,
                    sets=[
                        types.StickerSet(
                            id=1,
                            access_hash=777,
                            title="Original sticker title",
                            short_name="exact_set",
                            count=2,
                            hash=0,
                        )
                    ],
                )
            if isinstance(request, functions.help.GetConfigRequest):
                return SimpleNamespace(gif_search_username="gif")
            if isinstance(request, functions.messages.GetInlineBotResultsRequest):
                assert isinstance(request.bot, types.InputUser) and request.bot.user_id == 50
                assert (
                    isinstance(request.peer, types.InputPeerEmpty)
                    and request.query == "cat"
                    and request.offset == ""
                )
                return types.messages.BotResults(
                    query_id=9007199254740993,
                    results=[
                        types.BotInlineResult(
                            id="exact-result",
                            type="gif",
                            send_message=types.InputBotInlineMessageMediaAuto(""),
                            title="Exact GIF",
                        )
                    ],
                    cache_time=300,
                    users=[],
                    next_offset="more",
                )
            assert isinstance(request, functions.messages.SendInlineBotResultRequest)
            assert (
                request.query_id == 9007199254740993
                and request.id == "exact-result"
                and request.random_id
            )
            return types.UpdateShortSentMessage(id=20, pts=1, pts_count=1, date=datetime.now(UTC))

    sdk = DiscoverySDK()
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", send_chats=[CHAT])}
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        sets = data(await mcp.call_tool("stickers_list", {"profile_id": "personal"}))
        assert sets["ok"], sets
        assert sets["data"]["items"][0]["title"] == "Original sticker title"
        found = data(await mcp.call_tool("gifs_search", {"profile_id": "personal", "query": "cat"}))
        assert found["ok"], found
        assert found["data"]["items"][0]["id"] == "exact-result" and found["data"]["incomplete"]
        assert "query_id" not in str(found) and "777" not in str(sets)
        handle = found["data"]["items"][0]["handle"]
        plan = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "send_gif", "chat_id": CHAT, "gif_handle": handle},
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
        assert (await complete(mcp, "personal", job))["status"] == "completed"
        assert sdk.uploaded == []


@pytest.mark.asyncio
async def test_selected_message_photos_open_inline_sheet_coverage_and_safe_download(tmp_path):
    class PhotoSDK(SDK):
        async def iter_download(self, media, **kwargs):
            assert kwargs["request_size"] == 65536
            yield pixels if media.photo.id != 3 else b"Unavailable image"

        async def iter_messages(self, peer, **kwargs):
            self.calls.append(kwargs)
            rows = self.rows
            if kwargs.get("ids"):
                rows = [row for row in rows if row.id in kwargs["ids"]]
            if isinstance(kwargs.get("filter"), types.InputMessagesFilterPhotos):
                rows = rows[: kwargs["limit"]]
            for row in rows:
                yield row

    pixels = encoded_image()
    sdk = PhotoSDK()
    sdk.rows = [
        types.Message(
            id=id_,
            peer_id=types.PeerChannel(100),
            date=datetime.now(UTC),
            message=f"Exact caption {id_}",
            media=types.MessageMediaPhoto(
                photo=types.Photo(
                    id=id_,
                    access_hash=777,
                    file_reference=b"PRIVATE",
                    date=datetime.now(UTC),
                    sizes=[
                        types.PhotoSize(
                            "w", 30, 20, len(pixels) if id_ != 3 else len(b"Unavailable image")
                        )
                    ],
                    dc_id=2,
                )
            ),
        )
        for id_ in (3, 2, 1)
    ]
    root = tmp_path / "selected"
    root.mkdir()
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={
            "personal": Profile(
                kind="user", read_mode="selected", read_chats=[CHAT], file_roots=[str(root)]
            )
        },
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        listing = data(
            await mcp.call_tool(
                "photos_list",
                {"profile_id": "personal", "chat_id": CHAT, "source": "messages", "limit": 3},
            )
        )
        assert listing["ok"], listing
        assert [row["id"] for row in listing["data"]["items"]] == ["3", "2", "1"]
        opened = await mcp.call_tool(
            "photo_open", {"profile_id": "personal", "chat_id": CHAT, "message_id": "2"}
        )
        result = data(opened)
        assert result["ok"], result
        assert result["data"]["width"] == 30 and result["data"]["height"] == 20
        assert opened.content[1].type == "image" and opened.content[1].mimeType == "image/jpeg"
        assert opened.structuredContent == json.loads(opened.content[0].text)
        assert "_image" not in result["data"]
        sheet = await mcp.call_tool(
            "photo_sheet",
            {
                "profile_id": "personal",
                "chat_id": CHAT,
                "source": "messages",
                "limit": 3,
                "columns": 2,
            },
        )
        result = data(sheet)
        assert result["ok"], result
        assert [row["id"] for row in result["data"]["items"]] == ["2", "1"]
        assert result["data"]["errors"] == [{"id": "3", "error": "invalid_image"}]
        assert result["data"]["incomplete"] and sheet.content[1].type == "image"
        destination = root / "copy.png"
        downloaded = data(
            await mcp.call_tool(
                "media_download",
                {
                    "profile_id": "personal",
                    "chat_id": CHAT,
                    "message_id": "2",
                    "destination_path": str(destination),
                },
            )
        )
        assert downloaded["ok"], downloaded
        assert destination.read_bytes() == pixels
        assert list((settings.data_dir / "media-downloads").iterdir()) == []
        before = len(sdk.calls)
        denied = data(
            await mcp.call_tool(
                "photo_open",
                {"profile_id": "personal", "chat_id": "-1000000000200", "message_id": "2"},
            )
        )
        assert denied["error"]["code"] == "read_not_allowed" and len(sdk.calls) == before
