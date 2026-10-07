from datetime import UTC, datetime, timedelta

import pytest
from telethon import functions, types

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.telegram_fakes import SDK, factory
from tests.test_projection_cli import cli_owner as cli_owner
from tests.test_transport import client, running

NOW = datetime(2026, 10, 5, tzinfo=UTC)


class GlobalSDK(SDK):
    async def __call__(self, request, *args, **kwargs):
        self.calls.append(request)
        assert isinstance(request, functions.messages.SearchGlobalRequest)
        assert request.q == "decision" and request.limit <= 100
        if request.offset_id == 0:
            assert isinstance(request.offset_peer, types.InputPeerEmpty)
            assert request.max_date >= NOW and request.min_date < NOW - timedelta(days=1)
            return types.messages.MessagesSlice(
                count=3,
                next_rate=77,
                messages=[
                    types.Message(
                        id=30, peer_id=types.PeerChannel(200), date=NOW, message="Excluded"
                    ),
                    types.Message(
                        id=12,
                        peer_id=types.PeerChannel(200),
                        date=NOW - timedelta(hours=1),
                        message="First decision",
                    ),
                    types.Message(
                        id=12,
                        peer_id=types.PeerChannel(100),
                        date=NOW - timedelta(days=1),
                        message="Start decision",
                    ),
                ],
                chats=[
                    types.Channel(
                        id=200,
                        title="Other",
                        date=NOW,
                        photo=types.ChatPhotoEmpty(),
                        access_hash=123456789,
                    )
                ],
                users=[],
                topics=[],
            )
        assert request.offset_id == 12 and request.offset_rate == 77
        assert isinstance(request.offset_peer, types.InputPeerChannel)
        return types.messages.Messages(messages=[], chats=[], users=[], topics=[])


@pytest.mark.asyncio
async def test_global_search_exact_bounds_scoped_cursor_and_repeated_ids_across_chats(tmp_path):
    sdk = GlobalSDK()
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(kind="user"),
            "other": Profile(kind="user"),
            "helper": Profile(kind="bot"),
        },
    )
    args = {
        "profile_id": "personal",
        "query": "decision",
        "since": (NOW - timedelta(days=1)).isoformat(),
        "until": NOW.isoformat(),
        "limit": 1,
        "max_requests": 1,
    }
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
        assert "messages_search_global" in tools
        assert tools["messages_search_global"].annotations.readOnlyHint is True
        first = data(await mcp.call_tool("messages_search_global", args))
        assert first["ok"], first
        assert [(row["chat_id"], row["id"]) for row in first["data"]["items"]] == [
            ("-1000000000200", "12")
        ]
        assert first["data"]["coverage"]["requested_since"] == args["since"]
        assert first["data"]["next_cursor"] and first["data"]["incomplete"]
        assert "123456789" not in str(first)
        cursor = first["data"]["next_cursor"]
        for changed in ({"profile_id": "other"}, {"query": "different"}, {"kind": "group"}):
            rejected = data(
                await mcp.call_tool("messages_search_global", {**args, "cursor": cursor, **changed})
            )
            assert rejected["error"]["code"] == "invalid_cursor"
        second = data(await mcp.call_tool("messages_search_global", {**args, "cursor": cursor}))
        assert [(row["chat_id"], row["id"]) for row in second["data"]["items"]] == [
            ("-1000000000100", "12")
        ]
        assert len(sdk.calls) == 2
        assert not second["data"]["next_cursor"] and not second["data"]["incomplete"]
        again = data(await mcp.call_tool("messages_search_global", {**args, "cursor": cursor}))
        assert again == second and len(sdk.calls) == 2
        bot = data(await mcp.call_tool("messages_search_global", {**args, "profile_id": "helper"}))
        assert bot["error"]["code"] == "unsupported_capability"
        blank = data(await mcp.call_tool("messages_search_global", {**args, "query": " "}))
        assert blank["error"]["code"] == "invalid_query"
        settings.profiles["personal"].generation = "replacement"
        stale = data(await mcp.call_tool("messages_search_global", {**args, "cursor": cursor}))
        assert stale["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_public_chat_search_and_bounded_nearest_context_with_reply(tmp_path):
    class DiscoverySDK(SDK):
        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            assert isinstance(request, functions.contacts.SearchRequest)
            assert request.q == "science" and request.limit == 100
            return types.contacts.Found(
                my_results=[],
                results=[types.PeerChannel(100)],
                chats=[
                    types.Channel(
                        id=100,
                        title="Science",
                        date=NOW,
                        photo=types.ChatPhotoEmpty(),
                        username="science",
                        access_hash=123456789,
                    )
                ],
                users=[types.User(id=50, first_name="Ignored unrelated", phone="SECRET_PHONE")],
            )

        async def iter_messages(self, peer, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("ids"):
                rows = [row for row in self.rows if row.id in kwargs["ids"]]
            elif kwargs.get("reverse"):
                assert kwargs["min_id"] == 10 and kwargs["limit"] == 2
                rows = [row for row in reversed(self.rows) if row.id > kwargs["min_id"]]
            else:
                rows = [row for row in self.rows if row.id < kwargs["offset_id"]]
            for row in rows[: kwargs.get("limit") or len(rows)]:
                yield row

    sdk = DiscoverySDK()
    sdk.rows = [
        types.Message(
            id=id_,
            peer_id=types.PeerChannel(100),
            date=NOW,
            message=f"Evidence {id_}",
            reply_to=types.MessageReplyHeader(reply_to_msg_id=2) if id_ == 10 else None,
        )
        for id_ in (14, 13, 10, 7, 5, 2)
    ]
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        found = data(
            await mcp.call_tool(
                "chats_search",
                {"profile_id": "personal", "query": "science", "scope": "public", "limit": 2},
            )
        )
        assert found["ok"], found
        assert [(row["id"], row["title"]) for row in found["data"]["items"]] == [
            ("-1000000000100", "Science")
        ]
        assert "SECRET_PHONE" not in str(found) and "123456789" not in str(found)
        context = data(
            await mcp.call_tool(
                "context_get",
                {
                    "profile_id": "personal",
                    "chat_id": "-1000000000100",
                    "message_id": "10",
                    "context_size": 1,
                    "fields": ["text"],
                },
            )
        )
        assert context["ok"], context
        assert [(row["id"], row["is_target"]) for row in context["data"]["items"]] == [
            ("7", False),
            ("10", True),
            ("13", False),
        ]
        assert context["data"]["reply_context"][0]["id"] == "2"
        assert context["data"]["coverage"]["has_older"] and context["data"]["coverage"]["has_newer"]
        assert context["data"]["incomplete"] is False


@pytest.mark.asyncio
async def test_selected_global_scope_uses_only_exact_allowed_peer_rpcs_and_bounded_empty_pages(
    tmp_path,
):
    class SelectedProfile(Profile):
        read_mode: str = "selected"
        read_chats: list[str] = ["-1000000000100"]

    class ScopedSDK(SDK):
        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            assert isinstance(request, functions.messages.SearchRequest), (
                "Selected scope must never invoke raw global search"
            )
            assert request.peer.channel_id == 100 and request.limit == 100
            if request.offset_id == 0:
                return types.messages.MessagesSlice(
                    count=101,
                    messages=[
                        types.Message(
                            id=20, peer_id=types.PeerChannel(100), date=NOW, message="Excluded end"
                        )
                    ],
                    chats=[],
                    users=[],
                    topics=[],
                )
            if request.offset_id == 20:
                return types.messages.MessagesSlice(
                    count=101,
                    messages=[
                        types.Message(
                            id=10,
                            peer_id=types.PeerChannel(100),
                            date=NOW - timedelta(hours=1),
                            message="Allowed decision",
                        )
                    ],
                    chats=[],
                    users=[],
                    topics=[],
                )
            return types.messages.Messages(messages=[], chats=[], users=[], topics=[])

    sdk = ScopedSDK()
    settings = Settings(data_dir=tmp_path, profiles={"personal": SelectedProfile(kind="user")})
    args = {
        "profile_id": "personal",
        "query": "decision",
        "since": (NOW - timedelta(days=1)).isoformat(),
        "until": NOW.isoformat(),
        "limit": 1,
        "max_requests": 1,
    }
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        first = data(await mcp.call_tool("messages_search_global", args))["data"]
        assert first["items"] == [] and first["next_cursor"]
        assert first["coverage"]["requests"] == 1 and first["coverage"]["scope"] == "allowed_chats"
        second = data(
            await mcp.call_tool("messages_search_global", {**args, "cursor": first["next_cursor"]})
        )["data"]
        assert [row["id"] for row in second["items"]] == ["10"]
        settings.profiles["personal"].read_chats = []
        invalid = data(
            await mcp.call_tool("messages_search_global", {**args, "cursor": second["next_cursor"]})
        )
        assert invalid["error"]["code"] == "invalid_cursor" and len(sdk.calls) == 2
        empty = data(await mcp.call_tool("messages_search_global", args))
        assert empty["ok"] and empty["data"]["items"] == []
        assert empty["data"]["coverage"]["requests"] == 0


@pytest.mark.asyncio
async def test_global_cursor_survives_restart_retains_partial_floodwait_and_expires(
    tmp_path, monkeypatch
):
    from telethon import errors

    class FloodOnceSDK(GlobalSDK):
        flood = True

        async def __call__(self, request, *args, **kwargs):
            if request.offset_id and self.flood:
                self.calls.append(request)
                self.flood = False
                raise errors.FloodWaitError(request, capture=5)
            return await super().__call__(request, *args, **kwargs)

    moment = [NOW]
    monkeypatch.setattr("teleloom.runtime.utcnow", lambda: moment[0])
    sdk = FloodOnceSDK()
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {
        "profile_id": "personal",
        "query": "decision",
        "since": (NOW - timedelta(days=1)).isoformat(),
        "until": NOW.isoformat(),
        "limit": 1,
        "max_requests": 1,
    }
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        first = data(await mcp.call_tool("messages_search_global", args))["data"]
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        partial = data(
            await mcp.call_tool("messages_search_global", {**args, "cursor": first["next_cursor"]})
        )
        assert partial["ok"] and partial["data"]["items"][0]["chat_id"] == "-1000000000100"
        assert partial["data"]["coverage"]["partial_error"]["retry_after"] == 5
        assert partial["data"]["incomplete"] and partial["data"]["next_cursor"]
        repeated = data(
            await mcp.call_tool("messages_search_global", {**args, "cursor": first["next_cursor"]})
        )
        assert repeated == partial and len(sdk.calls) == 2
        complete = data(
            await mcp.call_tool(
                "messages_search_global", {**args, "cursor": partial["data"]["next_cursor"]}
            )
        )["data"]
        assert (
            complete["items"] == [] and not complete["next_cursor"] and not complete["incomplete"]
        )
        moment[0] += timedelta(minutes=16)
        expired = data(
            await mcp.call_tool("messages_search_global", {**args, "cursor": first["next_cursor"]})
        )
        assert expired["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_global_search_and_rich_projection_use_real_cli_and_subprocess_stdio(
    cli_owner, tmp_path, monkeypatch
):
    import json
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from tests.test_projection_cli import ProjectionTelegramAPI

    async def fake_external_search(self, **kwargs):
        self.rows[0].text = "Exact block words"
        self.rows[0].text_source = "reconstructed"
        self.rows[0].original_text = ""
        self.rows[0].rich_text = {
            "blocks": [
                {"_": "PageBlockParagraph", "text": {"_": "TextPlain", "text": "Exact block words"}}
            ]
        }
        return {"items": self.rows, "complete": True, "next_offset": None}

    monkeypatch.setattr(
        ProjectionTelegramAPI, "global_search_batch", fake_external_search, raising=False
    )
    args = {"profile_id": "personal", "query": "block"}
    code, stdout, stderr = await cli_owner(
        "call", "messages_search_global", "--args", json.dumps(args), "--field", "text"
    )
    assert code == 0, stderr
    parsed = json.loads(stdout)
    assert parsed["ok"] and parsed["data"]["items"][0]["text_source"] == "reconstructed"
    assert (
        parsed["data"]["items"][0]["original_text"] == ""
        and "rich_text" not in parsed["data"]["items"][0]
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teleloom", "mcp"],
        env={
            **os.environ,
            "TELELOOM_DATA_DIR": str(tmp_path),
            "TELELOOM_MCP_TOKEN": "isolated-projection-cli-token",
        },
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert {"messages_search_global", "context_get", "chats_search"} <= {
            tool.name for tool in (await session.list_tools()).tools
        }
        wire = await session.call_tool("messages_search_global", {**args, "fields": ["text"]})
        assert wire.structuredContent == json.loads(wire.content[0].text)
        assert wire.structuredContent["data"]["items"] == parsed["data"]["items"]
