import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from teleloom.cli import app as cli
from teleloom.config import Profile, Settings
from teleloom.models import Chat
from tests.fakes import TelegramAPI, data
from tests.test_transport import client, running


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("exposure", ["read-only", "selected"])
async def test_restricted_owner_blocks_queued_and_resumed_external_delivery(
    tmp_path, monkeypatch, paused, exposure
):
    from teleloom.models import TeleloomError, utcnow
    from tests.test_jobs import complete

    now = utcnow()
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: now)
    calls = []

    class RateOnce(TelegramAPI):
        async def send(self, *args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise TeleloomError(
                    "rate_limited", "Request rejected before acceptance", retry_after=60
                )
            return "101"

    profile = Profile(kind="user", send_chats=["100"])
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, RateOnce) as app, client(app, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "delivery_preview",
                {
                    "profile_id": "personal",
                    "recipients": ["100"],
                    "text": "Approved earlier",
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
        for _ in range(30):
            state = data(
                await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
            )["data"]
            if state["next_run"] > 0:
                break
            await asyncio.sleep(0.03)
        assert state["next_run"] > 0 and len(calls) == 1
        if paused:
            assert data(
                await mcp.call_tool(
                    "jobs_control", {"profile_id": "personal", "job_id": job, "action": "pause"}
                )
            )["ok"]
    restricted = Settings(
        data_dir=tmp_path,
        profiles={"personal": profile},
        exposure_mode=exposure,
        exposed_tools=["jobs_status", "jobs_control"] if exposure == "selected" else [],
    )
    now += timedelta(seconds=61)
    async with running(restricted, RateOnce) as app, client(app, restricted) as mcp:
        for _ in range(30):
            state = data(
                await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
            )["data"]
            if state["status"] == "paused":
                break
            await asyncio.sleep(0.03)
        assert state["status"] == "paused" and len(calls) == 1
        resumed = data(
            await mcp.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )
        assert resumed["error"]["code"] == "tool_not_exposed"
        assert len(calls) == 1
    async with running(settings, RateOnce) as app, client(app, settings) as mcp:
        assert data(
            await mcp.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )["ok"]
        assert (await complete(mcp, "personal", job))["status"] == "completed"
        assert len(calls) == 2


class PrivateTelegram(TelegramAPI):
    reads = []

    async def chats(self):
        return [*(await super().chats()), Chat(id="200", title="Private secret")]

    async def folders(self):
        return [
            {
                "id": "2",
                "title": "Work",
                "included_chat_ids": ["100", "200"],
                "pinned_chat_ids": ["200"],
                "excluded_chat_ids": [],
            }
        ]

    async def history(self, chat, **kwargs):
        self.reads.append(chat)
        return await super().history(chat, **kwargs)

    async def discussion(self, chat, post):
        return {"chat_id": "200", "root_id": "1"}


@pytest.mark.asyncio
async def test_selected_reads_cover_live_index_jobs_media_and_discovery(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user",
                read_mode="selected",
                read_chats=["100"],
                sync_chats=["200"],
                send_chats=["200"],
            )
        },
    )
    PrivateTelegram.reads = []
    async with running(settings, PrivateTelegram) as app:
        async with client(app, settings) as session:
            chats = data(await session.call_tool("chats_list", {"profile_id": "work"}))
            assert [c["id"] for c in chats["data"]["items"]] == ["100"]
            folders = data(await session.call_tool("folders_list", {"profile_id": "work"}))
            assert "200" not in json.dumps(folders)
            members = data(
                await session.call_tool("folder_members", {"profile_id": "work", "folder_id": "2"})
            )
            assert "200" not in json.dumps(members)
            inbox = data(await session.call_tool("inbox_get", {"profile_id": "work"}))
            assert "Private secret" not in json.dumps(inbox)
            comments = data(
                await session.call_tool(
                    "comments_get", {"profile_id": "work", "chat_id": "100", "message_id": "1"}
                )
            )
            assert comments["error"]["code"] == "read_not_allowed"
            now = datetime.now(UTC)
            for tool, args in [
                ("messages_get", {}),
                ("messages_get", {"source": "index"}),
                ("messages_search", {"query": "secret"}),
                ("topics_list", {}),
                ("thread_get", {"root_message_id": "1"}),
                ("attachments_read_start", {"message_ids": ["1"]}),
                ("sync_start", {}),
                ("export_start", {}),
            ]:
                result = data(
                    await session.call_tool(tool, {"profile_id": "work", "chat_id": "200", **args})
                )
                assert result["error"]["code"] == "read_not_allowed", (tool, result)
            result = data(
                await session.call_tool(
                    "digest_context_many_start",
                    {
                        "profile_id": "work",
                        "chat_ids": ["200"],
                        "since": (now - timedelta(days=1)).isoformat(),
                        "until": now.isoformat(),
                    },
                )
            )
            assert result["error"]["code"] == "read_not_allowed"
            denied = data(
                await session.call_tool("chat_resolve", {"profile_id": "work", "target": "200"})
            )
            assert denied["error"]["code"] == "read_not_allowed"
            preview = data(
                await session.call_tool(
                    "delivery_preview",
                    {"profile_id": "work", "recipients": ["200"], "text": "secret"},
                )
            )
            assert preview["error"]["code"] == "read_not_allowed"
            assert "200" not in PrivateTelegram.reads
            assert (data(await session.call_tool("jobs_status", {"profile_id": "work"})))["data"][
                "jobs"
            ] == []


@pytest.mark.asyncio
async def test_revoked_policy_rejects_cached_cursor_and_evidence_without_deleting_it(tmp_path):
    profile = Profile(kind="user", read_mode="all")
    settings = Settings(data_dir=tmp_path, profiles={"work": profile})
    async with running(settings, PrivateTelegram) as app:
        async with client(app, settings) as session:
            first = data(await session.call_tool("chats_list", {"profile_id": "work", "limit": 1}))[
                "data"
            ]
            now = datetime.now(UTC)
            job = data(
                await session.call_tool(
                    "digest_context_many_start",
                    {
                        "profile_id": "work",
                        "chat_ids": ["100"],
                        "since": (now - timedelta(days=1)).isoformat(),
                        "until": now.isoformat(),
                    },
                )
            )["data"]
            profile.read_mode = "selected"
            profile.read_chats = []
            cursor = data(
                await session.call_tool(
                    "chats_list", {"profile_id": "work", "cursor": first["next_cursor"]}
                )
            )
            assert cursor["error"]["code"] == "invalid_cursor"
            for tool in ["jobs_results", "jobs_status"]:
                result = data(
                    await session.call_tool(tool, {"profile_id": "work", "job_id": job["job_id"]})
                )
                assert result["error"]["code"] == "read_not_allowed"
            profile.read_mode = "all"
            assert data(
                await session.call_tool(
                    "jobs_status", {"profile_id": "work", "job_id": job["job_id"]}
                )
            )["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,tools", [("read-only", []), ("selected", ["server_status"])])
async def test_exposure_discovery_matches_actual_dispatch(tmp_path, mode, tools):
    settings = Settings(data_dir=tmp_path, exposure_mode=mode, exposed_tools=tools)
    async with running(settings) as app:
        async with client(app, settings) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert "delivery_execute" not in names
            assert (
                await session.call_tool(
                    "delivery_execute",
                    {"profile_id": "work", "plan_id": "x", "plan_hash": "x", "confirmed": True},
                )
            ).isError
            if mode == "selected":
                assert names == {"server_status"}


def test_cli_read_policy_and_exposure_are_explicit_and_legacy_compatible(tmp_path, monkeypatch):
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    Settings(data_dir=tmp_path, profiles={"work": Profile(kind="user")}).save()
    runner = CliRunner()
    assert (
        runner.invoke(cli, ["profile", "read-policy", "work", "--mode", "selected"]).exit_code == 0
    )
    assert runner.invoke(cli, ["profile", "allow", "work", "100", "--scope", "read"]).exit_code == 0
    assert (
        runner.invoke(
            cli, ["config", "exposure", "--mode", "selected", "--tool", "messages_get"]
        ).exit_code
        == 0
    )
    stored = Settings.load(tmp_path)
    assert stored.profile("work").read_mode == "selected"
    assert stored.profile("work").read_chats == ["100"]
    assert stored.profile("work").send_chats == []
    assert stored.exposure_mode == "selected"
    assert stored.exposed_tools == ["messages_get"]
    assert Profile(kind="user").read_mode == "all"
