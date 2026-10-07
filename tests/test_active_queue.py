import asyncio
import json

import pytest

from teleloom.config import Profile, Settings
from tests.fakes import TelegramAPI, data
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.mark.asyncio
async def test_terminal_originals_do_not_get_decoded_by_active_queue(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, TelegramAPI) as app, client(app, settings) as server:
        first = data(
            await server.call_tool(
                "activity_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                },
            )
        )["data"]["job_id"]
        await complete(server, "personal", first)
        decoded_archives = []
        original_loads = json.loads

        def observe(value, *args, **kwargs):
            result = original_loads(value, *args, **kwargs)
            if isinstance(result, dict) and result.get("id") == first and "payload" in result:
                decoded_archives.append(first)
            return result

        monkeypatch.setattr(json, "loads", observe)
        second = data(
            await server.call_tool(
                "activity_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                },
            )
        )["data"]["job_id"]
        await complete(server, "personal", second)
        await asyncio.sleep(0.3)  # Include an idle cleanup/tick with terminal history.
        listing = data(
            await server.call_tool(
                "jobs_status",
                {
                    "profile_id": "personal",
                },
            )
        )["data"]["jobs"]
        assert [row["id"] for row in listing] == [first, second]
        assert [row["status"] for row in listing] == ["completed", "completed"]
        assert decoded_archives == []
        monkeypatch.setattr(json, "loads", original_loads)
        retained = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": first,
                },
            )
        )["data"]
        assert retained["items"][0]["chat_id"] == "100"
