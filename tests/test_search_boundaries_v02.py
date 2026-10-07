from datetime import timedelta

import pytest
from telethon import functions, types

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.telegram_fakes import CHAT, NOW, SDK, factory
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, "boundary", "activity"])
async def test_reading_jobs_keep_inclusive_start_and_fractional_exclusive_end(tmp_path, query):
    start = NOW - timedelta(days=1)
    end = NOW + timedelta(microseconds=500000)

    class StrictDates(SDK):
        async def __call__(self, request, *args, **kwargs):
            if isinstance(
                request, (functions.messages.SearchRequest, functions.messages.GetHistoryRequest)
            ):
                minimum = getattr(request, "min_date", None)
                maximum = getattr(request, "max_date", None) or getattr(
                    request, "offset_date", None
                )
                rows = [
                    types.Message(
                        id=id_, peer_id=types.PeerChannel(100), date=date, message="boundary"
                    )
                    for id_, date in [(100, start), (200, NOW)]
                ]
                rows = [
                    row
                    for row in rows
                    if (not minimum or row.date.timestamp() > int(minimum.timestamp()))
                    and (not maximum or row.date.timestamp() < int(maximum.timestamp()))
                    and (not request.offset_id or row.id < request.offset_id)
                ]
                return types.messages.MessagesSlice(
                    count=2,
                    messages=sorted(rows, key=lambda r: r.id, reverse=True),
                    chats=[],
                    users=[],
                    topics=[],
                )
            return await super().__call__(request, *args, **kwargs)

    settings = Settings(data_dir=tmp_path, profiles={"person": Profile(kind="user")})
    async with running(settings, factory(StrictDates())) as app, client(app, settings) as session:
        arguments = {
            "profile_id": "person",
            "chat_ids": [CHAT],
            "since": start.isoformat(),
            "until": end.isoformat(),
        }
        tool = "digest_context_many_start"
        if query:
            tool = "messages_search_many_start"
            arguments["query"] = query
        if query == "activity":
            from unittest.mock import patch

            tool = "activity_start"
            arguments = {"profile_id": "person", "chat_ids": [CHAT]}
            with patch("teleloom.jobs.utcnow", return_value=end):
                started = data(await session.call_tool(tool, arguments))
        else:
            started = data(await session.call_tool(tool, arguments))
        assert started["ok"]
        await complete(session, "person", started["data"]["job_id"])
        result = data(
            await session.call_tool(
                "jobs_results", {"profile_id": "person", "job_id": started["data"]["job_id"]}
            )
        )
        assert [m["id"] for m in result["data"]["items"]] == (
            ["200"] if query == "activity" else ["200", "100"]
        )
        assert result["data"]["incomplete"] is False
