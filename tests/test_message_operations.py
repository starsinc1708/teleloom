import asyncio
from datetime import timedelta

import pytest

from teleloom.config import Profile, Settings
from tests.fakes import TelegramAPI, data
from tests.test_jobs import complete
from tests.test_transport import client, running


class MessageAPI(TelegramAPI):
    operations = []
    states = {}

    async def mutate_message(self, operation, random_id):
        self.operations.append(operation)
        return {"accepted": True, "message_ids": ["101"]}

    async def message_state(self, chat, kind, message_id, limit, query, bot_id):
        return self.states.get(kind, {"items": []})


@pytest.mark.asyncio
async def test_skipped_forward_receipts_are_partial_and_cannot_be_resumed(tmp_path):
    class PartialForward(MessageAPI):
        async def mutate_message(self, operation, random_id):
            return {"accepted": True, "complete": False, "message_ids": ["101"]}

    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", send_chats=["100"])}
    )
    async with running(settings, PartialForward) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
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
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        finished = await complete(mcp, "personal", job)
        assert finished["status"] == "failed"
        assert finished["deliveries"][0]["status"] == "partial"
        assert finished["deliveries"][0]["receipt"]["message_ids"] == ["101"]
        resumed = data(
            await mcp.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )
        assert not resumed["ok"]


@pytest.mark.asyncio
async def test_formatted_reply_is_immutable_confirmed_and_journaled_through_http(tmp_path):
    MessageAPI.operations = []
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", send_chats=["100"])}
    )
    async with running(settings, MessageAPI) as app, client(app, settings) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
        assert "message_operation_preview" in tools
        preview = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send",
                        "chat_id": "100",
                        "text": "<b>Approved</b>",
                        "format": "html",
                        "reply_to_message_id": "4",
                    },
                },
            )
        )["data"]
        assert preview["preview"]["operation"]["format"] == "html"
        assert preview["source_messages"][0]["text"] == "Decision 4"
        execute = {
            "profile_id": "personal",
            "plan_id": preview["plan_id"],
            "plan_hash": preview["plan_hash"],
        }
        assert (
            data(await mcp.call_tool("delivery_execute", execute))["error"]["code"]
            == "confirmation_required"
        )
        assert (
            data(
                await mcp.call_tool(
                    "delivery_execute", {**execute, "confirmed": True, "plan_hash": "changed"}
                )
            )["error"]["code"]
            == "plan_changed"
        )
        first = data(await mcp.call_tool("delivery_execute", {**execute, "confirmed": True}))[
            "data"
        ]
        duplicate = data(await mcp.call_tool("delivery_execute", {**execute, "confirmed": True}))[
            "data"
        ]
        assert first["job_id"] == duplicate["job_id"]
        finished = await complete(mcp, "personal", first["job_id"])
        assert finished["status"] == "completed"
        assert finished["deliveries"][0]["receipt"] == {"accepted": True, "message_ids": ["101"]}
        assert len(MessageAPI.operations) == 1


@pytest.mark.asyncio
async def test_legacy_send_permission_never_grants_edit_and_ack_requires_its_exact_plan(tmp_path):
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", send_chats=["100"])}
    )
    async with running(settings, MessageAPI) as app, client(app, settings) as mcp:
        denied = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "edit",
                        "chat_id": "100",
                        "message_id": "4",
                        "text": "Changed",
                    },
                },
            )
        )
        assert denied["error"]["code"] == "mutation_not_allowed"
        legacy = data(
            await mcp.call_tool(
                "inbox_ack", {"profile_id": "personal", "chat_id": "100", "through_message_id": "4"}
            )
        )
        assert legacy["error"]["code"] == "confirmation_required"
        settings.profile("personal").mutation_chats = ["100"]
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "read_ack", "chat_id": "100", "through_message_id": "4"},
                },
            )
        )["data"]
        args = {
            "profile_id": "personal",
            "chat_id": "100",
            "through_message_id": "5",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        assert data(await mcp.call_tool("inbox_ack", args))["error"]["code"] == "plan_changed"
        result = data(await mcp.call_tool("inbox_ack", {**args, "through_message_id": "4"}))["data"]
        assert (await complete(mcp, "personal", result["job_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_edit_detects_source_change_and_permission_revocation_before_external_mutation(
    tmp_path,
):
    apis = []

    class HeldAPI(MessageAPI):
        def __init__(self, *args):
            super().__init__(*args)
            apis.append(self)

    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", mutation_chats=["100"])}
    )
    async with running(settings, HeldAPI) as app, client(app, settings) as mcp:
        operation = {"kind": "edit", "chat_id": "100", "message_id": "4", "text": "Confirmed"}
        plan = data(
            await mcp.call_tool(
                "message_operation_preview", {"profile_id": "personal", "operation": operation}
            )
        )["data"]
        apis[0].rows[3].text = "Changed after review"
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
        failed = await complete(mcp, "personal", job)
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "source_changed"
        plan2 = data(
            await mcp.call_tool(
                "message_operation_preview", {"profile_id": "personal", "operation": operation}
            )
        )["data"]
        settings.profile("personal").mutation_chats.clear()
        revoked = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan2["plan_id"],
                    "plan_hash": plan2["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert revoked["error"]["code"] == "mutation_not_allowed"
        assert not apis[0].sent


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["disconnect", "timeout"])
async def test_mutation_lost_acceptance_stays_unknown_across_restart_without_replay(
    tmp_path, fault
):
    accepted = []

    class LostAPI(MessageAPI):
        async def mutate_message(self, operation, random_id):
            accepted.append(operation)
            if fault == "timeout":
                await asyncio.Event().wait()
            raise ConnectionError("PRIVATE_RESPONSE")

    settings = Settings(
        data_dir=tmp_path,
        read_timeout_seconds=0.05,
        profiles={"personal": Profile(kind="user", mutation_chats=["100"])},
    )
    async with running(settings, LostAPI) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "reaction",
                        "chat_id": "100",
                        "message_id": "4",
                        "reactions": ["👍"],
                    },
                },
            )
        )["data"]
        args = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        job = data(await mcp.call_tool("delivery_execute", args))["data"]["job_id"]
        state = await complete(mcp, "personal", job)
        assert state["status"] == "needs_review"
        assert state["deliveries"][0]["status"] == "unknown"
        assert "PRIVATE_RESPONSE" not in str(state)
        assert (
            data(
                await mcp.call_tool(
                    "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
                )
            )["error"]["code"]
            == "delivery_unknown"
        )
    async with running(settings, LostAPI) as app, client(app, settings) as mcp:
        assert data(await mcp.call_tool("delivery_execute", args))["data"]["job_id"] == job
        await asyncio.sleep(0.3)
        assert (
            data(await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job}))[
                "data"
            ]["status"]
            == "needs_review"
        )
    assert len(accepted) == 1


@pytest.mark.asyncio
async def test_expired_operation_and_account_replacement_do_not_mutate(tmp_path, monkeypatch):
    from teleloom.models import utcnow

    clock = [utcnow()]
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock[0])
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(kind="user", mutation_chats=["100"]),
            "other": Profile(kind="user"),
        },
    )
    async with running(settings, MessageAPI) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "delete", "chat_id": "100", "message_ids": ["3", "4"]},
                },
            )
        )["data"]
        args = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        assert (
            data(await mcp.call_tool("delivery_execute", {**args, "profile_id": "other"}))["error"][
                "code"
            ]
            == "plan_not_found"
        )
        clock[0] += timedelta(minutes=15)
        assert (
            data(await mcp.call_tool("delivery_execute", args))["error"]["code"] == "plan_expired"
        )
        settings.profiles["personal"] = Profile(kind="user", mutation_chats=["100"])
        assert (
            data(await mcp.call_tool("delivery_execute", args))["error"]["code"]
            == "account_changed"
        )


@pytest.mark.asyncio
async def test_single_album_forward_freezes_all_siblings_and_can_select_exact_photo(tmp_path):
    class AlbumAPI(MessageAPI):
        async def start(self):
            self.rows[2].grouped_id = self.rows[3].grouped_id = "album-1"

    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", send_chats=["200"])}
    )
    async with running(settings, AlbumAPI) as app, client(app, settings) as mcp:
        operation = {
            "kind": "forward",
            "chat_id": "200",
            "source_chat_id": "100",
            "message_ids": ["4"],
        }
        expanded = data(
            await mcp.call_tool(
                "message_operation_preview", {"profile_id": "personal", "operation": operation}
            )
        )["data"]
        assert expanded["preview"]["operation"]["message_ids"] == ["3", "4"]
        assert [row["text"] for row in expanded["source_messages"]] == ["Decision 3", "Decision 4"]
        exact = data(
            await mcp.call_tool(
                "message_operation_preview",
                {"profile_id": "personal", "operation": {**operation, "expand_album": False}},
            )
        )["data"]
        assert exact["preview"]["operation"]["message_ids"] == ["4"]


@pytest.mark.asyncio
async def test_full_history_delete_continues_acknowledged_batches_with_durable_receipts(
    tmp_path, monkeypatch
):
    from teleloom.models import utcnow

    clock = [utcnow()]
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock[0])
    accepted = []

    class BatchedAPI(MessageAPI):
        async def mutate_message(self, operation, random_id):
            accepted.append(operation)
            clock[0] += timedelta(seconds=6)
            return {
                "accepted": True,
                "complete": len(accepted) == 2,
                **({"continue": True, "remaining_offset": 100} if len(accepted) == 1 else {}),
            }

    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", mutation_chats=["100"])}
    )
    async with running(settings, BatchedAPI) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "delete_history",
                        "chat_id": "100",
                        "through_message_id": "4",
                        "max_requests": 2,
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
        state = await complete(mcp, "personal", job)
        assert state["status"] == "completed"
        assert state["deliveries"][0]["receipts"][0]["remaining_offset"] == 100
        assert state["deliveries"][0]["receipt"]["complete"] is True
        assert len(accepted) == 2


@pytest.mark.asyncio
async def test_owner_shutdown_during_accepted_mutation_preserves_unknown_without_replay(tmp_path):
    accepted = asyncio.Event()
    calls = []

    class InterruptedAPI(MessageAPI):
        async def mutate_message(self, operation, random_id):
            calls.append(operation)
            accepted.set()
            await asyncio.Event().wait()

    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", mutation_chats=["100"])}
    )
    async with running(settings, InterruptedAPI) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "pin", "chat_id": "100", "message_id": "4"},
                },
            )
        )["data"]
        args = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        job = data(await mcp.call_tool("delivery_execute", args))["data"]["job_id"]
        await asyncio.wait_for(accepted.wait(), timeout=2)
    async with running(settings, InterruptedAPI) as app, client(app, settings) as mcp:
        state = data(await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job}))[
            "data"
        ]
        assert state["status"] == "needs_review"
        assert state["deliveries"][0]["status"] == "unknown"
        assert data(await mcp.call_tool("delivery_execute", args))["data"]["job_id"] == job
        await asyncio.sleep(0.3)
    assert len(calls) == 1
