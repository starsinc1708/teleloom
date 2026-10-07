import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telethon import types

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.telegram_fakes import CHAT, SDK, factory
from tests.test_attachments_v02 import complete
from tests.test_transport import client, running

NOW = datetime(2026, 10, 5, tzinfo=UTC)


@pytest.mark.asyncio
async def test_current_native_inline_buttons_preserve_urls_without_callback_bytes(tmp_path):
    sdk = SDK()
    sdk.rows = [
        types.Message(
            id=10,
            peer_id=types.PeerChannel(100),
            date=NOW,
            message="Original",
            reply_markup=types.ReplyInlineMarkup(
                [
                    types.KeyboardInlineButtonRow(
                        [
                            types.KeyboardInlineButton(
                                text="Source",
                                type=types.InlineButtonTypeUrl("https://example.org/source"),
                            ),
                            types.KeyboardInlineButton(
                                text="Callback",
                                type=types.InlineButtonTypeCallback(data=b"PRIVATE_CALLBACK"),
                            ),
                        ]
                    )
                ]
            ),
        )
    ]
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool("messages_get", {"profile_id": "personal", "chat_id": CHAT})
        )
        assert result["ok"], result
        assert "PRIVATE_CALLBACK" not in str(result)
        assert result["data"]["items"][0]["buttons"] == [
            [
                {"text": "Source", "type": "url", "url": "https://example.org/source"},
                {"text": "Callback", "type": "callback"},
            ]
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("known_source", [False, True])
async def test_bot_external_reply_preserves_provenance_and_scoped_context(
    tmp_path, monkeypatch, known_source
):
    from aiogram import Bot
    from aiogram.methods import GetUpdates
    from aiogram.types import (
        Chat,
        ExternalReplyInfo,
        Message,
        MessageOriginChannel,
        MessageOriginUser,
        TextQuote,
        Update,
        User,
    )

    from teleloom.adapters import make_adapter
    from tests.test_bots import BotAPI

    source = Chat(id=-1000000000200, type="channel", title="Allowed source")
    external = (
        ExternalReplyInfo(
            origin=MessageOriginChannel(date=NOW, chat=source, message_id=7),
            chat=source,
            message_id=7,
        )
        if known_source
        else ExternalReplyInfo(
            origin=MessageOriginUser(
                date=NOW, sender_user=User(id=200, is_bot=False, first_name="Private origin")
            )
        )
    )
    central = Message(
        message_id=10,
        chat=Chat(id=int(CHAT), type="supergroup", title="Allowed"),
        date=NOW,
        text="Allowed publication",
        external_reply=external,
        quote=TextQuote(text="PRIVATE_EXTERNAL_QUOTE", position=0, is_manual=False),
    )
    target = Message(message_id=7, chat=source, date=NOW, text="Observed external target")

    class ReplyAPI(BotAPI):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetUpdates) and self.polls == 0:
                self.requests.append(method)
                self.polls += 1
                rows = [central, target] if known_source else [central]
                return [Update(update_id=index + 1, message=row) for index, row in enumerate(rows)]
            return await super().make_request(bot, method, timeout)

    api = ReplyAPI()
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr("teleloom.adapters.Bot", lambda token, session: Bot(token, session=api))
    profile = Profile(
        kind="bot", polling=True, read_mode="selected", read_chats=[CHAT, str(source.id)]
    )
    settings = Settings(data_dir=tmp_path, profiles={"helper": profile})
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "context_get",
                {
                    "profile_id": "helper",
                    "chat_id": CHAT,
                    "message_id": "10",
                    "context_size": 0,
                },
            )
        )
        assert result["ok"], result
        row = result["data"]["items"][0]
        if known_source:
            assert row["reply_to_chat_id"] == str(source.id)
            assert row["reply_to_message_id"] == "7"
            assert [reply["id"] for reply in result["data"]["reply_context"]] == ["7"]
            profile.read_chats = [CHAT]
            withheld = data(
                await mcp.call_tool("messages_get", {"profile_id": "helper", "chat_id": CHAT})
            )
            assert "PRIVATE_EXTERNAL_QUOTE" not in str(withheld)
            profile.read_chats.append(str(source.id))
        else:
            assert "PRIVATE_EXTERNAL_QUOTE" not in str(result)
            assert not result["data"]["reply_context"]
        profile.read_mode = "all"
        restored = data(
            await mcp.call_tool("messages_get", {"profile_id": "helper", "chat_id": CHAT})
        )
        assert restored["data"]["items"][0]["reply_quote"]["text"] == "PRIVATE_EXTERNAL_QUOTE"


@pytest.mark.asyncio
async def test_selected_policy_withholds_cross_chat_metadata_preserving_original_evidence(tmp_path):
    sdk = SDK()
    sdk.rows = [
        types.Message(
            id=10,
            peer_id=types.PeerChannel(100),
            from_id=types.PeerUser(777),
            date=NOW,
            message="Original allowed publication",
            fwd_from=types.MessageFwdHeader(
                date=NOW, from_id=types.PeerChannel(200), from_name="Private channel"
            ),
            reply_to=types.MessageReplyHeader(
                reply_to_msg_id=7,
                reply_to_peer_id=types.PeerChannel(200),
                quote_text="Private quoted context",
                quote_offset=0,
                quote=True,
            ),
            rich_message=types.RichMessage(
                blocks=[
                    types.PageBlockChannel(
                        types.Channel(
                            id=200, title="Private channel", photo=types.ChatPhotoEmpty(), date=NOW
                        )
                    )
                ],
                photos=[],
                documents=[],
            ),
        ),
    ]
    profile = Profile(kind="user", read_mode="selected", read_chats=[CHAT], sync_chats=[CHAT])
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        sync = data(await mcp.call_tool("sync_start", {"profile_id": "personal", "chat_id": CHAT}))
        state = await complete(mcp, "personal", sync["data"]["job_id"])
        assert state["status"] == "completed", state
        for source in ["live", "index"]:
            result = data(
                await mcp.call_tool(
                    "messages_get", {"profile_id": "personal", "chat_id": CHAT, "source": source}
                )
            )
            assert result["ok"], result
            item = result["data"]["items"][0]
            assert item["text"] == "Original allowed publication"
            assert item["sender_id"] == "777"
            assert item["reply_to_chat_id"] is None
            assert item["reply_quote"] is None
            assert item["forwarded_from"] == {"redacted": True}
            assert "Private channel" not in json.dumps(result)
            assert "Private quoted context" not in json.dumps(result)
            assert result["data"]["coverage"]["read_policy_redactions"] >= 3
        export = data(
            await mcp.call_tool("export_start", {"profile_id": "personal", "chat_id": CHAT})
        )
        exported = await complete(mcp, "personal", export["data"]["job_id"])
        exported_text = Path(exported["result"]["path"]).read_text(encoding="utf-8")
        assert "Original allowed publication" in exported_text
        assert "Private quoted context" not in exported_text
        assert "Private channel" not in exported_text
        profile.read_mode = "all"
        restored = data(
            await mcp.call_tool(
                "messages_get", {"profile_id": "personal", "chat_id": CHAT, "source": "index"}
            )
        )
        assert restored["data"]["items"][0]["reply_quote"]["text"] == "Private quoted context"
        assert (
            restored["data"]["items"][0]["rich_text"]["blocks"][0]["channel"]["title"]
            == "Private channel"
        )


@pytest.mark.asyncio
async def test_export_policy_is_bound_across_restart_without_deleting_originals(tmp_path):
    sdk = SDK()
    sdk.rows = [
        types.Message(
            id=10,
            peer_id=types.PeerChannel(100),
            date=NOW,
            message="Allowed publication",
            reply_to=types.MessageReplyHeader(
                reply_to_msg_id=7,
                reply_to_peer_id=types.PeerChannel(200),
                quote_text="PRIVATE_ARTIFACT_QUOTE",
                quote_offset=0,
                quote=True,
            ),
        )
    ]
    profile = Profile(kind="user", sync_chats=[CHAT])
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        sync = data(await mcp.call_tool("sync_start", {"profile_id": "personal", "chat_id": CHAT}))[
            "data"
        ]
        assert (await complete(mcp, "personal", sync["job_id"]))["status"] == "completed"
        exported = data(
            await mcp.call_tool("export_start", {"profile_id": "personal", "chat_id": CHAT})
        )["data"]
        state = await complete(mcp, "personal", exported["job_id"])
        original = Path(state["result"]["path"])
        assert "PRIVATE_ARTIFACT_QUOTE" in original.read_text(encoding="utf-8")
    profile.read_mode = "selected"
    profile.read_chats = [CHAT]
    async with running(settings, factory(SDK())) as app, client(app, settings) as mcp:
        stale = data(
            await mcp.call_tool(
                "jobs_status", {"profile_id": "personal", "job_id": exported["job_id"]}
            )
        )
        assert stale["error"]["code"] == "read_policy_changed"
        assert "path" not in json.dumps(stale)
        fresh = data(
            await mcp.call_tool("export_start", {"profile_id": "personal", "chat_id": CHAT})
        )["data"]
        fresh_state = await complete(mcp, "personal", fresh["job_id"])
        assert "PRIVATE_ARTIFACT_QUOTE" not in Path(fresh_state["result"]["path"]).read_text(
            encoding="utf-8"
        )
        profile.read_mode = "all"
        restored = data(
            await mcp.call_tool(
                "jobs_status", {"profile_id": "personal", "job_id": exported["job_id"]}
            )
        )
        assert restored["ok"] and Path(restored["data"]["result"]["path"]) == original
        assert "PRIVATE_ARTIFACT_QUOTE" in original.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_partial_native_rich_content_is_reported_in_page_coverage(tmp_path):
    sdk = SDK()
    sdk.rows = [
        types.Message(
            id=10,
            peer_id=types.PeerChannel(100),
            date=NOW,
            message="",
            rich_message=types.RichMessage(
                part=True,
                blocks=[types.PageBlockParagraph(types.TextPlain("Available part"))],
                photos=[],
                documents=[],
            ),
        )
    ]
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool("messages_get", {"profile_id": "personal", "chat_id": CHAT})
        )["data"]
        assert result["items"][0]["rich_text"]["partial"] is True
        assert result["items"][0]["text"] == "Available part"
        assert result["incomplete"] is True
        assert result["coverage"]["partial_rich_messages"] == 1
        assert any("rich content" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_original_entities_quotes_and_native_blocks_survive_public_read(tmp_path):
    sdk = SDK()
    sdk.rows = [
        types.Message(
            id=10,
            peer_id=types.PeerChannel(100),
            date=NOW,
            message="😀 link 🐈",
            entities=[
                types.MessageEntityTextUrl(offset=3, length=4, url="https://example.org/source"),
                types.MessageEntityCustomEmoji(offset=8, length=2, document_id=9007199254740993),
            ],
            reply_to=types.MessageReplyHeader(
                reply_to_msg_id=7,
                quote_text="😀 exact quote",
                quote_offset=3,
                quote=True,
                quote_entities=[types.MessageEntityBold(offset=0, length=2)],
            ),
        ),
        types.Message(
            id=9,
            peer_id=types.PeerChannel(100),
            date=NOW,
            message="",
            rich_message=types.RichMessage(
                blocks=[
                    types.PageBlockHeading1(types.TextBold(types.TextPlain("Exact title"))),
                    types.PageBlockParagraph(
                        types.TextConcat(
                            [
                                types.TextUrl(types.TextPlain("source"), "https://example.org", 0),
                                types.TextPlain(" "),
                                types.TextCustomEmoji(9007199254740995, "🐈"),
                            ]
                        )
                    ),
                    types.PageBlockBlockquote(types.TextPlain("quote"), types.TextEmpty()),
                    types.PageBlockPreformatted(types.TextPlain("x = 1\n"), "python"),
                ],
                photos=[],
                documents=[],
            ),
        ),
    ]
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        wire = await mcp.call_tool("messages_get", {"profile_id": "personal", "chat_id": CHAT})
        result = data(wire)
        assert result["ok"], result
        classic, block = result["data"]["items"]
        assert classic["text"] == "😀 link 🐈" and classic["text_source"] == "original"
        assert classic["custom_emojis"] == [{"emoji": "🐈", "id": "9007199254740993"}]
        assert classic["entities"][0]["offset"] == 3
        assert classic["reply_quote"] == {
            "text": "😀 exact quote",
            "offset": 3,
            "offset_unit": "utf16",
            "manual": None,
            "selected": True,
            "entities": [{"_": "MessageEntityBold", "offset": 0, "length": 2}],
        }
        assert block["text"] == "Exact title\n\nsource 🐈\n\nquote\n\nx = 1\n"
        assert block["text_source"] == "reconstructed" and block["original_text"] == ""
        assert block["rich_text"]["blocks"][0] == {
            "_": "PageBlockHeading1",
            "text": {"_": "TextBold", "text": {"_": "TextPlain", "text": "Exact title"}},
        }
        assert block["custom_emojis"] == [{"emoji": "🐈", "id": "9007199254740995"}]
        assert wire.structuredContent == json.loads(wire.content[0].text)
        projected = data(
            await mcp.call_tool(
                "messages_get", {"profile_id": "personal", "chat_id": CHAT, "fields": ["text"]}
            )
        )
        assert projected["data"]["items"][1]["text_source"] == "reconstructed"
        assert projected["data"]["items"][1]["original_text"] == ""
        selection = data(
            await mcp.call_tool(
                "response_fields_select",
                {"tool_name": "messages_get", "request": "Read with source links"},
            )
        )["data"]
        assert "rich_text" in selection["fields"]
        linked = data(
            await mcp.call_tool(
                "messages_get",
                {"profile_id": "personal", "chat_id": CHAT, "fields": selection["fields"]},
            )
        )
        assert (
            linked["data"]["items"][1]["rich_text"]["blocks"][1]["text"]["texts"][0]["url"]
            == "https://example.org"
        )


@pytest.mark.asyncio
async def test_safe_media_previews_attribution_and_bot_quote_metadata(tmp_path):
    from aiogram.types import Chat as BotChat
    from aiogram.types import Message as BotMessage
    from aiogram.types import TextQuote, User, Voice

    from teleloom.adapters import BotAdapter, bot_message

    sdk = SDK()
    sdk.rows = [
        types.Message(
            id=10,
            peer_id=types.PeerChannel(100),
            date=NOW,
            message="URL caption",
            media=types.MessageMediaWebPage(
                webpage=types.WebPage(
                    id=55,
                    url="https://example.org/source",
                    display_url="example.org",
                    hash=0,
                    title="Source title",
                    description="Exact description",
                    photo=types.Photo(
                        id=1,
                        access_hash=123456789,
                        file_reference=b"PRIVATE_FILE_REFERENCE",
                        date=NOW,
                        sizes=[],
                        dc_id=2,
                    ),
                )
            ),
        )
    ]
    settings = Settings(data_dir=tmp_path / "user", profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool("messages_get", {"profile_id": "personal", "chat_id": CHAT})
        )
        row = result["data"]["items"][0]
        assert row["web_preview"] == {
            "url": "https://example.org/source",
            "display_url": "example.org",
            "title": "Source title",
            "description": "Exact description",
        }
        assert row["media"]["kind"] == "web_preview"
        assert "PRIVATE_FILE_REFERENCE" not in str(result) and "123456789" not in str(result)

    class SavedBot(BotAdapter):
        async def start(self):
            pass

        async def close(self):
            pass

    def bot_factory(profile_id, profile, store, credentials):
        adapter = object.__new__(SavedBot)
        adapter.profile_id, adapter.profile, adapter.store = profile_id, profile, store
        raw = BotMessage(
            message_id=2,
            chat=BotChat(id=-1000000000100, type="supergroup", title="Observed"),
            date=NOW,
            from_user=User(id=7, is_bot=False, first_name="Ada"),
            caption="🦉 caption",
            voice=Voice(
                file_id="selected-voice",
                file_unique_id="stable-voice",
                duration=12,
                mime_type="audio/ogg",
                file_size=123,
            ),
            quote=TextQuote(text="Exact quote", position=3, is_manual=False),
        )
        store.save_messages([bot_message(profile_id, raw)])
        return adapter

    settings = Settings(data_dir=tmp_path / "bot", profiles={"helper": Profile(kind="bot")})
    async with running(settings, bot_factory) as app, client(app, settings) as mcp:
        await mcp.call_tool("chats_list", {"profile_id": "helper"})
        result = data(
            await mcp.call_tool("messages_get", {"profile_id": "helper", "chat_id": CHAT})
        )
        row = result["data"]["items"][0]
        assert row["media"]["duration"] == 12 and row["media"]["file_unique_id"] == "stable-voice"
        assert row["reply_quote"]["text"] == "Exact quote" and row["reply_quote"]["manual"] is False
        assert row["text_source"] == "original" and row["text"] == "🦉 caption"
        assert result["data"]["source"] == "bot_updates" and result["data"]["incomplete"] is True
