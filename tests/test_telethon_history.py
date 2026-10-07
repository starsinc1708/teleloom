from datetime import UTC, datetime, timedelta

import pytest
from telethon import TelegramClient, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.test_transport import client, running


@pytest.mark.asyncio
@pytest.mark.parametrize("include_upper_boundary", [False, True])
async def test_live_history_crosses_telegram_chunk_boundary_without_false_completion(
    tmp_path, monkeypatch, include_upper_boundary
):
    since = datetime(2025, 1, 1, tzinfo=UTC)
    until = since + timedelta(days=1)
    messages = [
        types.Message(
            id=id_,
            peer_id=types.PeerChannel(100),
            date=since + timedelta(minutes=id_ - 1000),
            message=f"Synthetic record {id_}",
        )
        for id_ in range(1001, 1267)
    ]
    if include_upper_boundary:
        messages.append(
            types.Message(
                id=1267, peer_id=types.PeerChannel(100), date=until, message="Excluded boundary"
            )
        )

    class TelegramResponses(TelegramClient):
        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return types.User(id=1, first_name="Test owner")

        async def get_input_entity(self, peer):
            return types.InputPeerChannel(100, 0)

        async def __call__(self, request, *args, **kwargs):
            assert isinstance(
                request, (functions.messages.GetHistoryRequest, functions.messages.SearchRequest)
            )
            offset_date = getattr(request, "offset_date", getattr(request, "max_date", None))
            rows = [
                row
                for row in reversed(messages)
                if (not request.offset_id or row.id < request.offset_id)
                and (not offset_date or row.date <= offset_date)
                and (
                    not isinstance(request, functions.messages.SearchRequest)
                    or request.q.casefold() in row.message.casefold()
                )
            ][: request.limit]
            if isinstance(request, functions.messages.SearchRequest):
                return types.messages.Messages(messages=rows, chats=[], users=[], topics=[])
            return types.messages.MessagesSlice(
                count=len(messages), messages=rows, chats=[], users=[], topics=[]
            )

    session = StringSession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(b"\x01" * 256)
    monkeypatch.setenv("TELELOOM_PERSONAL_SESSION", session.save())
    monkeypatch.setenv("TELELOOM_PERSONAL_API_HASH", "synthetic-api-hash")
    monkeypatch.setattr("teleloom.adapters.TelegramClient", TelegramResponses)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user", api_id=1)})
    from teleloom.adapters import make_adapter

    async with running(settings, make_adapter) as app, client(app, settings) as mcp:
        cursor = None
        collected = []
        for count, more in [(100, True), (100, True), (66, False)]:
            raw_result = await mcp.call_tool(
                "messages_get",
                {
                    "profile_id": "personal",
                    "chat_id": "-1000000000100",
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "limit": 100,
                    "cursor": cursor,
                },
            )
            assert raw_result.structuredContent is not None, raw_result.content
            result = data(raw_result)
            assert result["ok"], result.get("error")
            page = result["data"]
            assert len(page["items"]) == count
            assert page["incomplete"] is more
            assert bool(page["next_cursor"]) is more
            collected.extend(int(row["id"]) for row in page["items"])
            cursor = page["next_cursor"]
        assert collected == list(range(1266, 1000, -1))
        searched = data(
            await mcp.call_tool(
                "messages_search",
                {
                    "profile_id": "personal",
                    "chat_id": "-1000000000100",
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "query": "Synthetic record 1266",
                    "limit": 100,
                },
            )
        )
        assert searched["ok"]
        assert [row["id"] for row in searched["data"]["items"]] == ["1266"]
