import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.test_transport import client, running


async def complete(server, profile, job):
    for _ in range(20):
        await asyncio.sleep(0.1)
        result = data(
            await server.call_tool("jobs_status", {"profile_id": profile, "job_id": job})
        )["data"]
        if result["status"] in {"completed", "failed", "needs_review"}:
            return result
    raise AssertionError("Job did not finish")


@pytest.mark.asyncio
async def test_expired_preview_is_rejected_without_creating_delivery_job(tmp_path, monkeypatch):
    now = datetime(2025, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: now)
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", send_chats=["100"])}
    )
    async with running(settings) as app, client(app, settings) as server:
        preview = data(
            await server.call_tool(
                "delivery_preview",
                {"profile_id": "personal", "recipients": ["100"], "text": "Expiry check"},
            )
        )["data"]
        now += timedelta(minutes=15)
        rejected = data(
            await server.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": preview["plan_id"],
                    "plan_hash": preview["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert rejected["error"]["code"] == "plan_expired"
        assert (
            data(await server.call_tool("jobs_status", {"profile_id": "personal"}))["data"]["jobs"]
            == []
        )


@pytest.mark.asyncio
async def test_selected_sync_is_resumable_searchable_and_exports_without_duplicates(tmp_path):
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", sync_chats=["100"])}
    )
    async with running(settings) as app, client(app, settings) as server:
        started = data(
            await server.call_tool("sync_start", {"profile_id": "personal", "chat_id": "100"})
        )["data"]
        assert (await complete(server, "personal", started["job_id"]))["status"] == "completed"
        again = data(
            await server.call_tool("sync_start", {"profile_id": "personal", "chat_id": "100"})
        )["data"]
        await complete(server, "personal", again["job_id"])
        found = data(
            await server.call_tool(
                "messages_search",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "query": "Decision",
                    "source": "index",
                },
            )
        )
        assert [row["id"] for row in found["data"]["items"]] == ["5", "4", "3", "2", "1"]
        export = data(
            await server.call_tool("export_start", {"profile_id": "personal", "chat_id": "100"})
        )["data"]
        finished = await complete(server, "personal", export["job_id"])
        from pathlib import Path

        rows = [
            json.loads(line)
            for line in Path(finished["result"]["path"]).read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 5
        denied = data(
            await server.call_tool("sync_start", {"profile_id": "personal", "chat_id": "200"})
        )
        assert denied["error"]["code"] == "sync_not_allowed"


@pytest.mark.asyncio
async def test_delivery_is_hash_bound_confirmed_idempotent_and_profile_isolated(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(kind="user", send_chats=["100"]),
            "work": Profile(kind="user"),
        },
    )
    async with running(settings) as app, client(app, settings) as server:
        args = {"profile_id": "personal", "recipients": ["100"], "text": "Approved text"}
        preview = data(await server.call_tool("delivery_preview", args))["data"]
        execute = {
            "profile_id": "personal",
            "plan_id": preview["plan_id"],
            "plan_hash": preview["plan_hash"],
        }
        denied = data(await server.call_tool("delivery_execute", execute))
        assert denied["error"]["code"] == "confirmation_required"
        changed = data(
            await server.call_tool(
                "delivery_execute", {**execute, "plan_hash": "wrong", "confirmed": True}
            )
        )
        assert changed["error"]["code"] == "plan_changed"
        foreign = data(
            await server.call_tool(
                "delivery_execute", {**execute, "profile_id": "work", "confirmed": True}
            )
        )
        assert foreign["error"]["code"] == "plan_not_found"
        first = data(await server.call_tool("delivery_execute", {**execute, "confirmed": True}))[
            "data"
        ]
        second = data(await server.call_tool("delivery_execute", {**execute, "confirmed": True}))[
            "data"
        ]
        assert first["job_id"] == second["job_id"]
        finished = await complete(server, "personal", first["job_id"])
        assert finished["deliveries"][0]["status"] == "sent"
        assert len(finished["deliveries"]) == 1


@pytest.mark.asyncio
async def test_multi_page_sync_resumes_after_daemon_restart_without_duplicates(tmp_path):
    from datetime import timedelta

    from teleloom.models import utcnow
    from tests.fakes import TelegramAPI

    class HistoryAPI(TelegramAPI):
        def __init__(self, *args):
            super().__init__(*args)
            template = self.rows[0]
            now = utcnow()
            self.rows = [
                template.model_copy(
                    update={
                        "id": str(i),
                        "text": f"Archive {i}",
                        "date": now - timedelta(seconds=500 - i),
                    }
                )
                for i in range(1, 206)
            ]

    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", sync_chats=["100"])}
    )
    async with running(settings, HistoryAPI) as app, client(app, settings) as session:
        job = data(
            await session.call_tool("sync_start", {"profile_id": "personal", "chat_id": "100"})
        )["data"]["job_id"]
        for _ in range(40):
            status = data(
                await session.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
            )["data"]
            if status["progress"] >= 100:
                break
            await asyncio.sleep(0.03)
        assert status["progress"] == 100
        await session.call_tool(
            "jobs_control", {"profile_id": "personal", "job_id": job, "action": "pause"}
        )
    async with running(settings, HistoryAPI) as app, client(app, settings) as session:
        await session.call_tool(
            "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
        )
        assert (await complete(session, "personal", job))["progress"] == 205
        ids = []
        cursor = None
        while True:
            page = data(
                await session.call_tool(
                    "messages_get",
                    {
                        "profile_id": "personal",
                        "chat_id": "100",
                        "source": "index",
                        "limit": 100,
                        "cursor": cursor,
                    },
                )
            )["data"]
            ids.extend(row["id"] for row in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len(ids) == len(set(ids)) == 205
        assert ids == [str(i) for i in range(205, 0, -1)]
