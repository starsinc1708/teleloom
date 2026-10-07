import asyncio
from datetime import UTC, datetime

import pytest
from aiogram import Bot, methods, types
from aiogram.client.session.base import BaseSession

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.fixture
def bot_message_sdk(monkeypatch):
    requests = []

    class API(BaseSession):
        polls = 0

        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            yield b""

        async def make_request(self, bot, method, timeout=None):
            requests.append(method)
            self.prepare_value(method.model_dump(warnings=False), bot=bot, files={})
            chat = types.Chat(id=100, type="private", first_name="Target")
            user = types.User(id=123456, is_bot=True, first_name="Bot")
            if isinstance(method, methods.GetMe):
                return user
            if isinstance(method, methods.GetWebhookInfo):
                return types.WebhookInfo(
                    url="", has_custom_certificate=False, pending_update_count=0
                )
            if isinstance(method, methods.GetChat):
                return chat
            if isinstance(method, methods.GetUpdates):
                if self.polls:
                    await asyncio.Event().wait()
                self.polls += 1
                return [
                    types.Update(
                        update_id=9,
                        message=types.Message(
                            message_id=4,
                            date=datetime.now(UTC),
                            chat=chat,
                            from_user=user,
                            text="Observed source",
                        ),
                    )
                ]
            if isinstance(method, methods.ForwardMessages):
                return [types.MessageId(message_id=101)]
            if isinstance(
                method,
                (
                    methods.SendMessage,
                    methods.SendRichMessage,
                    methods.SendPoll,
                    methods.SendContact,
                    methods.EditMessageText,
                ),
            ):
                return types.Message(
                    message_id=101,
                    date=datetime.now(UTC),
                    chat=chat,
                    from_user=user,
                    text="Approved",
                    entities=getattr(method, "entities", None),
                    poll=types.Poll(
                        id="poll-1",
                        question="Choose",
                        options=[
                            types.PollOption(persistent_id="a", text="A", voter_count=0),
                            types.PollOption(persistent_id="b", text="B", voter_count=0),
                        ],
                        total_voter_count=0,
                        is_closed=False,
                        is_anonymous=True,
                        type="regular",
                        allows_multiple_answers=False,
                        allows_revoting=False,
                        members_only=False,
                    )
                    if isinstance(method, methods.SendPoll)
                    else None,
                )
            return True

    api = API()
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr("teleloom.adapters.Bot", lambda token, session: Bot(token, session=api))
    return requests


BOT_CASES = [
    (
        {"kind": "send", "text": "<b>Bold</b>", "format": "html", "reply_to_message_id": "4"},
        methods.SendMessage,
    ),
    ({"kind": "send", "text": "# Block", "format": "rich_markdown"}, methods.SendRichMessage),
    ({"kind": "edit", "message_id": "4", "text": "Edit"}, methods.EditMessageText),
    ({"kind": "delete", "message_ids": ["4"]}, methods.DeleteMessages),
    (
        {"kind": "forward", "source_chat_id": "100", "message_ids": ["4"], "top_message_id": "4"},
        methods.ForwardMessages,
    ),
    ({"kind": "reaction", "message_id": "4", "reactions": ["👍"]}, methods.SetMessageReaction),
    ({"kind": "poll", "question": "Choose", "options": ["A", "B"]}, methods.SendPoll),
    (
        {"kind": "contact_send", "phone_number": "+10000000000", "first_name": "Owner-approved"},
        methods.SendContact,
    ),
    ({"kind": "pin", "message_id": "4"}, methods.PinChatMessage),
    ({"kind": "pin", "message_id": "4", "unpin": True}, methods.UnpinChatMessage),
    ({"kind": "unpin_all"}, methods.UnpinAllChatMessages),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,method_type",
    BOT_CASES,
    ids=[f"{item[0]['kind']}{i}" for i, item in enumerate(BOT_CASES)],
)
async def test_public_confirmed_message_operations_use_aiogram_methods(
    tmp_path, bot_message_sdk, operation, method_type
):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "helper": Profile(kind="bot", polling=True, send_chats=["100"], mutation_chats=["100"])
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        await asyncio.sleep(0.03)
        preview = data(
            await mcp.call_tool(
                "message_operation_preview",
                {"profile_id": "helper", "operation": {"chat_id": "100", **operation}},
            )
        )
        assert preview["ok"], preview.get("error")
        plan = preview["data"]
        assert not any(isinstance(method, method_type) for method in bot_message_sdk)
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "helper",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        finished = await complete(mcp, "helper", job)
        assert finished["status"] == "completed", finished
        written = [method for method in bot_message_sdk if isinstance(method, method_type)]
        assert len(written) == 1
        if operation.get("format") == "html":
            assert written[0].text == "Bold"
            assert written[0].entities[0].type == "bold"
        if operation["kind"] == "forward":
            assert written[0].message_thread_id == 4
        if operation["kind"] == "send":
            observed = data(
                await mcp.call_tool(
                    "messages_get",
                    {"profile_id": "helper", "chat_id": "100", "message_ids": ["101"]},
                )
            )["data"]["items"]
            assert len(observed) == 1 and observed[0]["id"] == "101" and observed[0]["outgoing"]


@pytest.mark.asyncio
async def test_backend_limits_are_rejected_at_preview_without_aiogram_writes(
    tmp_path, bot_message_sdk
):
    settings = Settings(
        data_dir=tmp_path,
        profiles={"helper": Profile(kind="bot", send_chats=["100"], mutation_chats=["100"])},
    )
    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        for operation in [
            {"kind": "draft_save", "text": "Draft"},
            {"kind": "read_ack", "through_message_id": "4"},
            {"kind": "send", "text": "Scheduled", "schedule_at": "2090-01-01T00:00:00Z"},
            {"kind": "forward", "source_chat_id": "100", "message_ids": ["4"], "drop_author": True},
        ]:
            result = data(
                await mcp.call_tool(
                    "message_operation_preview",
                    {"profile_id": "helper", "operation": {"chat_id": "100", **operation}},
                )
            )
            assert result["error"]["code"] == "unsupported_capability"
        assert all(
            isinstance(method, (methods.GetMe, methods.GetWebhookInfo))
            for method in bot_message_sdk
        )
