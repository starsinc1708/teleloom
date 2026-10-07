import asyncio
from datetime import timedelta

import pytest

from teleloom.config import Profile, Settings
from teleloom.models import utcnow
from tests.fakes import data
from tests.test_jobs import complete
from tests.test_message_operations import MessageAPI
from tests.test_transport import client, running


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,read_chats",
    [
        (
            {"kind": "forward", "chat_id": "100", "source_chat_id": "200", "message_ids": ["4"]},
            ["100"],
        ),
        ({"kind": "edit", "chat_id": "100", "message_id": "4", "text": "Changed"}, ["200"]),
        ({"kind": "send", "chat_id": "100", "text": "Approved", "send_as": "200"}, ["100"]),
        ({"kind": "inline_send", "chat_id": "100", "bot_id": "200", "result_id": "3"}, ["100"]),
    ],
)
async def test_mutation_denies_unreadable_inspected_peer_before_connecting(
    tmp_path, operation, read_chats
):
    connected = []

    def factory(*args):
        connected.append(True)
        return MessageAPI(*args)

    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user",
                read_mode="selected",
                read_chats=read_chats,
                send_chats=["100"],
                mutation_chats=["100"],
            )
        },
    )
    async with running(settings, factory) as app, client(app, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "work",
                    "operation": operation,
                },
            )
        )
        assert result["error"]["code"] == "read_not_allowed", result
        assert not connected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"chat_id": "200", "kind": "drafts"},
        {"chat_id": "100", "kind": "inline_results", "bot_id": "200"},
    ],
)
async def test_message_state_denies_unreadable_peer_before_connecting(tmp_path, args):
    connected = []

    def factory(*args):
        connected.append(True)
        return MessageAPI(*args)

    settings = Settings(
        data_dir=tmp_path,
        profiles={"work": Profile(kind="user", read_mode="selected", read_chats=["100"])},
    )
    async with running(settings, factory) as app, client(app, settings) as mcp:
        result = data(await mcp.call_tool("message_state", {"profile_id": "work", **args}))
        assert result["error"]["code"] == "read_not_allowed", result
        assert not connected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("read-only", {"message_state"}), ("selected", set())])
async def test_new_mutation_tools_follow_actual_exposure_dispatch(tmp_path, mode, expected):
    settings = Settings(data_dir=tmp_path, exposure_mode=mode)
    async with running(settings, MessageAPI) as app, client(app, settings) as mcp:
        tools = {tool.name for tool in (await mcp.list_tools()).tools}
        assert tools & {"message_state", "message_operation_preview"} == expected
        hidden = await mcp.call_tool(
            "message_operation_preview",
            {
                "profile_id": "work",
                "operation": {"kind": "send", "chat_id": "100", "text": "Hidden"},
            },
        )
        assert hidden.isError and "Unknown tool" in hidden.content[0].text


@pytest.mark.asyncio
async def test_read_revocation_denies_mutation_execution_and_job_evidence(tmp_path):
    MessageAPI.operations = []
    profile = Profile(kind="user", send_chats=["100"], read_mode="selected", read_chats=["100"])
    settings = Settings(data_dir=tmp_path, profiles={"work": profile})
    async with running(settings, MessageAPI) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "work",
                    "operation": {"kind": "send", "chat_id": "100", "text": "Reviewed"},
                },
            )
        )["data"]
        args = {
            "profile_id": "work",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        profile.read_chats.clear()
        assert (
            data(await mcp.call_tool("delivery_execute", args))["error"]["code"]
            == "read_not_allowed"
        )
        profile.read_chats.append("100")
        job = data(await mcp.call_tool("delivery_execute", args))["data"]["job_id"]
        assert (await complete(mcp, "work", job))["status"] == "completed"
        profile.read_chats.clear()
        assert (
            data(await mcp.call_tool("jobs_status", {"profile_id": "work", "job_id": job}))[
                "error"
            ]["code"]
            == "read_not_allowed"
        )
        assert (
            data(await mcp.call_tool("delivery_execute", args))["error"]["code"]
            == "read_not_allowed"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args",
    [
        ("delivery_preview", {"recipients": ["100"], "text": "Reviewed"}),
        (
            "message_operation_preview",
            {"operation": {"kind": "send", "chat_id": "100", "text": "Reviewed"}},
        ),
    ],
)
async def test_complete_preview_hash_binds_resolved_target_description(tmp_path, tool, args):
    settings = Settings(
        data_dir=tmp_path, profiles={"work": Profile(kind="user", send_chats=["100"])}
    )
    async with running(settings, MessageAPI) as app, client(app, settings) as mcp:
        plan = data(await mcp.call_tool(tool, {"profile_id": "work", **args}))["data"]
        assert plan["preview"]["resolved_targets"][0]["id"] == "100"
        assert plan["preview"]["resolved_targets"][0]["title"] == "Engineering"
        assert plan["preview"]["resolved_targets"] == plan["resolved_recipients"]


@pytest.mark.asyncio
async def test_forward_budget_counts_all_confirmed_messages_before_any_send(tmp_path, monkeypatch):
    clock = [utcnow()]
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock[0])
    MessageAPI.operations = []
    settings = Settings(
        data_dir=tmp_path, profiles={"work": Profile(kind="user", send_chats=["100"])}
    )
    settings.limits.daily_messages = 1
    async with running(settings, MessageAPI) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "work",
                    "operation": {
                        "kind": "forward",
                        "chat_id": "100",
                        "source_chat_id": "100",
                        "message_ids": ["3", "4"],
                    },
                },
            )
        )["data"]
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        for _ in range(40):
            state = data(await mcp.call_tool("jobs_status", {"profile_id": "work", "job_id": job}))[
                "data"
            ]
            if state["next_run"] or state["status"] == "completed":
                break
            await asyncio.sleep(0.03)
        assert state["status"] == "queued" and state["next_run"] > 0, state
        assert not MessageAPI.operations
        settings.limits.daily_messages = 2
        clock[0] += timedelta(days=1)
        assert (await complete(mcp, "work", job))["status"] == "completed"
        second = data(
            await mcp.call_tool(
                "delivery_preview",
                {
                    "profile_id": "work",
                    "recipients": ["100"],
                    "text": "One more",
                },
            )
        )["data"]
        second_job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": second["plan_id"],
                    "plan_hash": second["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        clock[0] += timedelta(seconds=6)
        for _ in range(40):
            state = data(
                await mcp.call_tool("jobs_status", {"profile_id": "work", "job_id": second_job})
            )["data"]
            if state["next_run"]:
                break
            await asyncio.sleep(0.03)
        assert state["status"] == "queued" and state["next_run"] > clock[0].timestamp()


@pytest.mark.asyncio
async def test_unpin_all_preview_freezes_current_state_semantics_instead_of_reviewed_ids(tmp_path):
    settings = Settings(
        data_dir=tmp_path, profiles={"work": Profile(kind="user", mutation_chats=["100"])}
    )
    async with running(settings, MessageAPI) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "work",
                    "operation": {"kind": "unpin_all", "chat_id": "100", "max_requests": 2},
                },
            )
        )["data"]
        semantics = plan["preview"]["operation"]["scope_semantics"]
        assert "all current pins" in semantics and "added after preview" in semantics
        assert "not atomic" in semantics


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["reply_quote", "rich_text", "original_text"])
async def test_reviewed_original_and_rich_source_changes_are_not_executed(tmp_path, field):
    apis = []

    class RichSource(MessageAPI):
        def __init__(self, *args):
            super().__init__(*args)
            row = self.rows[3]
            row.reply_to_chat_id, row.reply_to_message_id = "100", "3"
            row.reply_external = True
            row.reply_quote = {"text": "Reviewed quote", "offset": 0, "offset_unit": "utf16"}
            row.rich_text = {
                "blocks": [
                    {"_": "PageBlockParagraph", "text": {"_": "TextPlain", "text": "Reviewed rich"}}
                ],
                "reconstructed_text": "Reviewed rich",
            }
            row.original_text = "Original evidence"
            apis.append(self)

    settings = Settings(
        data_dir=tmp_path, profiles={"work": Profile(kind="user", mutation_chats=["100"])}
    )
    RichSource.operations = []
    async with running(settings, RichSource) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "work",
                    "operation": {
                        "kind": "edit",
                        "chat_id": "100",
                        "message_id": "4",
                        "text": "Reviewed replacement",
                    },
                },
            )
        )["data"]
        reviewed = plan["source_messages"][0]
        assert reviewed["reply_quote"]["text"] == "Reviewed quote"
        assert reviewed["reply_external"] is True
        assert reviewed["rich_text"]["reconstructed_text"] == "Reviewed rich"
        assert reviewed["original_text"] == "Original evidence"
        if field == "original_text":
            apis[0].rows[3].original_text = "Changed original evidence"
        elif field == "reply_quote":
            apis[0].rows[3].reply_quote["text"] = "Changed quote"
        else:
            apis[0].rows[3].rich_text["blocks"][0]["text"]["text"] = "Changed rich"
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        final = await complete(mcp, "work", job)
        assert final["status"] == "failed" and final["error"]["code"] == "source_changed"
        assert not RichSource.operations


@pytest.mark.asyncio
async def test_queued_forward_rechecks_revoked_source_before_more_telegram_reads(
    tmp_path, monkeypatch
):
    clock = [utcnow()]
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock[0])
    reads = []

    class SourceAPI(MessageAPI):
        def __init__(self, *args):
            super().__init__(*args)
            self.rows.extend([row.model_copy(update={"chat_id": "200"}) for row in self.rows])

        async def history(self, chat, **kwargs):
            reads.append(chat)
            return await super().history(chat, **kwargs)

    SourceAPI.operations = []
    profile = Profile(
        kind="user", read_mode="selected", read_chats=["100", "200"], send_chats=["100"]
    )
    settings = Settings(data_dir=tmp_path, profiles={"work": profile})
    settings.limits.daily_messages = 1
    async with running(settings, SourceAPI) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "work",
                    "operation": {
                        "kind": "forward",
                        "chat_id": "100",
                        "source_chat_id": "200",
                        "message_ids": ["3", "4"],
                    },
                },
            )
        )["data"]
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        profile.read_chats.remove("200")
        reads.clear()
        settings.limits.daily_messages = 2
        clock[0] += timedelta(days=1)
        for _ in range(50):
            summaries = data(await mcp.call_tool("jobs_status", {"profile_id": "work"}))["data"][
                "jobs"
            ]
            if next(row for row in summaries if row["id"] == job)["status"] == "failed":
                break
            await asyncio.sleep(0.03)
        assert (
            data(await mcp.call_tool("jobs_status", {"profile_id": "work", "job_id": job}))[
                "error"
            ]["code"]
            == "read_not_allowed"
        )
        profile.read_chats.append("200")
        final = data(await mcp.call_tool("jobs_status", {"profile_id": "work", "job_id": job}))[
            "data"
        ]
        assert final["status"] == "failed" and final["error"]["code"] == "read_not_allowed"
        assert not SourceAPI.operations and not reads
