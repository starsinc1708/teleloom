import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import functions, types

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.telegram_fakes import CHAT, SDK, factory
from tests.test_jobs import complete
from tests.test_projection_cli import cli_owner as cli_owner
from tests.test_transport import client, running


@pytest.mark.asyncio
async def test_unread_export_freezes_boundaries_and_resumes_without_duplicates_or_ack(
    tmp_path, monkeypatch
):
    now = datetime(2026, 10, 5, 12, tzinfo=UTC)
    monkeypatch.setattr("teleloom.runtime.utcnow", lambda: now)
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: now)
    blocked = asyncio.Event()
    release = asyncio.Event()

    class UnreadSDK(SDK):
        async def __call__(self, request, *args, **kwargs):
            assert isinstance(request, functions.messages.GetHistoryRequest)
            self.calls.append(request)
            if request.offset_id == 102:
                blocked.set()
                await release.wait()
            rows = [row for row in self.rows if not request.offset_id or row.id < request.offset_id]
            return types.messages.MessagesSlice(
                count=len(self.rows), messages=rows[: request.limit], chats=[], users=[], topics=[]
            )

    sdk = UnreadSDK()
    sdk.rows = [
        types.Message(
            id=id_,
            peer_id=types.PeerChannel(100),
            date=now - timedelta(minutes=1),
            message=f"Unread {id_}",
            out=id_ == 150,
        )
        for id_ in range(201, 0, -1)
    ]
    sdk.dialogs = [
        SimpleNamespace(
            id=int(CHAT),
            name="Selected",
            entity=types.Channel(
                id=100, title="Selected", photo=types.ChatPhotoEmpty(), date=now, broadcast=True
            ),
            is_group=False,
            is_channel=True,
            unread_count=199,
            dialog=SimpleNamespace(read_inbox_max_id=2, top_message=201, notify_settings=None),
        )
    ]
    profile = Profile(kind="user", read_mode="selected", read_chats=[CHAT])
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        started = data(
            await mcp.call_tool(
                "unread_export_start",
                {
                    "profile_id": "personal",
                    "chat_ids": [CHAT],
                    "max_messages": 500,
                },
            )
        )
        assert started["ok"], started
        job = started["data"]["job_id"]
        await asyncio.wait_for(blocked.wait(), 3)
        paused = data(
            await mcp.call_tool(
                "jobs_control",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "action": "pause",
                },
            )
        )
        assert paused["ok"]
        release.set()
        await asyncio.sleep(0.05)
        status = data(await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job}))
        assert status["data"]["status"] == "paused"
    sdk.rows.insert(
        0, types.Message(id=202, peer_id=types.PeerChannel(100), date=now, message="Later arrival")
    )
    sdk.dialogs[0].dialog.read_inbox_max_id = 201
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        assert data(
            await mcp.call_tool(
                "jobs_control",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "action": "resume",
                },
            )
        )["ok"]
        state = await complete(mcp, "personal", job)
        assert state["status"] == "completed", state
        rows = [
            json.loads(line)
            for line in Path(state["result"]["path"]).read_text(encoding="utf-8").splitlines()
        ]
        assert [int(row["id"]) for row in rows] == [id_ for id_ in range(201, 2, -1) if id_ != 150]
        assert state["result"]["coverage"]["chats"][0]["complete"] is True
        assert state["result"]["incomplete"] is False
        page = data(
            await mcp.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 100}
            )
        )
        assert page["ok"] and page["data"]["next_cursor"]
        profile.read_chats = []
        denied = data(await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job}))
        assert denied["error"]["code"] == "read_not_allowed"
        assert all(
            isinstance(request, functions.messages.GetHistoryRequest) for request in sdk.calls
        )


@pytest.mark.asyncio
async def test_unread_export_reports_unusable_unread_boundaries_without_claiming_empty(tmp_path):
    sdk = SDK()
    sdk.dialogs = [
        SimpleNamespace(
            id=int(CHAT),
            name="Selected",
            entity=types.Channel(
                id=100,
                title="Selected",
                photo=types.ChatPhotoEmpty(),
                date=datetime.now(UTC),
                broadcast=True,
            ),
            is_group=False,
            is_channel=True,
            unread_count=3,
            dialog=SimpleNamespace(read_inbox_max_id=0, top_message=0, notify_settings=None),
        )
    ]
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        started = data(
            await mcp.call_tool(
                "unread_export_start", {"profile_id": "personal", "chat_ids": [CHAT]}
            )
        )
        state = await complete(mcp, "personal", started["data"]["job_id"])
        assert state["result"]["unavailable"][0]["error"]["code"] == "unread_state_unavailable"
        assert state["result"]["incomplete"] and not sdk.calls


@pytest.mark.asyncio
async def test_unread_export_cli_and_subprocess_stdio_share_frozen_evidence(
    cli_owner, tmp_path, monkeypatch
):
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from teleloom.models import Chat
    from tests.test_projection_cli import ProjectionTelegramAPI

    async def dialogs(self):
        return [
            Chat(
                id="100",
                title="Selected",
                unread_count=1,
                read_inbox_max_id="4",
                top_message_id="5",
                muted=False,
                archived=False,
            )
        ]

    monkeypatch.setattr(ProjectionTelegramAPI, "chats", dialogs)
    code, stdout, stderr = await cli_owner(
        "call",
        "unread_export_start",
        "--args",
        json.dumps({"profile_id": "personal", "chat_ids": ["100"]}),
    )
    assert code == 0, stderr
    started = json.loads(stdout)
    assert started["ok"], started
    job = started["data"]["job_id"]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teleloom", "mcp"],
        env={
            **os.environ,
            "TELELOOM_DATA_DIR": str(tmp_path),
            "TELELOOM_MCP_TOKEN": "isolated-projection-cli-token",
        },
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as mcp:
        await mcp.initialize()
        state = await complete(mcp, "personal", job)
        result = data(
            await mcp.call_tool("jobs_results", {"profile_id": "personal", "job_id": job})
        )
        assert result["data"]["items"][0]["id"] == "5"
        text = Path(state["result"]["path"]).read_text(encoding="utf-8")
        assert "Ship the agreed fix today." in text
        assert state["result"]["incomplete"] is False
    code, stdout, stderr = await cli_owner(
        "call", "jobs_status", "--args", json.dumps({"profile_id": "personal", "job_id": job})
    )
    assert code == 0, stderr
    assert json.loads(stdout)["data"]["result"]["path"] == state["result"]["path"]


@pytest.mark.asyncio
async def test_bot_unread_export_freezes_pending_originals_before_worker_and_ack(
    tmp_path, monkeypatch
):
    from aiogram import Bot
    from aiogram.methods import GetUpdates
    from aiogram.types import Chat, Message, Update, User

    from teleloom.adapters import make_adapter
    from tests.test_bots import BotAPI

    blocked, release, edit, edited = (asyncio.Event() for _ in range(4))

    class BlockingSDK(SDK):
        async def __call__(self, request, *args, **kwargs):
            assert isinstance(request, functions.messages.GetHistoryRequest)
            blocked.set()
            await release.wait()
            return types.messages.Messages(messages=[], topics=[], chats=[], users=[])

    class EditingAPI(BotAPI):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetUpdates) and self.polls == 1:
                self.requests.append(method)
                self.polls += 1
                await edit.wait()
                edited.set()
                common = {
                    "date": datetime.now(UTC),
                    "chat": Chat(id=100, type="private", first_name="Owner"),
                    "from_user": User(id=42, is_bot=False, first_name="Owner"),
                }
                return [
                    Update(
                        update_id=10,
                        edited_message=Message(
                            message_id=4,
                            text="Changed original",
                            edit_date=int(common["date"].timestamp()),
                            **common,
                        ),
                    ),
                    Update(
                        update_id=11, message=Message(message_id=5, text="Later arrival", **common)
                    ),
                ]
            return await super().make_request(bot, method, timeout)

    api, sdk = EditingAPI(), BlockingSDK()
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr("teleloom.adapters.Bot", lambda token, session: Bot(token, session=api))

    def adapters(profile_id, profile, store, credentials):
        return (
            make_adapter(profile_id, profile, store, credentials)
            if profile.kind == "bot"
            else factory(sdk)(profile_id, profile, store, credentials)
        )

    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user"), "helper": Profile(kind="bot", polling=True)},
    )
    async with running(settings, adapters) as app, client(app, settings) as mcp:
        holding = data(
            await mcp.call_tool("activity_start", {"profile_id": "personal", "chat_ids": [CHAT]})
        )["data"]["job_id"]
        await asyncio.wait_for(blocked.wait(), 3)
        inbox = data(await mcp.call_tool("inbox_get", {"profile_id": "helper"}))["data"]
        assert inbox["chats"][0]["messages"][0]["text"] == "Observed message"
        started = data(
            await mcp.call_tool(
                "unread_export_start", {"profile_id": "helper", "chat_ids": ["100"]}
            )
        )
        assert started["ok"], started
        job = started["data"]["job_id"]
        assert data(
            await mcp.call_tool(
                "inbox_ack",
                {
                    "profile_id": "helper",
                    "chat_id": "100",
                    "through_message_id": "4",
                    "snapshot_id": inbox["chats"][0]["snapshot_id"],
                },
            )
        )["ok"]
        edit.set()
        await asyncio.wait_for(edited.wait(), 3)
        await mcp.call_tool(
            "jobs_control", {"profile_id": "personal", "job_id": holding, "action": "cancel"}
        )
        release.set()
        state = await complete(mcp, "helper", job)
        rows = [
            json.loads(line)
            for line in Path(state["result"]["path"]).read_text(encoding="utf-8").splitlines()
        ]
        assert [(row["id"], row["text"]) for row in rows] == [("4", "Observed message")]
        assert state["result"]["source"] == "bot_updates" and state["result"]["incomplete"]
        for _ in range(50):
            current = data(await mcp.call_tool("inbox_get", {"profile_id": "helper"}))["data"]
            if current["chats"]:
                break
            await asyncio.sleep(0.02)
        assert current["chats"], json.dumps(data(await mcp.call_tool("profiles_list", {})))
        assert {row["text"] for row in current["chats"][0]["messages"]} == {
            "Changed original",
            "Later arrival",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("limit, reason", [(1, "message_budget"), (100, "export_byte_budget")])
async def test_unread_export_reports_missing_chats_and_exact_message_or_byte_budget(
    tmp_path, limit, reason
):
    now = datetime.now(UTC) - timedelta(minutes=1)

    class BudgetSDK(SDK):
        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            assert isinstance(request, functions.messages.GetHistoryRequest)
            return types.messages.Messages(messages=self.rows, topics=[], chats=[], users=[])

    sdk = BudgetSDK()
    sdk.rows = [
        types.Message(id=i, peer_id=types.PeerChannel(100), date=now, message="Original")
        for i in (3, 2, 1)
    ]
    sdk.dialogs = [
        SimpleNamespace(
            id=int(CHAT),
            name="Selected",
            entity=types.Channel(
                id=100, title="Selected", photo=types.ChatPhotoEmpty(), date=now, broadcast=True
            ),
            is_group=False,
            is_channel=True,
            unread_count=3,
            dialog=SimpleNamespace(read_inbox_max_id=0, top_message=3, notify_settings=None),
        )
    ]
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        started = data(
            await mcp.call_tool(
                "unread_export_start",
                {
                    "profile_id": "personal",
                    "chat_ids": [CHAT, "999"],
                    "format": "markdown",
                    "max_messages": limit,
                    "max_bytes": 1 if reason == "export_byte_budget" else 100000,
                },
            )
        )
        assert started["ok"], started
        state = await complete(mcp, "personal", started["data"]["job_id"])
        result = state["result"]
        assert result["coverage"].get("stopped_reason") == reason, result
        assert result["incomplete"] and result["unavailable"][0]["chat_id"] == "999"
        assert result["exported_messages"] == (1 if reason == "message_budget" else 0)
        text = Path(result["path"]).read_text(encoding="utf-8")
        assert text.count("Original") == result["exported_messages"]
        settings.profiles["personal"].read_mode = "selected"
        settings.profiles["personal"].read_chats = [CHAT, "999"]
        assert (
            data(
                await mcp.call_tool(
                    "jobs_status", {"profile_id": "personal", "job_id": state["id"]}
                )
            )["error"]["code"]
            == "read_policy_changed"
        )
