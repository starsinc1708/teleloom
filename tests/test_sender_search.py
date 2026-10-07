"""T12: exact sender search through MCP, real SQLite and fake SDK."""

from datetime import timedelta

import pytest
from telethon import types

from teleloom.config import Profile, Settings
from teleloom.models import Message, TeleloomError
from teleloom.store import Store
from tests.fakes import data
from tests.telegram_fakes import CHAT, NOW, SDK, factory
from tests.test_transport import client, running


class SearchSDK(SDK):
    async def __call__(self, request, *args, **kwargs):
        self.calls.append(request)
        rows = [
            row
            for row in self.rows
            if (not request.offset_id or row.id < request.offset_id)
            and (not getattr(request, "q", "") or request.q in row.message)
        ][: request.limit]
        return types.messages.Messages(messages=rows, users=[], chats=[], topics=[])


def record(id_, sender=None, text="decision"):
    return types.Message(
        id=id_,
        peer_id=types.PeerChannel(100),
        date=NOW + timedelta(seconds=id_),
        from_id=types.PeerUser(sender) if sender else None,
        message=text,
        post_author="Same name",
    )


@pytest.mark.asyncio
async def test_quiet_sender_crosses_newest_chunk_and_cursor_keeps_filtered_lookahead(tmp_path):
    sdk = SearchSDK()
    sdk.rows = [record(i, 8) for i in range(266, 0, -1)]
    sdk.rows[0] = record(266)
    sdk.rows[165] = record(101, 7)
    sdk.rows[265] = record(1, 7)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", read_mode="selected", read_chats=[CHAT])},
    )
    args = {
        "profile_id": "personal",
        "chat_id": CHAT,
        "sender_id": "7",
        "query": "decision",
        "since": NOW.isoformat(),
        "until": (NOW + timedelta(seconds=267)).isoformat(),
        "limit": 1,
        "max_requests": 1,
    }
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        first = data(await mcp.call_tool("messages_search", args))
        assert first["ok"], first
        assert first["data"]["items"] == []
        assert first["data"]["coverage"]["unknown_sender"] == 1
        assert first["data"]["coverage"]["scan_complete"] is False
        assert first["data"]["next_cursor"]
        denied = data(await mcp.call_tool("messages_search", {**args, "chat_id": "7"}))
        assert denied["error"]["code"] == "read_not_allowed"
        cursor = first["data"]["next_cursor"]
        mismatch = data(
            await mcp.call_tool("messages_search", {**args, "cursor": cursor, "sender_id": "8"})
        )
        assert mismatch["error"]["code"] == "invalid_cursor"
        settings.profiles["personal"].read_chats = []
        revoked = data(await mcp.call_tool("messages_search", {**args, "cursor": cursor}))
        assert revoked["error"]["code"] == "read_not_allowed"
        settings.profiles["personal"].read_chats = [CHAT]
        generation = settings.profiles["personal"].generation
        settings.profiles["personal"].generation = "replacement"
        replaced = data(await mcp.call_tool("messages_search", {**args, "cursor": cursor}))
        assert replaced["error"]["code"] == "invalid_cursor"
        settings.profiles["personal"].generation = generation
        collected = []
        while cursor:
            page = data(await mcp.call_tool("messages_search", {**args, "cursor": cursor}))
            assert page["ok"], page
            collected.extend(row["id"] for row in page["data"]["items"])
            repeated = data(await mcp.call_tool("messages_search", {**args, "cursor": cursor}))
            assert repeated == page
            cursor = page["data"]["next_cursor"]
        assert collected == ["101", "1"]
        assert page["data"]["coverage"]["scan_complete"] is True
        assert all(str(request.peer.channel_id) == "100" for request in sdk.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,backend,source",
    [("user", "bot_api", "index"), ("bot", "bot_api", "live"), ("bot", "mtproto", "live")],
)
async def test_local_and_bot_sender_search_preserve_scope_dates_ids_and_unknowns(
    tmp_path, kind, backend, source
):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind=kind, bot_backend=backend, read_mode="selected", read_chats=[CHAT]
            )
        },
    )
    store = Store(tmp_path)
    store.save_messages(
        [
            Message(
                profile_id="personal",
                chat_id=chat,
                id=str(i),
                date=NOW + timedelta(seconds=i),
                text="decision",
                sender_id=None if i == 9 else "7" if i in {1, 4, 10, 11} else "8",
                sender_name="Same name",
                author_signature="Same name",
            )
            for chat in [CHAT, "-1000000000200"]
            for i in range(1, 204)
        ]
    )
    store.close()

    def no_connection(*args):
        raise AssertionError("Saved/index sender search must not connect to Telegram")

    args = {
        "profile_id": "personal",
        "chat_id": CHAT,
        "sender_id": "7",
        "source": source,
        "since": (NOW + timedelta(seconds=1)).isoformat(),
        "until": (NOW + timedelta(seconds=11)).isoformat(),
        "limit": 1,
    }
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        page = data(await mcp.call_tool("messages_search", args))
        assert page["ok"], page
        assert page["data"]["coverage"]["unknown_sender"] == 1
        assert page["data"]["source"] == ("bot_updates" if kind == "bot" else "local_index")
        collected = list(page["data"]["items"])
        while page["data"]["next_cursor"]:
            page = data(
                await mcp.call_tool(
                    "messages_search", {**args, "cursor": page["data"]["next_cursor"]}
                )
            )
            assert page["ok"], page
            collected.extend(page["data"]["items"])
        assert [row["id"] for row in collected] == ["10", "4", "1"]
        assert all(row["chat_id"] == CHAT and row["sender_id"] == "7" for row in collected)
        assert page["data"]["incomplete"]  # saved source cannot prove absence in Telegram
        for sender in ["Same name", "007", "0", "@ada"]:
            invalid = data(await mcp.call_tool("messages_search", {**args, "sender_id": sender}))
            assert invalid["error"]["code"] == "invalid_id"
        unsupported = data(
            await mcp.call_tool(
                "messages_search_global",
                {"profile_id": "personal", "sender_id": "7", "query": "decision"},
            )
        )
        assert unsupported["error"]["code"] == "unsupported_capability"


@pytest.mark.asyncio
async def test_sender_partial_failure_preserves_hits_and_resumable_scan(tmp_path):
    class FailingSDK(SearchSDK):
        async def __call__(self, request, *args, **kwargs):
            if request.offset_id == 101:
                raise TeleloomError("rate_limited", "Fake wait", retry_after=2)
            return await super().__call__(request, *args, **kwargs)

    sdk = FailingSDK()
    sdk.rows = [record(i, 7 if i == 200 else 8) for i in range(200, 0, -1)]
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {"profile_id": "personal", "chat_id": CHAT, "sender_id": "7", "limit": 2}
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        partial = data(await mcp.call_tool("messages_search", args))
        assert partial["ok"], partial
        assert [row["id"] for row in partial["data"]["items"]] == ["200"]
        assert partial["data"]["coverage"]["partial_error"]["retry_after"] == 2
        cursor = partial["data"]["next_cursor"]
        assert cursor
        sdk.rows.clear()
        # Remove the external failure, then resume exactly from the retained offset.
        sdk.__class__ = SearchSDK
        resumed = data(await mcp.call_tool("messages_search", {**args, "cursor": cursor}))
        assert resumed["ok"], resumed
        assert resumed["data"]["items"] == []
        assert resumed["data"]["coverage"]["scan_complete"] is True
        assert resumed["data"]["next_cursor"] is None
