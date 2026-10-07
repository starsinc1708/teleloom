import asyncio

import pytest

from teleloom.config import Limits, Profile, Settings
from teleloom.models import TeleloomError
from teleloom.server import create_server
from tests.fakes import TelegramAPI, data
from tests.test_transport import client, running


async def launch(server, recipients=None, broadcast=False):
    preview = data(
        await server.call_tool(
            "delivery_preview",
            {
                "profile_id": "personal",
                "recipients": recipients or ["100"],
                "text": "Confirmed",
                "broadcast": broadcast,
            },
        )
    )["data"]
    return data(
        await server.call_tool(
            "delivery_execute",
            {
                "profile_id": "personal",
                "plan_id": preview["plan_id"],
                "plan_hash": preview["plan_hash"],
                "confirmed": True,
            },
        )
    )["data"]["job_id"]


@pytest.mark.asyncio
async def test_uncertain_send_cannot_be_resumed_or_replayed_after_restart(tmp_path):
    calls = []

    def external_api(profile, config, store, credentials):
        api = TelegramAPI(profile, config, store, credentials)
        api.send_error = ConnectionError("SENSITIVE_NETWORK_DETAILS")
        calls.append(api)
        return api

    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", send_chats=["100"])}
    )
    async with running(settings, external_api) as app:
        async with client(app, settings) as session:
            job = await launch(session)
            for _ in range(40):
                status = data(
                    await session.call_tool(
                        "jobs_status", {"profile_id": "personal", "job_id": job}
                    )
                )["data"]
                if status["status"] == "needs_review":
                    break
                await asyncio.sleep(0.03)
            assert status["deliveries"][0]["status"] == "unknown"
            assert "SENSITIVE_NETWORK_DETAILS" not in str(status)
            for action in ["resume", "pause"]:
                rejected = data(
                    await session.call_tool(
                        "jobs_control", {"profile_id": "personal", "job_id": job, "action": action}
                    )
                )
                assert rejected["error"]["code"] == "delivery_unknown"
    async with running(settings, external_api) as app:
        async with client(app, settings) as session:
            status = data(
                await session.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
            )["data"]
            assert status["status"] == "needs_review"
    assert sum(len(api.sent) for api in calls) == 1


@pytest.mark.asyncio
async def test_replacement_rejects_old_plan_and_pending_delivery_and_isolates_index(tmp_path):
    from tests.test_jobs import complete

    old = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="user", identity={"id": "42"}, send_chats=["100"], sync_chats=["100"]
            )
        },
    )
    async with running(old) as app, client(app, old) as session:
        old_page = data(
            await session.call_tool(
                "messages_get", {"profile_id": "personal", "chat_id": "100", "limit": 2}
            )
        )["data"]
        sync = data(
            await session.call_tool("sync_start", {"profile_id": "personal", "chat_id": "100"})
        )["data"]
        await complete(session, "personal", sync["job_id"])
        preview = data(
            await session.call_tool(
                "delivery_preview",
                {"profile_id": "personal", "recipients": ["100"], "text": "Old account"},
            )
        )["data"]
        job = await launch(session)
        await session.call_tool(
            "jobs_control", {"profile_id": "personal", "job_id": job, "action": "pause"}
        )
    new = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="user", identity={"id": "99"}, send_chats=["100"], sync_chats=["100"]
            )
        },
    )
    async with running(new) as app, client(app, new) as session:
        old_cursor = data(
            await session.call_tool(
                "messages_get",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "limit": 2,
                    "cursor": old_page["next_cursor"],
                },
            )
        )
        assert old_cursor["error"]["code"] == "invalid_cursor"
        rejected = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": preview["plan_id"],
                    "plan_hash": preview["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert rejected["error"]["code"] == "account_changed"
        resumed = data(
            await session.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )
        assert resumed["error"]["code"] == "account_changed"
        history = data(
            await session.call_tool(
                "messages_get", {"profile_id": "personal", "chat_id": "100", "source": "index"}
            )
        )["data"]
        assert not history["items"]


@pytest.mark.asyncio
async def test_broadcast_pause_cancel_and_separate_allowlist(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(kind="user", send_chats=["100"], broadcast_chats=["100", "200"])
        },
    )
    server = create_server(settings, adapter_factory=TelegramAPI)
    denied = data(
        await server.call_tool(
            "delivery_preview", {"profile_id": "personal", "recipients": ["200"], "text": "x"}
        )
    )
    assert denied["error"]["code"] == "recipient_not_allowed"
    job = await launch(server, ["100", "200"], broadcast=True)
    paused = data(
        await server.call_tool(
            "jobs_control", {"profile_id": "personal", "job_id": job, "action": "pause"}
        )
    )
    assert paused["data"]["status"] == "paused"
    status = data(await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job}))[
        "data"
    ]
    assert all(item["status"] == "pending" for item in status["deliveries"])
    await server.call_tool(
        "jobs_control", {"profile_id": "personal", "job_id": job, "action": "cancel"}
    )
    status = data(await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job}))[
        "data"
    ]
    assert status["status"] == "cancelled"
    assert all(item["status"] == "cancelled" for item in status["deliveries"])


@pytest.mark.asyncio
async def test_rate_limit_waits_and_daily_budget_covers_uncertain_attempts(tmp_path, monkeypatch):
    apis = []

    def external_api(*args):
        api = TelegramAPI(*args)
        api.send_error = TeleloomError("rate_limited", "Wait", retry_after=10)
        apis.append(api)
        return api

    settings = Settings(
        data_dir=tmp_path,
        limits=Limits(daily_messages=1),
        profiles={"personal": Profile(kind="user", send_chats=["100"])},
    )
    from datetime import timedelta

    from teleloom.models import utcnow

    clock = [utcnow()]
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock[0])
    async with running(settings, external_api) as app, client(app, settings) as server:
        job = await launch(server)
        for _ in range(40):
            first = data(
                await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
            )["data"]
            if first["next_run"] > 0:
                break
            await asyncio.sleep(0.03)
        assert first["deliveries"][0]["status"] == "pending"
        assert first["next_run"] == clock[0].timestamp() + 10
        clock[0] += timedelta(seconds=11)
        await asyncio.sleep(0.3)
        second = data(
            await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
        )["data"]
        assert second["next_run"] > clock[0].timestamp()
        assert len(apis[0].sent) == 1
