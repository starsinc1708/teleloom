from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.fixture
def message_sdk(monkeypatch):
    requests = []
    state = {"requests": requests, "batches": 0}
    date = datetime.now(UTC)

    def message(id_=4):
        return types.Message(
            id=id_, peer_id=types.PeerUser(100), date=date, message="Decision 4", out=True
        )

    class SDK(TelegramClient):
        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return types.User(
                id=1, first_name="Owner", premium=True, bot=state.get("owner_bot", False)
            )

        async def get_input_entity(self, entity):
            if isinstance(entity, int):
                return types.InputPeerUser(entity, 99)
            return entity

        async def get_entity(self, entity):
            return types.User(id=entity.user_id, access_hash=99, first_name="Target")

        async def iter_messages(self, peer, **kwargs):
            for id_ in kwargs.get("ids", [4]):
                yield message(id_)

        async def get_messages(self, peer, **kwargs):
            raw = message(kwargs.get("ids", 4))
            raw.reply_markup = types.ReplyInlineMarkup(
                [
                    types.KeyboardInlineButtonRow(
                        [
                            types.KeyboardInlineButton(
                                text="Approve",
                                type=types.InlineButtonTypeCallback(data=b"exact-callback"),
                            )
                        ]
                    )
                ]
            )
            return raw

        async def __call__(self, request, *args, **kwargs):
            # Exercise real TL encoding, not just constructor attributes.
            bytes(request)
            requests.append(request)
            if isinstance(
                request,
                (
                    functions.messages.SendMessageRequest,
                    functions.messages.SendMediaRequest,
                    functions.messages.SendInlineBotResultRequest,
                ),
            ):
                if state.get("scheduled_acceptance") and request.schedule_date:
                    scheduled = message(301)
                    scheduled.date = request.schedule_date
                    return types.Updates(
                        updates=[types.UpdateNewScheduledMessage(message=scheduled)],
                        users=[],
                        chats=[],
                        date=date,
                        seq=1,
                    )
                return types.Updates(
                    updates=[types.UpdateMessageID(id=101, random_id=request.random_id)],
                    users=[],
                    chats=[],
                    date=date,
                    seq=1,
                )
            if isinstance(request, functions.messages.ForwardMessagesRequest):
                return types.Updates(
                    updates=[
                        types.UpdateMessageID(id=101 + i, random_id=id_)
                        for i, id_ in enumerate(request.random_id)
                    ],
                    users=[],
                    chats=[],
                    date=date,
                    seq=1,
                )
            if isinstance(request, functions.channels.GetSendAsRequest):
                return SimpleNamespace(
                    peers=[SimpleNamespace(peer=types.PeerUser(200), premium_required=True)]
                )
            if isinstance(request, functions.messages.GetScheduledHistoryRequest):
                return SimpleNamespace(messages=[message(4)])
            if isinstance(request, functions.messages.GetBotCallbackAnswerRequest):
                return types.messages.BotCallbackAnswer(
                    cache_time=0, message="Accepted", alert=False
                )
            if isinstance(request, functions.messages.GetInlineBotResultsRequest):
                return types.messages.BotResults(
                    query_id=91,
                    cache_time=0,
                    users=[],
                    results=[
                        types.BotInlineResult(
                            id="one",
                            type="article",
                            send_message=types.BotInlineMessageText(
                                message="Inline approved", entities=[]
                            ),
                        )
                    ],
                )
            if isinstance(request, functions.messages.GetPeerDialogsRequest):
                return SimpleNamespace(
                    dialogs=[
                        SimpleNamespace(
                            peer=types.PeerUser(100),
                            draft=state.get(
                                "draft", types.DraftMessage(message="Draft original", date=date)
                            ),
                        )
                    ]
                )
            if isinstance(request, functions.messages.GetAllDraftsRequest):
                return types.Updates(
                    updates=state.get("all_drafts", []), users=[], chats=[], date=date, seq=1
                )
            if isinstance(request, functions.messages.GetMessageReadParticipantsRequest):
                return [types.ReadParticipantDate(user_id=200, date=date)]
            if isinstance(request, functions.messages.GetMessageReactionsListRequest):
                return SimpleNamespace(
                    reactions=[
                        SimpleNamespace(
                            peer_id=types.PeerUser(200),
                            reaction=types.ReactionCustomEmoji(document_id=123),
                            date=date,
                        )
                    ],
                    next_offset=None,
                )
            return True

    session = StringSession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(b"\x01" * 256)
    monkeypatch.setenv("TELELOOM_PERSONAL_SESSION", session.save())
    monkeypatch.setenv("TELELOOM_PERSONAL_API_HASH", "synthetic-api-hash")
    monkeypatch.setattr("teleloom.adapters.TelegramClient", SDK)
    return state


OPERATIONS = [
    (
        {
            "kind": "send",
            "text": '<b>Title</b> <tg-emoji emoji-id="123">😀</tg-emoji>',
            "format": "html",
            "reply_to_message_id": "4",
            "quote_text": "Decision",
            "quote_offset": 0,
        },
        functions.messages.SendMessageRequest,
    ),
    (
        {
            "kind": "send",
            "text": "# Block\n| A | B |\n|---|---|\n| 1 | 2 |",
            "format": "rich_markdown",
        },
        functions.messages.SendMessageRequest,
    ),
    (
        {
            "kind": "send",
            "text": "Later",
            "schedule_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        functions.messages.SendMessageRequest,
    ),
    (
        {"kind": "edit", "message_id": "4", "text": "__Changed__", "format": "markdown"},
        functions.messages.EditMessageRequest,
    ),
    ({"kind": "delete", "message_ids": ["3", "4"]}, functions.messages.DeleteMessagesRequest),
    (
        {
            "kind": "forward",
            "source_chat_id": "100",
            "message_ids": ["3", "4"],
            "send_as": "200",
            "top_message_id": "4",
            "drop_author": True,
            "silent": True,
        },
        functions.messages.ForwardMessagesRequest,
    ),
    (
        {"kind": "reaction", "message_id": "4", "reactions": ["custom:123"]},
        functions.messages.SendReactionRequest,
    ),
    (
        {"kind": "reaction", "message_id": "4", "reactions": []},
        functions.messages.SendReactionRequest,
    ),
    (
        {
            "kind": "poll",
            "question": "Choose",
            "options": ["A", "B"],
            "quiz": True,
            "correct_option": 1,
        },
        functions.messages.SendMediaRequest,
    ),
    (
        {"kind": "contact_send", "first_name": "Approved", "phone_number": "+10000000000"},
        functions.messages.SendMediaRequest,
    ),
    ({"kind": "pin", "message_id": "4"}, functions.messages.UpdatePinnedMessageRequest),
    (
        {"kind": "pin", "message_id": "4", "unpin": True},
        functions.messages.UpdatePinnedMessageRequest,
    ),
    ({"kind": "unpin_all"}, functions.messages.UnpinAllMessagesRequest),
    ({"kind": "read_ack", "through_message_id": "4"}, functions.messages.ReadHistoryRequest),
    (
        {"kind": "delete_history", "through_message_id": "4", "revoke": True},
        functions.messages.DeleteHistoryRequest,
    ),
    ({"kind": "mute", "muted": True}, functions.account.UpdateNotifySettingsRequest),
    ({"kind": "archive", "archived": True}, functions.folders.EditPeerFoldersRequest),
    (
        {"kind": "draft_save", "text": "Draft", "reply_to_message_id": "4"},
        functions.messages.SaveDraftRequest,
    ),
    ({"kind": "draft_clear"}, functions.messages.SaveDraftRequest),
    (
        {"kind": "scheduled_delete", "message_ids": ["4"]},
        functions.messages.DeleteScheduledMessagesRequest,
    ),
    (
        {"kind": "inline_callback", "message_id": "4", "button_index": 0},
        functions.messages.GetBotCallbackAnswerRequest,
    ),
    (
        {"kind": "inline_send", "bot_id": "200", "query": "approved", "result_id": "one"},
        functions.messages.SendInlineBotResultRequest,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,request_type",
    OPERATIONS,
    ids=[item[0]["kind"] + str(i) for i, item in enumerate(OPERATIONS)],
)
async def test_http_confirmed_operations_use_real_serializable_telethon_requests(
    tmp_path, message_sdk, operation, request_type
):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(kind="user", api_id=1, send_chats=["100"], mutation_chats=["100"])
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        preview = data(
            await mcp.call_tool(
                "message_operation_preview",
                {"profile_id": "personal", "operation": {"chat_id": "100", **operation}},
            )
        )
        assert preview["ok"], preview.get("error")
        plan = preview["data"]
        assert not any(isinstance(request, request_type) for request in message_sdk["requests"])
        result = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]
        finished = await complete(mcp, "personal", result["job_id"])
        assert finished["status"] == "completed", finished
        writes = [
            request for request in message_sdk["requests"] if isinstance(request, request_type)
        ]
        assert len(writes) == 1
        request = writes[0]
        if operation.get("format") == "html":
            assert request.message == "Title 😀"
            assert isinstance(request.entities[0], types.MessageEntityBold)
            assert request.entities[1].document_id == 123
            assert request.reply_to.quote_text == "Decision"
        if operation.get("format") == "rich_markdown":
            assert request.message == ""
            assert request.rich_message.markdown.startswith("# Block")
        if operation.get("schedule_at"):
            assert finished["deliveries"][0]["receipt"]["delivery_confirmed"] is False
        if operation["kind"] == "forward":
            assert request.send_as.user_id == 200
            assert request.top_msg_id == 4 and request.drop_author and request.silent
            assert request.reply_to is None
        if operation["kind"] == "poll":
            assert request.media.poll.hash == 0
            assert request.media.correct_answers == [1]
        if operation["kind"] == "inline_callback":
            assert request.data == b"exact-callback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["scheduled", "drafts", "buttons", "send_as", "reactions", "read_receipts", "inline_results"],
)
async def test_message_state_readers_use_real_user_sdk_shapes(tmp_path, message_sdk, kind):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user", api_id=1)})
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "message_state",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "kind": kind,
                    "message_id": "4",
                    "bot_id": "200",
                },
            )
        )
        assert result["ok"], result.get("error")
        assert len(result["data"]["items"]) == 1
        assert "access_hash" not in str(result)
        assert "phone_number" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("original", ["Original draft", ""])
async def test_draft_reader_preserves_native_formatting_and_distinguishes_rich_reconstruction(
    tmp_path, message_sdk, original
):
    message_sdk["draft"] = types.DraftMessage(
        message=original,
        date=datetime.now(UTC),
        entities=[types.MessageEntityBold(0, 5)] if original else [],
        rich_message=types.RichMessage(
            blocks=[types.PageBlockParagraph(types.TextPlain("Rich draft evidence"))],
            photos=[],
            documents=[],
        ),
    )
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user", api_id=1)})
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "message_state", {"profile_id": "personal", "chat_id": "100", "kind": "drafts"}
            )
        )
        assert result["ok"], result
        draft = result["data"]["items"][0]
        assert draft["text"] == (original or "Rich draft evidence")
        assert draft["rich_text"]["reconstructed_text"] == "Rich draft evidence"
        if original:
            assert draft["entities"][0]["_"] == "MessageEntityBold"
            assert draft["entities"][0]["length"] == 5
        else:
            assert draft["text_source"] == "reconstructed" and draft["original_text"] == ""


@pytest.mark.asyncio
async def test_rich_draft_withholds_private_peer_metadata_and_external_quote(tmp_path, message_sdk):
    date = datetime.now(UTC)
    message_sdk["draft"] = types.DraftMessage(
        message="Allowed draft",
        date=date,
        reply_to=types.InputReplyToMessage(
            reply_to_msg_id=7,
            reply_to_peer_id=types.InputPeerChannel(200, 99),
            quote_text="PRIVATE_DRAFT_QUOTE",
        ),
        rich_message=types.RichMessage(
            blocks=[
                types.PageBlockChannel(
                    types.Channel(
                        id=200,
                        title="PRIVATE_DRAFT_CHANNEL",
                        photo=types.ChatPhotoEmpty(),
                        date=date,
                    )
                )
            ],
            photos=[],
            documents=[],
        ),
    )
    profile = Profile(kind="user", api_id=1, read_mode="selected", read_chats=["100"])
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "message_state", {"profile_id": "personal", "chat_id": "100", "kind": "drafts"}
            )
        )
        assert result["ok"], result
        assert "PRIVATE_DRAFT" not in str(result), result
        profile.read_mode = "all"
        restored = data(
            await mcp.call_tool(
                "message_state", {"profile_id": "personal", "chat_id": "100", "kind": "drafts"}
            )
        )
        assert "PRIVATE_DRAFT_CHANNEL" in str(restored)
        assert restored["data"]["items"][0]["reply_quote"]["text"] == "PRIVATE_DRAFT_QUOTE"
