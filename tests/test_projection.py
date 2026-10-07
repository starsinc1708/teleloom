import json
from datetime import UTC, datetime

import pytest

from teleloom.config import Profile, Settings
from tests.fakes import TelegramAPI, data
from tests.test_attachments_v02 import AttachmentAPI
from tests.test_attachments_v02 import complete as attachments_complete
from tests.test_jobs import complete
from tests.test_threads_v02 import ThreadAPI
from tests.test_transport import client, running


class RichTelegram(TelegramAPI):
    def __init__(self, *args):
        super().__init__(*args)
        for message in self.rows:
            message.date = datetime(2026, 10, 3, 12, int(message.id), tzinfo=UTC)
            message.link = f"https://t.me/example/{message.id}"
            message.sender_name = "Alice"
            message.reactions = [{"emoji": "👍", "count": 100}] * 100
            message.entities = [{"type": "bold", "offset": 0, "length": 8}] * 100
            message.reply_to_message_id = "1"
            message.views = 1000


@pytest.mark.asyncio
async def test_digest_projection_reduces_both_wire_formats_and_preserves_original_coverage(
    tmp_path,
):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {"profile_id": "personal", "chat_id": "100", "limit": 2}
    async with running(settings, RichTelegram) as app, client(app, settings) as session:
        original = await session.call_tool("digest_context", args)
        compact = await session.call_tool("digest_context", {**args, "preset": "digest"})
        full = data(original)["data"]
        shown = data(compact)["data"]
        assert "reactions" not in shown["items"][0]
        assert "entities" not in shown["items"][0]
        assert shown["items"][0]["text"] == "Decision 5"
        assert shown["items"][0]["sender_name"] == "Alice"
        for key in ("profile_id", "chat_id", "id", "date", "link", "reply_to_message_id"):
            assert shown["items"][0][key] == full["items"][0][key]
        for key in ("next_cursor", "source", "coverage", "incomplete", "warnings"):
            assert shown[key] == full[key]
        assert shown["incomplete"] is True
        assert compact.structuredContent == json.loads(compact.content[0].text)
        assert len(compact.content[0].text) < len(original.content[0].text) / 2
        restored = data(await session.call_tool("digest_context", args))["data"]
        assert restored == full


@pytest.mark.asyncio
async def test_explicit_fields_keep_identity_and_cursor_can_restore_full_following_pages(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {"profile_id": "personal", "chat_id": "100", "limit": 2}
    async with running(settings, RichTelegram) as app, client(app, settings) as session:
        first = data(await session.call_tool("messages_get", {**args, "fields": ["text"]}))["data"]
        assert [row["id"] for row in first["items"]] == ["5", "4"]
        assert "sender_name" not in first["items"][0]
        assert first["items"][0]["reply_to_message_id"] == "1"
        assert first["items"][0]["date"] == "2026-10-03T12:05:00Z"
        second = data(
            await session.call_tool(
                "messages_get", {**args, "cursor": first["next_cursor"], "preset": "full"}
            )
        )["data"]
        assert [row["id"] for row in second["items"]] == ["3", "2"]
        assert len(second["items"][0]["reactions"]) == 100
        assert "projection" not in second
        minimum = data(await session.call_tool("messages_get", {**args, "fields": []}))["data"]
        assert "text" not in minimum["items"][0]
        assert minimum["items"][0]["link"] == "https://t.me/example/5"
        assert minimum["coverage"]["returned"] == 2


@pytest.mark.asyncio
async def test_invalid_projection_fails_before_read_and_errors_keep_normal_envelope(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        for options in (
            {"fields": ["token"]},
            {"fields": ["text"], "preset": "digest"},
            {"preset": "invented"},
            {"fields": ["text"] * 65},
        ):
            response = await session.call_tool(
                "messages_get", {"profile_id": "unknown", "chat_id": "100", **options}
            )
            result = data(response)
            assert response.isError
            assert result["error"]["code"] == "invalid_projection"
            assert result["data"] == {}
            assert json.loads(response.content[0].text) == result
        valid = data(
            await session.call_tool(
                "messages_get", {"profile_id": "unknown", "chat_id": "100", "preset": "digest"}
            )
        )
        assert valid["error"]["code"] == "profile_not_found"


@pytest.mark.asyncio
async def test_inbox_nested_messages_and_chat_projection_keep_source_and_ack_warnings(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, RichTelegram) as app, client(app, settings) as session:
        shown = data(
            await session.call_tool("inbox_get", {"profile_id": "personal", "fields": ["text"]})
        )["data"]
        entry = shown["chats"][0]
        assert shown["source"] == "telegram_unread"
        assert "snapshot_at" in shown
        assert "ack_warning" in entry and "ack_through" in entry
        assert entry["chat"]["title"] == "Engineering"
        assert "unread_count" not in entry["chat"]
        assert "reactions" not in entry["messages"][0]
        resolved = data(
            await session.call_tool(
                "chat_resolve", {"profile_id": "personal", "target": "100", "preset": "minimal"}
            )
        )["data"]
        assert resolved["id"] == "100" and resolved["title"] == "Engineering"
        assert "read_inbox_max_id" not in resolved
        listed = data(
            await session.call_tool("chats_list", {"profile_id": "personal", "preset": "minimal"})
        )["data"]
        assert listed["items"][0]["id"] == "100"
        assert "unread_count" not in listed["items"][0]


@pytest.mark.asyncio
async def test_durable_evidence_projection_keeps_snapshot_partial_coverage_and_index_originals(
    tmp_path,
):
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", sync_chats=["100"])}
    )
    async with running(settings, RichTelegram) as app, client(app, settings) as session:
        sync = data(
            await session.call_tool(
                "sync_start",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "since": "2026-10-03T00:00:00Z",
                    "until": "2026-10-04T00:00:00Z",
                },
            )
        )["data"]
        await complete(session, "personal", sync["job_id"])
        local = data(
            await session.call_tool(
                "messages_search",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "query": "Decision",
                    "source": "index",
                    "preset": "digest",
                },
            )
        )["data"]
        assert local["source"] == "local_index" and local["incomplete"] is True
        assert local["warnings"] and "sync" in local["coverage"]
        assert "reactions" not in local["items"][0]
        indexed = data(
            await session.call_tool(
                "messages_get", {"profile_id": "personal", "chat_id": "100", "source": "index"}
            )
        )["data"]
        assert len(indexed["items"][0]["reactions"]) == 100
        job = data(
            await session.call_tool(
                "digest_context_many_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                    "since": "2026-10-03T00:00:00Z",
                    "until": "2026-10-04T00:00:00Z",
                    "max_messages": 2,
                },
            )
        )["data"]
        await attachments_complete(session, "personal", job["job_id"])
        projected = data(
            await session.call_tool(
                "jobs_results",
                {"profile_id": "personal", "job_id": job["job_id"], "limit": 1, "preset": "digest"},
            )
        )["data"]
        assert "reactions" not in projected["items"][0]
        assert projected["coverage"]["stopped_reason"] == "message_budget"
        assert projected["incomplete"] is True
        original = data(
            await session.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job["job_id"],
                    "cursor": projected["next_cursor"],
                    "preset": "full",
                },
            )
        )["data"]
        assert original["items"][0]["id"] == "4"
        assert len(original["items"][0]["reactions"]) == 100
        assert original["coverage"] == projected["coverage"]
        assert original["result_snapshot_at"] == projected["result_snapshot_at"]


class RichThreads(ThreadAPI):
    async def thread(self, *args, **kwargs):
        rows = await super().thread(*args, **kwargs)
        return [row.model_copy(update={"reactions": [{"emoji": "👍", "count": 4}]}) for row in rows]

    async def pinned(self, chat, *, before, limit):
        return await self.thread(chat, 10, before=before, limit=limit)


@pytest.mark.parametrize(
    "tool,extra",
    [
        ("topic_history", {"topic_id": "10"}),
        ("thread_get", {"root_message_id": "10"}),
        ("comments_get", {"message_id": "5"}),
        ("messages_pinned", {}),
    ],
)
@pytest.mark.asyncio
async def test_thread_projection_retains_discussion_and_structural_source_ids(
    tmp_path, tool, extra
):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, RichThreads) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                tool,
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "limit": 2,
                    "fields": ["text"],
                    **extra,
                },
            )
        )["data"]
        assert "reactions" not in result["items"][0]
        assert result["items"][0]["thread_root_id"] == "10"
        assert result["items"][0]["topic_id"] == "10"
        assert result["next_cursor"] and result["incomplete"] is True
        if tool == "comments_get":
            assert result["discussion"]["chat_id"] == "200"
            assert result["discussion"]["root_id"] == "10"
            assert result["discussion"]["original_status"] == "available"
        elif tool in {"thread_get", "topic_history"}:
            assert result["original_status"] == "deleted_or_unavailable"


@pytest.mark.asyncio
async def test_classification_projection_cannot_authorize_jev_and_cursor_keeps_profile_scope(
    tmp_path,
):
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")}
    )
    async with running(settings, RichTelegram) as app, client(app, settings) as session:
        classification = data(
            await session.call_tool(
                "messages_classify",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "message_ids": ["5"],
                    "preset": "digest",
                },
            )
        )["data"]
        assert classification["analysis_status"] == "disabled"
        assert classification["judgments"] is None
        assert "reactions" not in classification["evidence"]["items"][0]
        first = data(
            await session.call_tool(
                "messages_get",
                {"profile_id": "personal", "chat_id": "100", "limit": 1, "fields": ["text"]},
            )
        )["data"]
        cross = data(
            await session.call_tool(
                "messages_get",
                {
                    "profile_id": "work",
                    "chat_id": "100",
                    "cursor": first["next_cursor"],
                    "fields": ["text"],
                },
            )
        )
        assert cross["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_attachment_projection_retains_chunks_expiry_and_partial_errors(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, AttachmentAPI) as app, client(app, settings) as session:
        job = data(
            await session.call_tool(
                "attachments_read_start",
                {"profile_id": "personal", "chat_id": "100", "message_ids": ["1", "999"]},
            )
        )["data"]
        await attachments_complete(session, "personal", job["job_id"])
        args = {"profile_id": "personal", "job_id": job["job_id"]}
        full = data(await session.call_tool("jobs_results", args))["data"]
        shown = data(await session.call_tool("jobs_results", {**args, "preset": "minimal"}))["data"]
        assert "text" not in shown["items"][0]
        for key in (
            "profile_id",
            "chat_id",
            "message_id",
            "link",
            "method",
            "part_index",
            "part_count",
            "truncated",
            "untrusted",
            "expires_at",
        ):
            assert shown["items"][0][key] == full["items"][0][key]
        assert shown["errors"] == full["errors"]
        assert shown["errors"][0]["code"] == "message_not_found"
        assert shown["incomplete"] is True
        assert shown["coverage"] == full["coverage"]


@pytest.mark.asyncio
async def test_mcp_discovery_exposes_projection_only_on_supported_readers(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        for name in (
            "messages_get",
            "messages_search",
            "digest_context",
            "topic_history",
            "thread_get",
            "comments_get",
            "messages_pinned",
            "messages_classify",
            "inbox_get",
            "jobs_results",
            "chats_list",
            "folder_members",
            "chat_resolve",
        ):
            assert "fields" in tools[name].inputSchema["properties"]
            assert "preset" in tools[name].inputSchema["properties"]
        for name in (
            "delivery_preview",
            "delivery_execute",
            "inbox_ack",
            "jobs_control",
            "jobs_status",
        ):
            assert "fields" not in tools[name].inputSchema["properties"]
        assert tools["response_fields_select"].annotations.readOnlyHint is False
        assert tools["response_fields_select"].annotations.destructiveHint is False
