import asyncio
from datetime import timedelta

import pytest

from teleloom.config import Profile, Settings
from teleloom.models import Message, TeleloomError
from tests.fakes import data
from tests.test_jobs import complete
from tests.test_reading_jobs_v02 import NOW, ReadingAPI, wait_status
from tests.test_transport import client, running


@pytest.mark.asyncio
async def test_total_deadline_survives_floodwait_restart_with_originals(tmp_path, monkeypatch):
    clock = NOW
    calls = []
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)

    class SlowAPI(ReadingAPI):
        async def history(self, chat, **kwargs):
            nonlocal clock
            calls.append(kwargs["before"])
            clock += timedelta(seconds=3)
            if kwargs["before"] is not None:
                raise TeleloomError("flood_wait", "Wait", retry_after=60)
            return [
                Message(
                    profile_id=self.profile,
                    chat_id=chat,
                    id=str(i),
                    date=NOW - timedelta(minutes=1),
                    text=f"Original {i}",
                )
                for i in range(101, 201)
            ]

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, SlowAPI) as app, client(app, settings) as server:
        started = data(
            await server.call_tool(
                "digest_context_many_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                    "since": (NOW - timedelta(days=1)).isoformat(),
                    "until": NOW.isoformat(),
                    "max_duration_seconds": 10,
                },
            )
        )["data"]
        job = started["job_id"]
        await wait_status(server, job, lambda row: row["error"] is not None)
    clock += timedelta(seconds=12)
    async with running(settings, SlowAPI) as app, client(app, settings) as server:
        finished = await complete(server, "personal", job)
        assert finished["status"] == "completed"
        assert finished["payload"]["deadline_at"] == (NOW + timedelta(seconds=10)).isoformat()
        page = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "limit": 100,
                },
            )
        )["data"]
        assert len(page["items"]) == 100
        assert page["items"][0]["text"] == "Original 200"
        assert page["coverage"]["stopped_reason"] == "total_deadline"
        assert page["coverage"]["budget_stop"] is True
        assert page["coverage"]["requests"] == page["coverage"]["logical_requests"] == 2
        assert page["coverage"]["observed_rpc_requests"] is None
        assert page["coverage"]["elapsed_seconds"] == 18
        assert page["incomplete"] is True
        assert page["coverage"]["chats"][0]["complete"] is False
        rejected = data(
            await server.call_tool(
                "jobs_control",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "action": "resume",
                },
            )
        )
        assert rejected["error"]["code"] == "job_terminal"
        assert calls == [None, 101]


@pytest.mark.asyncio
async def test_deadline_cancels_pending_read_without_retries(tmp_path):
    cancelled = asyncio.Event()
    calls = []

    class HangingAPI(ReadingAPI):
        async def history(self, chat, **kwargs):
            calls.append(chat)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, HangingAPI) as app, client(app, settings) as server:
        job = data(
            await server.call_tool(
                "activity_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                    "max_duration_seconds": 1,
                },
            )
        )["data"]["job_id"]
        finished = await complete(server, "personal", job)
        assert cancelled.is_set()
        assert calls == ["100"]
        assert finished["result"]["coverage"]["stopped_reason"] == "total_deadline"
        assert finished["result"]["coverage"]["logical_requests"] == 1


@pytest.mark.asyncio
async def test_reconnect_consumes_existing_deadline_before_history(tmp_path, monkeypatch):
    clock = NOW
    connections = []
    reads = []
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)

    class ReconnectingAPI(ReadingAPI):
        async def start(self):
            nonlocal clock
            connections.append(self.profile)
            if len(connections) == 2:
                clock += timedelta(seconds=10)

        async def history(self, chat, **kwargs):
            reads.append(chat)
            raise TeleloomError("connection_error", "Retry", retry_after=1)

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ReconnectingAPI) as app, client(app, settings) as server:
        job = data(
            await server.call_tool(
                "activity_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                    "max_duration_seconds": 10,
                },
            )
        )["data"]["job_id"]
        await wait_status(server, job, lambda row: row["error"] is not None)
    clock += timedelta(seconds=1)
    async with running(settings, ReconnectingAPI) as app, client(app, settings) as server:
        finished = await complete(server, "personal", job)
        assert reads == ["100"]
        assert connections == ["personal", "personal"]
        assert finished["result"]["coverage"]["elapsed_seconds"] == 11
        assert finished["result"]["coverage"]["stopped_reason"] == "total_deadline"
        assert finished["result"]["coverage"]["logical_requests"] == 2


@pytest.mark.asyncio
async def test_local_processing_stops_at_atomic_original_checkpoint(tmp_path, monkeypatch):
    clock = NOW
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)

    class SlowMessage(Message):
        def model_dump(self, **kwargs):
            nonlocal clock
            result = super().model_dump(**kwargs)
            clock += timedelta(seconds=2)
            return result

    class LocalAPI(ReadingAPI):
        async def history(self, chat, **kwargs):
            return [
                SlowMessage(
                    profile_id=self.profile,
                    chat_id=chat,
                    id=str(i),
                    date=NOW - timedelta(minutes=1),
                    text=f"Original {i}",
                )
                for i in (2, 1)
            ]

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, LocalAPI) as app, client(app, settings) as server:
        job = data(
            await server.call_tool(
                "messages_search_many_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                    "query": "Original",
                    "since": (NOW - timedelta(days=1)).isoformat(),
                    "until": NOW.isoformat(),
                    "max_duration_seconds": 1,
                },
            )
        )["data"]["job_id"]
        finished = await complete(server, "personal", job)
        assert finished["progress"] == 1
        assert finished["result"]["coverage"]["chats"][0]["returned"] == 1
    async with running(settings, LocalAPI) as app, client(app, settings) as server:
        page = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                },
            )
        )["data"]
        assert [row["text"] for row in page["items"]] == ["Original 2"]
        assert page["coverage"]["stopped_reason"] == "total_deadline"
        assert page["coverage"]["chats"][0]["complete"] is False
