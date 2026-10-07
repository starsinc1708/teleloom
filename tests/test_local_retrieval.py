"""T13: selected local FTS retrieval; real SQLite and public MCP workflows."""

from datetime import timedelta

import pytest

from teleloom.config import Profile, Settings
from teleloom.models import Message
from teleloom.store import Store
from tests.fakes import data
from tests.telegram_fakes import NOW
from tests.test_transport import client, running


def no_connection(*args):
    raise AssertionError("Local retrieval must not connect, sync, scan Telegram or upload AI")


def seed(directory):
    store = Store(directory)
    store.save_messages(
        [
            Message(
                profile_id="personal",
                chat_id=chat,
                id=id_,
                date=NOW,
                text=text,
                sender_id="7",
                link=f"https://t.me/c/123/{id_}",
            )
            for chat, id_, text in [
                ("100", "10", "alpha beta"),
                ("100", "2", "alpha beta"),
                ("200", "10", "alpha beta"),
                ("100", "11", "alpha"),
                ("200", "11", "beta"),
                ("300", "10", "private alpha beta"),
            ]
        ]
    )
    store.close()


@pytest.mark.asyncio
async def test_local_fts_literal_and_stable_ties_and_frozen_original_lookup(tmp_path):
    seed(tmp_path)
    settings = Settings(
        data_dir=tmp_path,
        exposure_mode="read-only",
        profiles={
            "personal": Profile(kind="user", read_mode="selected", read_chats=["100", "200", "400"])
        },
    )
    args = {
        "profile_id": "personal",
        "chat_ids": ["200", "100", "400"],
        "query": "alpha beta",
        "limit": 1,
        "snippet_characters": 32,
    }
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
        assert "messages_search_local" in tools
        assert tools["messages_search_local"].annotations.readOnlyHint is False
        first = data(await mcp.call_tool("messages_search_local", args))
        assert first["ok"], first
        page = first["data"]
        assert page["source"] == "local_index"
        assert page["incomplete"]
        assert page["coverage"]["missing_chats"] == ["400"]
        hit = page["items"][0]
        assert (hit["chat_id"], hit["id"]) == ("100", "10")
        assert hit["snippet"] == "alpha beta" and "text" not in hit
        assert hit["snippet_is_excerpt"] is True
        assert hit["source_message_version"]
        reference, version = page["evidence_ref"], page["source_version"]
        assert reference != page["next_cursor"]
        store = Store(tmp_path)
        store.save_messages(
            [
                Message(
                    profile_id="personal",
                    chat_id="100",
                    id="10",
                    date=NOW,
                    text="edited away",
                    edited_at=NOW + timedelta(hours=1),
                )
            ]
        )
        store.delete_messages("personal", "200", ["10"])
        store.close()
        collected = [(hit["chat_id"], hit["id"])]
        while page["next_cursor"]:
            page = data(
                await mcp.call_tool(
                    "messages_search_local", {**args, "cursor": page["next_cursor"]}
                )
            )["data"]
            assert page["evidence_ref"] == reference and page["source_version"] == version
            collected += [(row["chat_id"], row["id"]) for row in page["items"]]
        assert collected == [("100", "10"), ("100", "2"), ("200", "10")]
        originals = data(
            await mcp.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": page["job_id"],
                    "evidence_ref": reference,
                    "message_keys": [
                        {"chat_id": "100", "message_id": "10"},
                        {"chat_id": "200", "message_id": "10"},
                    ],
                },
            )
        )
        assert originals["ok"], originals
        assert [row["text"] for row in originals["data"]["items"]] == ["alpha beta", "alpha beta"]
        assert originals["data"]["source_version"] == version
        fresh = data(await mcp.call_tool("messages_search_local", args))
        assert fresh["ok"], fresh
        assert [(row["chat_id"], row["id"]) for row in fresh["data"]["items"]] == [("100", "2")]


@pytest.mark.asyncio
async def test_reconstructed_snippets_and_originals_reapply_current_private_metadata_policy(
    tmp_path,
):
    store = Store(tmp_path)
    store.save_messages(
        [
            Message(
                profile_id="personal",
                chat_id="100",
                id="1",
                date=NOW,
                text="alpha Private metadata",
                text_source="reconstructed",
                original_text="alpha",
                rich_text={
                    "blocks": [
                        {"_": "PageBlockParagraph", "text": "alpha"},
                        {"_": "User", "id": "777", "title": "Private metadata"},
                    ],
                    "reconstructed_text": "alpha Private metadata",
                },
            )
        ]
    )
    store.close()
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {"profile_id": "personal", "chat_ids": ["100"], "query": "alpha"}
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        first = data(await mcp.call_tool("messages_search_local", args))["data"]
        settings.profiles["personal"].read_mode = "selected"
        settings.profiles["personal"].read_chats = ["100"]
        originals = data(
            await mcp.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": first["job_id"],
                    "evidence_ref": first["evidence_ref"],
                    "message_keys": [{"chat_id": "100", "message_id": "1"}],
                },
            )
        )
        assert originals["ok"], originals
        row = originals["data"]["items"][0]
        assert "Private metadata" not in str(row)
        assert row["original_text"] == "alpha"
        hit = data(await mcp.call_tool("messages_search_local", args))["data"]["items"][0]
        assert hit["snippet"] == "alpha"
        assert hit["text_source"] == "reconstructed"


@pytest.mark.asyncio
async def test_rank_precedes_date_ties_and_snippets_and_hit_budget_are_bounded(tmp_path):
    store = Store(tmp_path)
    rows = [
        ("100", "10", NOW, "alpha beta alpha beta alpha beta"),
        ("100", "2", NOW, "alpha beta"),
        ("200", "10", NOW, "alpha beta"),
        ("100", "20", NOW + timedelta(hours=1), "alpha beta"),
        ("100", "21", NOW + timedelta(hours=2), "alpha beta"),
        ("100", "1", NOW - timedelta(hours=1), "alpha beta"),
        ("100", "11", NOW + timedelta(seconds=1), "noise " * 100 + "alpha beta"),
        ("100", "90", NOW, "alphabet beta"),
    ]
    store.save_messages(
        [
            Message(profile_id="personal", chat_id=chat, id=id_, date=date, text=text)
            for chat, id_, date, text in rows
        ]
    )
    store.save_messages(
        [
            Message(
                profile_id="work",
                chat_id="100",
                id="10",
                date=NOW,
                text="alpha beta alpha beta alpha beta",
            )
        ]
    )
    store.close()
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {
        "profile_id": "personal",
        "chat_ids": ["100", "200"],
        "query": "alpha beta",
        "since": NOW.isoformat(),
        "until": (NOW + timedelta(hours=2)).isoformat(),
        "limit": 100,
        "snippet_characters": 32,
    }
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        page = data(await mcp.call_tool("messages_search_local", args))
        assert page["ok"], page
        hits = page["data"]["items"]
        assert [(hit["chat_id"], hit["id"]) for hit in hits] == [
            ("100", "10"),
            ("100", "20"),
            ("100", "2"),
            ("200", "10"),
            ("100", "11"),
        ]
        assert hits[0]["rank"] < hits[1]["rank"] < hits[-1]["rank"]
        assert all(len(hit["snippet"]) <= 32 for hit in hits)
        assert hits[-1]["snippet_truncated"]
        assert page["data"]["coverage"]["ordering"] == "bm25_asc_date_desc_chat_asc_message_id_desc"
        capped = data(await mcp.call_tool("messages_search_local", {**args, "max_hits": 2}))["data"]
        assert len(capped["items"]) == 2 and capped["next_cursor"] is None
        assert capped["coverage"]["stopped_reason"] == "hit_budget"
        assert capped["coverage"]["matched_total"] is None
        literal = data(
            await mcp.call_tool("messages_search_local", {**args, "query": "alpha OR beta"})
        )
        assert literal["ok"] and literal["data"]["items"] == []
        wildcard = data(
            await mcp.call_tool("messages_search_local", {**args, "query": "alpha* beta"})
        )
        assert wildcard["ok"], wildcard
        assert "90" not in [row["id"] for row in wildcard["data"]["items"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,backend", [("user", "bot_api"), ("bot", "bot_api"), ("bot", "mtproto")]
)
async def test_coverage_no_match_and_acl_are_local_and_conservative(tmp_path, kind, backend):
    seed(tmp_path)
    store = Store(tmp_path)
    checkpoint = {
        "since": (NOW - timedelta(days=2)).isoformat(),
        "until": (NOW + timedelta(days=2)).isoformat(),
        "complete": True,
        "updated_at": (NOW - timedelta(days=1)).isoformat(),
        "before": 1,
    }
    with store.db:
        store.set_state("sync:personal:100", checkpoint)
    store.delete_messages("personal", None, ["10"])
    store.close()
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind=kind,
                bot_backend=backend,
                read_mode="selected",
                read_chats=["100", "200", "400"],
            )
        },
    )
    args = {
        "profile_id": "personal",
        "chat_ids": ["100", "200", "400"],
        "query": "uncollectedword",
        "since": NOW.isoformat(),
        "until": (NOW + timedelta(days=1)).isoformat(),
    }
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        page = data(await mcp.call_tool("messages_search_local", args))
        assert page["ok"], page
        page = page["data"]
        assert page["items"] == [] and page["incomplete"]
        assert page["coverage"]["telegram_complete"] is False
        assert page["coverage"]["backend_scope"] == (
            "saved_updates_only" if kind == "bot" else "collected_index_only"
        )
        coverage = {chat["chat_id"]: chat for chat in page["coverage"]["chats"]}
        assert coverage["100"]["sync"] == checkpoint
        assert coverage["100"]["last_collection_at"] == checkpoint["updated_at"]
        assert coverage["100"]["requested_range_collected"] is True
        assert coverage["100"]["stale_possible"] and coverage["100"]["known_gap"]
        assert coverage["200"]["freshness_unknown"] and coverage["200"]["index_status"] == "partial"
        assert coverage["400"]["index_status"] == "missing"
        assert "absence" in " ".join(page["warnings"])
        denied = data(
            await mcp.call_tool("messages_search_local", {**args, "chat_ids": ["100", "300"]})
        )
        assert denied["error"]["code"] == "read_not_allowed"
        listed = data(await mcp.call_tool("jobs_status", {"profile_id": "personal"}))["data"][
            "jobs"
        ]
        assert len(listed) == 1  # denied selection cannot create a frozen evidence job
        for changed, code in [
            ({"chat_ids": []}, "invalid_selection"),
            ({"chat_ids": ["100", "100"]}, "invalid_selection"),
            ({"chat_ids": ["0100"]}, "invalid_id"),
            ({"query": " "}, "invalid_query"),
            ({"query": "a " * 33}, "invalid_query"),
            ({"since": (NOW + timedelta(days=2)).isoformat()}, "invalid_range"),
        ]:
            result = data(await mcp.call_tool("messages_search_local", {**args, **changed}))
            assert result["error"]["code"] == code


@pytest.mark.asyncio
async def test_frozen_hits_restart_scoped_cursor_expiry_policy_and_generation(
    tmp_path, monkeypatch
):
    seed(tmp_path)
    clock = [NOW]
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock[0])
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")}
    )
    args = {"profile_id": "personal", "chat_ids": ["100", "200"], "query": "alpha beta", "limit": 1}
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        first = data(await mcp.call_tool("messages_search_local", args))["data"]
        cursor, reference, version = (
            first["next_cursor"],
            first["evidence_ref"],
            first["source_version"],
        )
    store = Store(tmp_path)
    store.delete_messages("personal", "100", ["10"])
    store.close()
    original_args = {
        "profile_id": "personal",
        "job_id": first["job_id"],
        "evidence_ref": reference,
        "message_keys": [{"chat_id": "100", "message_id": "10"}],
    }
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        for changed in [
            {"query": "beta"},
            {"chat_ids": ["100"]},
            {"max_hits": 20},
            {"snippet_characters": 32},
            {"profile_id": "work"},
            {"until": NOW.isoformat()},
        ]:
            mismatch = data(
                await mcp.call_tool("messages_search_local", {**args, "cursor": cursor, **changed})
            )
            assert mismatch["error"]["code"] == "invalid_cursor"
        continued = data(await mcp.call_tool("messages_search_local", {**args, "cursor": cursor}))
        assert continued["ok"], continued
        assert continued["data"]["source_version"] == version
        assert continued["data"]["evidence_ref"] == reference
        replay = data(await mcp.call_tool("messages_search_local", {**args, "cursor": cursor}))
        assert replay == continued
        original = data(await mcp.call_tool("jobs_results", original_args))
        assert original["ok"] and original["data"]["items"][0]["text"] == "alpha beta"
        outside = data(
            await mcp.call_tool(
                "jobs_results",
                {**original_args, "message_keys": [{"chat_id": "100", "message_id": "11"}]},
            )
        )
        assert outside["error"]["code"] == "message_not_in_snapshot"
        config = settings.profiles["personal"]
        config.read_mode, config.read_chats = "selected", ["100"]
        revoked = data(await mcp.call_tool("jobs_results", original_args))
        assert revoked["error"]["code"] == "read_not_allowed"
        narrow_cursor = data(
            await mcp.call_tool(
                "messages_search_local", {**args, "chat_ids": ["100"], "cursor": cursor}
            )
        )
        assert narrow_cursor["error"]["code"] == "invalid_cursor"
        config.read_mode, config.read_chats = "all", []
        generation = config.generation
        config.generation = "replacement"
        changed_account = data(await mcp.call_tool("jobs_results", original_args))
        assert changed_account["error"]["code"] == "account_changed"
        config.generation = generation
        clock[0] = NOW + timedelta(minutes=20)
        expired_cursor = data(
            await mcp.call_tool("messages_search_local", {**args, "cursor": cursor})
        )
        assert expired_cursor["error"]["code"] == "invalid_cursor"
        pinned = data(await mcp.call_tool("jobs_results", original_args))
        assert pinned["ok"] and pinned["data"]["source_version"] == version
        clock[0] = NOW + timedelta(minutes=31)
        expired_ref = data(await mcp.call_tool("jobs_results", original_args))
        assert expired_ref["error"]["code"] == "reference_expired"
        durable = data(
            await mcp.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": first["job_id"]}
            )
        )
        assert durable["ok"] and durable["data"]["items"][0]["text"] == "alpha beta"


@pytest.mark.asyncio
async def test_hit_pages_bound_snippets_and_freeze_budget_reports_omitted_matches(tmp_path):
    store = Store(tmp_path)
    store.save_messages(
        [
            Message(
                profile_id="personal",
                chat_id="100",
                id=str(i),
                date=NOW,
                text="alpha " + "x" * 4994,
            )
            for i in range(1, 41)
        ]
        + [
            Message(
                profile_id="personal",
                chat_id="200",
                id=str(i),
                date=NOW,
                text="oversize " + "x" * 49991,
            )
            for i in range(1, 22)
        ]
    )
    store.close()
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {
        "profile_id": "personal",
        "chat_ids": ["100"],
        "query": "alpha",
        "limit": 100,
        "snippet_characters": 1000,
    }
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        first = data(await mcp.call_tool("messages_search_local", args))
        assert first["ok"], first
        assert len(first["data"]["items"]) == 32
        assert sum(len(hit["snippet"]) for hit in first["data"]["items"]) == 32000
        second = data(
            await mcp.call_tool(
                "messages_search_local", {**args, "cursor": first["data"]["next_cursor"]}
            )
        )
        assert second["ok"] and len(second["data"]["items"]) == 8
        assert second["data"]["next_cursor"] is None
        budget = data(
            await mcp.call_tool(
                "messages_search_local", {**args, "chat_ids": ["200"], "query": "oversize"}
            )
        )
        assert budget["ok"], budget
        assert budget["data"]["coverage"]["stopped_reason"] == "character_budget"
        assert budget["data"]["coverage"]["frozen_hits"] == 20
        assert budget["data"]["incomplete"]
