import asyncio

import pytest

from teleloom.config import Profile, Settings
from teleloom.server import create_server
from tests.fakes import TelegramAPI, data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "expected", "missing", "incomplete"),
    [(100, ["5", "3", "2", "1"], ["99"], False), (2, ["5", "3"], ["2", "1", "99"], True)],
)
async def test_explicit_message_ids_are_ordered_before_response_budget(
    tmp_path, limit, expected, missing, incomplete
):
    class RequestedOrderAPI(TelegramAPI):
        async def history(self, chat, *, ids=None, **kwargs):
            rows = await super().history(chat, ids=ids, **kwargs)
            if ids:
                by_id = {int(row.id): row for row in rows}
                return [by_id[id_] for id_ in ids if id_ in by_id]
            return rows

    from tests.test_transport import client, running

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, RequestedOrderAPI) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                "messages_get",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "message_ids": ["2", "5", "1", "3", "99"],
                    "limit": limit,
                },
            )
        )
        assert result["ok"]
        page = result["data"]
        assert [row["id"] for row in page["items"]] == expected
        assert page["coverage"]["ordering"] == "message_id_descending"
        assert page["coverage"]["missing_message_ids"] == missing
        assert page["coverage"]["returned"] == len(expected)
        assert page["incomplete"] is incomplete
        assert page["next_cursor"] is None
        assert bool(page["warnings"]) is incomplete


@pytest.mark.asyncio
async def test_stalled_dialog_read_times_out_and_later_read_can_succeed(tmp_path):
    class StalledDialogsAPI(TelegramAPI):
        attempts = 0

        async def chats(self):
            self.attempts += 1
            if self.attempts == 1:
                await asyncio.Event().wait()
            return await super().chats()

    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user")},
        read_timeout_seconds=0.05,
    )
    from tests.test_transport import client, running

    async with running(settings, StalledDialogsAPI) as app, client(app, settings) as session:
        first = await asyncio.wait_for(
            session.call_tool("chats_list", {"profile_id": "personal", "limit": 10}), timeout=1
        )
        result = data(first)
        assert first.isError
        assert result["error"]["code"] == "read_timeout"
        assert result["error"]["retryable"]
        assert not data(await session.call_tool("server_status", {}))["error"]
        second = data(
            await session.call_tool("chats_list", {"profile_id": "personal", "limit": 10})
        )
        assert second["ok"]
        assert [row["id"] for row in second["data"]["items"]] == ["100"]


@pytest.mark.asyncio
async def test_interrupted_connection_keeps_profile_owned_until_cleanup_finishes(tmp_path):
    from tests.test_transport import client, running

    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    created = []

    class SlowDisconnectAPI(TelegramAPI):
        def __init__(self, *args):
            super().__init__(*args)
            created.append(self)

        async def start(self):
            if len(created) == 1:
                await asyncio.Event().wait()

        async def close(self):
            async def disconnect():
                await release_cleanup.wait()
                cleanup_finished.set()

            await asyncio.shield(asyncio.create_task(disconnect()))

    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user")},
        read_timeout_seconds=0.05,
    )
    async with running(settings, SlowDisconnectAPI) as app:
        try:
            async with client(app, settings) as session:
                first = data(
                    await asyncio.wait_for(
                        session.call_tool("chats_list", {"profile_id": "personal"}), timeout=7
                    )
                )
                assert first["error"]["code"] == "read_timeout"
                second = data(await session.call_tool("chats_list", {"profile_id": "personal"}))
                assert second["error"]["code"] == "profile_closing"
                assert second["error"]["retryable"]
                assert len(created) == 1
                release_cleanup.set()
                await asyncio.wait_for(cleanup_finished.wait(), timeout=1)
                final = data(await session.call_tool("chats_list", {"profile_id": "personal"}))
                assert final["ok"]
        finally:
            release_cleanup.set()


@pytest.mark.asyncio
async def test_connection_timeout_is_retryable_through_http_mcp(tmp_path):
    from tests.test_transport import client, running

    class ConnectionTimeoutAPI(TelegramAPI):
        async def start(self):
            raise TimeoutError

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ConnectionTimeoutAPI) as app, client(app, settings) as session:
        result = data(await session.call_tool("chats_list", {"profile_id": "personal"}))
        assert result["error"]["code"] == "connection_timeout"
        assert result["error"]["retryable"]


@pytest.mark.asyncio
async def test_history_pages_are_profile_bound_and_inbox_ack_is_explicit(tmp_path):
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")}
    )
    server = create_server(settings, adapter_factory=TelegramAPI)
    first = data(
        await server.call_tool(
            "messages_get", {"profile_id": "personal", "chat_id": "100", "limit": 2}
        )
    )
    assert [m["id"] for m in first["data"]["items"]] == ["5", "4"]
    second = data(
        await server.call_tool(
            "messages_get",
            {
                "profile_id": "personal",
                "chat_id": "100",
                "limit": 2,
                "cursor": first["data"]["next_cursor"],
            },
        )
    )
    assert [m["id"] for m in second["data"]["items"]] == ["3", "2"]
    invalid = data(
        await server.call_tool(
            "messages_get",
            {"profile_id": "work", "chat_id": "100", "cursor": first["data"]["next_cursor"]},
        )
    )
    assert invalid["error"]["code"] == "invalid_cursor"
    inbox = data(await server.call_tool("inbox_get", {"profile_id": "personal"}))
    assert inbox["data"]["source"] == "telegram_unread"
    assert [m["id"] for m in inbox["data"]["chats"][0]["messages"]] == ["5", "4", "3"]
    ack = data(
        await server.call_tool(
            "inbox_ack", {"profile_id": "personal", "chat_id": "100", "through_message_id": "4"}
        )
    )
    assert ack["error"]["code"] == "confirmation_required"


@pytest.mark.asyncio
async def test_dialog_pages_keep_snapshot_when_recency_order_changes(tmp_path):
    from teleloom.models import Chat
    from tests.test_transport import client, running

    class ChangingDialogsAPI(TelegramAPI):
        calls = 0

        async def chats(self):
            self.calls += 1
            ids = ["100", "200", "300"] if self.calls == 1 else ["300", "100", "200"]
            return [Chat(id=id_, title=id_) for id_ in ids]

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ChangingDialogsAPI) as app, client(app, settings) as session:
        first = data(await session.call_tool("chats_list", {"profile_id": "personal", "limit": 2}))[
            "data"
        ]
        fresh = data(await session.call_tool("chats_list", {"profile_id": "personal", "limit": 2}))[
            "data"
        ]
        assert fresh["items"][0]["id"] == "300"
        second = data(
            await session.call_tool(
                "chats_list", {"profile_id": "personal", "limit": 2, "cursor": first["next_cursor"]}
            )
        )["data"]
        assert [row["id"] for row in first["items"] + second["items"]] == ["100", "200", "300"]
