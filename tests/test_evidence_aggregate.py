"""T07: typed counts over terminal collected evidence through HTTP MCP."""

import asyncio
import zoneinfo
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from teleloom.config import Profile, Settings
from teleloom.evaluation import _call
from teleloom.models import Chat, Message, TeleloomError
from tests.fakes import TelegramAPI, data
from tests.test_jobs import complete
from tests.test_transport import client, running

NOW = datetime(2025, 4, 3, tzinfo=UTC)
START = "2025-03-31T00:00:00Z"
END = "2025-04-03T00:00:00Z"


class AggregateAPI(TelegramAPI):
    instances = []

    def __init__(self, *args):
        super().__init__(*args)
        self.history_calls = 0
        self.instances.append(self)
        self.rows = [
            Message(
                profile_id=self.profile,
                chat_id=chat,
                id=id_,
                date=datetime.fromisoformat(date),
                text=f"Original {chat}/{id_}",
                edited_at=NOW,
                outgoing=outgoing is True,
                sender_id=sender,
                kind=kind,
            ).model_copy(update={"outgoing": outgoing})
            for chat, id_, date, outgoing, sender, kind in [
                ("10", "1", "2025-03-31T19:59:59Z", False, "11", "message"),
                ("10", "2", "2025-03-31T20:00:00Z", True, "12", "message"),
                ("2", "1", "2025-04-01T19:59:59Z", False, None, "service"),
                ("2", "2", "2025-04-01T20:00:00Z", None, None, "message"),
            ]
        ]

    async def chats(self):
        return [Chat(id=id_, title=f"Chat {id_}") for id_ in ["10", "2", "3"]]

    async def resolve(self, target):
        return next(chat for chat in await self.chats() if chat.id == target)

    async def history(self, chat, **kwargs):
        self.history_calls += 1
        return await super().history(chat, **kwargs)


async def start_job(server, **overrides):
    args = {
        "profile_id": "personal",
        "chat_ids": ["10", "2", "3"],
        "since": START,
        "until": END,
        **overrides,
    }
    return data(await server.call_tool("digest_context_many_start", args))["data"]["job_id"]


class TimezoneAPI(AggregateAPI):
    def __init__(self, *args):
        super().__init__(*args)
        self.rows = [
            Message(
                profile_id=self.profile,
                chat_id="10",
                id=str(index),
                date=datetime.fromisoformat(date),
                text="Fictional publication around local midnight and the DST transition.",
            )
            for index, date in enumerate(
                [
                    "2025-03-09T04:59:59Z",
                    "2025-03-09T05:00:00Z",
                    "2025-03-10T03:59:59Z",
                    "2025-03-10T04:00:00Z",
                ],
                start=1,
            )
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timezone", "expected"),
    [
        ("Indian/Mauritius", [("2025-03-09", 2), ("2025-03-10", 2)]),
        ("+04:00", [("2025-03-09", 2), ("2025-03-10", 2)]),
        ("UTC", [("2025-03-09", 2), ("2025-03-10", 2)]),
        ("America/New_York", [("2025-03-08", 1), ("2025-03-09", 2), ("2025-03-10", 1)]),
    ],
)
async def test_aggregate_local_days_without_system_timezone_data(
    tmp_path, monkeypatch, timezone, expected
):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    # Bypass the zone cache as well as system files: only the declared tzdata can help.
    monkeypatch.setattr("teleloom.reading_jobs.ZoneInfo", zoneinfo.ZoneInfo.no_cache)
    original_path = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(())
    try:
        settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
        async with running(settings, TimezoneAPI) as app, client(app, settings) as server:
            job = await start_job(
                server,
                chat_ids=["10"],
                since="2025-03-08T00:00:00Z",
                until="2025-03-11T00:00:00Z",
            )
            await complete(server, "personal", job)
            base = {"profile_id": "personal", "job_id": job}
            originals = data(await server.call_tool("jobs_results", base))["data"]
            calls = AggregateAPI.instances[-1].history_calls
            response = data(
                await server.call_tool(
                    "jobs_results",
                    base
                    | {
                        "view": "aggregate",
                        "evidence_ref": originals["evidence_ref"],
                        "aggregate": {"group_by": ["day"], "timezone": timezone},
                    },
                )
            )
            assert response["ok"], response
            result = response["data"]
            assert (
                sorted((row["day"], row["count"]) for row in result["aggregate"]["groups"])
                == expected
            )
            assert result["aggregate"]["total"] == 4
            assert result["coverage"] == originals["coverage"]
            assert result["source_version"] == originals["source_version"]
            assert AggregateAPI.instances[-1].history_calls == calls
    finally:
        zoneinfo.reset_tzpath(original_path)


@pytest.mark.asyncio
async def test_aggregate_counts_frozen_messages_with_coverage_identity_and_no_reread(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, AggregateAPI) as app, client(app, settings) as server:
        job = await start_job(server)
        before = await complete(server, "personal", job)
        original = data(
            await server.call_tool("jobs_results", {"profile_id": "personal", "job_id": job})
        )["data"]
        adapter = AggregateAPI.instances[-1]
        calls = adapter.history_calls
        request = {
            "profile_id": "personal",
            "job_id": job,
            "view": "aggregate",
            "aggregate": {
                "group_by": ["chat", "day"],
                "metrics": ["count", "incoming_count"],
                "timezone": "+04:00",
                "top_k": 2,
            },
        }
        response = data(await server.call_tool("jobs_results", request))
        assert response["ok"], response
        result = response["data"]
        assert result["status"] == "completed"
        assert result["source_version"] == original["source_version"]
        assert len(result["source_hash"]) == 64
        assert result["source_identity"] == {
            "profile_id": "personal",
            "generation": settings.profiles["personal"].generation,
            "job_id": job,
            "kind": "evidence",
        }
        assert [row["id"] for row in result["selection"]["items"]] == ["10", "2", "3"]
        assert result["period"] == {
            "since": "2025-03-31T00:00:00+00:00",
            "until": "2025-04-03T00:00:00+00:00",
            "interval": "[since,until)",
            "time_basis": "publication_date",
        }
        assert result["coverage"] == original["coverage"]
        assert result["incomplete"] is False
        assert result["items"] == []
        aggregate = result["aggregate"]
        assert aggregate["definition"] == request["aggregate"] | {
            "include_outgoing": True,
            "include_service": True,
        }
        assert aggregate["denominator"] == "collected_evidence"
        assert aggregate["observed_count"] == aggregate["total"] == 4
        assert aggregate["totals"] == {
            "count": 4,
            "incoming_count": None,
            "known_incoming_count": 2,
            "unknown_incoming_count": 1,
            "unknown_sender_count": 2,
        }
        assert aggregate["group_count"] == 4
        assert aggregate["groups_truncated"] is True
        assert [(row["chat_id"], row["day"], row["count"]) for row in aggregate["groups"]] == [
            ("2", "2025-04-01", 1),
            ("2", "2025-04-02", 1),
        ]
        repeated = data(await server.call_tool("jobs_results", request))["data"]
        assert repeated == result
        after = data(
            await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
        )["data"]
        assert after == before
        originals = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": original["evidence_ref"],
                    "message_keys": [{"chat_id": "2", "message_id": "2"}],
                },
            )
        )["data"]
        assert originals["items"] == [original["items"][2]]
        assert adapter.history_calls == calls


class PartialAPI(AggregateAPI):
    async def history(self, chat, **kwargs):
        if chat == "2":
            self.history_calls += 1
            raise TeleloomError("chat_unavailable", "Fictional unavailable history.")
        return await super().history(chat, **kwargs)


@pytest.mark.asyncio
async def test_aggregate_keeps_failed_and_pending_gaps_and_unknown_total(tmp_path, monkeypatch):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, PartialAPI) as app, client(app, settings) as server:
        job = await start_job(server, max_requests=2)
        before = await complete(server, "personal", job)
        page = data(
            await server.call_tool("jobs_results", {"profile_id": "personal", "job_id": job})
        )["data"]
        result = data(
            await server.call_tool(
                "jobs_results",
                {"profile_id": "personal", "job_id": job, "view": "aggregate"},
            )
        )["data"]
        assert result["coverage"] == page["coverage"]
        assert [chat["status"] for chat in result["coverage"]["chats"]] == [
            "completed",
            "failed",
            "budget_exhausted",
        ]
        assert result["coverage"]["stopped_reason"] == "request_budget"
        assert result["unavailable"] == result["errors"] == page["errors"]
        assert result["incomplete"] is True
        assert result["aggregate"]["observed_count"] == 2
        assert result["aggregate"]["total"] is None
        assert result["aggregate"]["totals"]["incoming_count"] == 1
        assert any("coverage gaps" in warning for warning in result["warnings"])
        assert (
            data(await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job}))[
                "data"
            ]
            == before
        )


@pytest.mark.asyncio
async def test_aggregate_empty_complete_and_bot_scope_distinguish_zero_from_unknown(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user"), "bot": Profile(kind="bot")},
    )
    async with running(settings, AggregateAPI) as app, client(app, settings) as server:
        for profile, source, total, incomplete in [
            ("personal", "telegram", 0, False),
            ("bot", "bot_updates", None, True),
        ]:
            job = await start_job(server, profile_id=profile, chat_ids=["3"])
            await complete(server, profile, job)
            result = data(
                await server.call_tool(
                    "jobs_results", {"profile_id": profile, "job_id": job, "view": "aggregate"}
                )
            )["data"]
            assert result["source"] == source
            assert result["incomplete"] is incomplete
            assert result["aggregate"]["observed_count"] == 0
            assert result["aggregate"]["total"] == total
            assert result["aggregate"]["totals"] == {
                "count": 0,
                "incoming_count": 0,
                "known_incoming_count": 0,
                "unknown_incoming_count": 0,
                "unknown_sender_count": 0,
            }
            assert result["aggregate"]["groups"] == []


@pytest.mark.asyncio
async def test_typed_filters_exclude_known_outgoing_services_and_preserve_unknowns(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, AggregateAPI) as app, client(app, settings) as server:
        job = await start_job(server)
        await complete(server, "personal", job)
        base = {"profile_id": "personal", "job_id": job, "view": "aggregate"}
        unknown = data(
            await server.call_tool(
                "jobs_results",
                base | {"aggregate": {"include_outgoing": False, "include_service": False}},
            )
        )["data"]
        assert unknown["aggregate"]["observed_count"] == 1
        assert unknown["aggregate"]["total"] is None
        assert unknown["aggregate"]["unknown_filter_count"] == 1
        assert unknown["aggregate"]["totals"]["incoming_count"] == 1
        assert unknown["incomplete"] is True
        assert any("unknown filter facts" in warning for warning in unknown["warnings"])
        known = data(
            await server.call_tool(
                "jobs_results",
                base
                | {
                    "aggregate": {
                        "include_service": False,
                        "metrics": ["count"],
                        "group_by": ["day"],
                    }
                },
            )
        )["data"]
        assert known["source_hash"] == unknown["source_hash"]
        assert known["aggregate"]["observed_count"] == known["aggregate"]["total"] == 3
        assert known["aggregate"]["totals"] == {"count": 3, "unknown_sender_count": 1}
        assert [(row["day"], row["count"]) for row in known["aggregate"]["groups"]] == [
            ("2025-03-31", 2),
            ("2025-04-01", 1),
        ]


class WaitingAPI(AggregateAPI):
    async def history(self, chat, **kwargs):
        self.waiting = asyncio.Event()
        self.waiting.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_nonterminal_and_incompatible_aggregate_requests_fail_explicitly(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, WaitingAPI) as app, client(app, settings) as server:
        job = await start_job(server)
        paused = data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "pause"}
            )
        )["data"]
        assert paused["status"] == "paused"
        base = {"profile_id": "personal", "job_id": job, "view": "aggregate"}
        response = data(await server.call_tool("jobs_results", base))
        assert response["error"]["code"] == "aggregate_not_ready"
        for incompatible in [
            {"cursor": "anything"},
            {"message_keys": [{"chat_id": "10", "message_id": "1"}]},
        ]:
            response = data(await server.call_tool("jobs_results", base | incompatible))
            assert response["error"]["code"] == "invalid_aggregate"
        cancelled = data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "cancel"}
            )
        )["data"]
        assert cancelled["status"] == "cancelled"
        result = data(await server.call_tool("jobs_results", base))["data"]
        assert result["status"] == "cancelled"
        assert result["aggregate"]["total"] is None
        assert result["coverage"]["chats"][0]["complete"] is False


@pytest.mark.asyncio
async def test_aggregate_allowlist_schema_rejects_sql_invalid_definitions_and_job_kinds(tmp_path):
    AggregateAPI.instances = []
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, AggregateAPI) as app, client(app, settings) as server:
        tools = {tool.name: tool for tool in (await server.list_tools()).tools}
        for name in ["messages_search_many_start", "digest_context_many_start"]:
            properties = tools[name].inputSchema["properties"]
            assert "1..1000000" in properties["max_characters"]["description"]
            assert "all selected chats" in properties["max_characters"]["description"]
            assert "1..1000000" in tools[name].description
            invalid_start = {
                "profile_id": "personal",
                "chat_ids": ["10"],
                "since": START,
                "until": END,
                "max_characters": 1100000,
            }
            if name == "messages_search_many_start":
                invalid_start["query"] = "Original"
            assert (await server.call_tool(name, invalid_start)).isError
        assert data(await server.call_tool("jobs_status", {"profile_id": "personal"}))["data"] == {
            "jobs": []
        }
        assert not AggregateAPI.instances  # Invalid inputs never reach Telegram selection/history.
        schema = tools["jobs_results"].inputSchema
        assert "without evidence_ref" in schema["properties"]["cursor"]["description"]
        assert "without cursor" in schema["properties"]["evidence_ref"]["description"]
        assert "cursor and evidence_ref" in tools["jobs_results"].description
        assert schema["properties"]["view"]["default"] == "messages"
        assert schema["properties"]["coverage"]["default"] == "full"
        assert schema["properties"]["coverage"]["enum"] == ["full", "compact"]
        definition = schema["$defs"]["EvidenceAggregate"]
        assert "Indian/Mauritius" in definition["properties"]["timezone"]["description"]
        assert definition["additionalProperties"] is False
        assert definition["properties"]["group_by"]["items"]["enum"] == ["chat", "day"]
        job = await start_job(server)
        await complete(server, "personal", job)
        base = {"profile_id": "personal", "job_id": job, "view": "aggregate"}
        for invalid in [
            {"sql": "SELECT * FROM messages"},
            {"group_by": ["sender"]},
            {"group_by": ["chat", "chat"]},
            {"metrics": []},
            {"metrics": ["count", "count"]},
            {"metrics": ["telegram_total"]},
            {"top_k": 0},
            {"top_k": 1001},
            {"top_k": True},
            {"include_outgoing": "false"},
        ]:
            assert (await server.call_tool("jobs_results", base | {"aggregate": invalid})).isError
        for invalid_zone in ["Not/AZone", "+24:00", "+04:60", "04:00"]:
            response = data(
                await server.call_tool(
                    "jobs_results", base | {"aggregate": {"timezone": invalid_zone}}
                )
            )
            assert response["error"]["code"] == "invalid_timezone"
        response = data(
            await server.call_tool("jobs_results", base | {"view": "messages", "aggregate": {}})
        )
        assert response["error"]["code"] == "invalid_aggregate"
        activity = data(
            await server.call_tool("activity_start", {"profile_id": "personal", "chat_ids": ["10"]})
        )["data"]["job_id"]
        await complete(server, "personal", activity)
        response = data(await server.call_tool("jobs_results", base | {"job_id": activity}))
        assert response["error"]["code"] == "unsupported_job_results"


class ThousandAPI(AggregateAPI):
    def __init__(self, *args):
        super().__init__(*args)
        self.rows = [
            Message(
                profile_id=self.profile,
                chat_id=str(100 + index % 5),
                id=str(index + 1),
                date=datetime(2025, 3, 25, 12, tzinfo=UTC) + timedelta(days=(index // 5) % 7),
                text=f"Fictional collected original {index + 1}: preserve this evidence exactly.",
                outgoing=bool(index % 2),
                sender_id=str(1000 + index % 13),
            )
            for index in range(1000)
        ]

    async def chats(self):
        return [Chat(id=str(100 + index), title=f"Fictional chat {index}") for index in range(5)]


@pytest.mark.asyncio
async def test_1000_message_35_bucket_complete_mcp_payload_is_at_most_ten_percent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ThousandAPI) as app, client(app, settings) as server:
        job = await start_job(
            server, chat_ids=[str(100 + index) for index in range(5)], since="2025-03-25T00:00:00Z"
        )
        # The shared helper allows two seconds; fifteen queue steps need a longer bound.
        async with asyncio.timeout(15):
            while True:
                before = data(
                    await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
                )["data"]
                if before["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.1)
        assert before["status"] == "completed", before
        calls = AggregateAPI.instances[-1].history_calls
        base = {"profile_id": "personal", "job_id": job}
        originals, enumeration = [], []
        cursor = None
        while True:
            page, measured = await _call(
                server, "jobs_results", base | {"limit": 100, "cursor": cursor}
            )
            originals.extend(page["items"])
            enumeration.append(measured)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        result, measured = await _call(
            server,
            "jobs_results",
            base | {"view": "aggregate", "aggregate": {"group_by": ["chat", "day"]}},
        )
        assert (
            len(originals)
            == result["aggregate"]["observed_count"]
            == result["aggregate"]["total"]
            == 1000
        )
        assert result["aggregate"]["totals"] == {
            "count": 1000,
            "incoming_count": 500,
            "known_incoming_count": 500,
            "unknown_incoming_count": 0,
            "unknown_sender_count": 0,
        }
        assert result["aggregate"]["group_count"] == len(result["aggregate"]["groups"]) == 35
        assert result["aggregate"]["groups_truncated"] is False
        expected = Counter((row["chat_id"], row["date"][:10]) for row in originals)
        incoming = Counter(
            (row["chat_id"], row["date"][:10]) for row in originals if row["outgoing"] is False
        )
        assert {
            (row["chat_id"], row["day"]): row["count"] for row in result["aggregate"]["groups"]
        } == expected
        assert {
            (row["chat_id"], row["day"]): row["incoming_count"]
            for row in result["aggregate"]["groups"]
        } == incoming
        assert result["coverage"] == page["coverage"]
        assert result["source_version"] == page["source_version"]
        assert result["incomplete"] is page["incomplete"] is False
        assert measured["representations_agree"] and all(
            row["representations_agree"] for row in enumeration
        )
        enumeration_bytes = sum(row["response_bytes"] for row in enumeration)
        total_enumeration_bytes = sum(
            row["request_bytes"] + row["response_bytes"] for row in enumeration
        )
        total_aggregate_bytes = measured["request_bytes"] + measured["response_bytes"]
        assert measured["response_bytes"] <= enumeration_bytes * 0.1
        assert total_aggregate_bytes <= total_enumeration_bytes * 0.1
        print(
            {
                "messages": len(originals),
                "buckets": 35,
                "enumeration_response_bytes": enumeration_bytes,
                "aggregate_response_bytes": measured["response_bytes"],
                "enumeration_request_response_bytes": total_enumeration_bytes,
                "aggregate_request_response_bytes": total_aggregate_bytes,
                "response_ratio": measured["response_bytes"] / enumeration_bytes,
            }
        )
        assert data(await server.call_tool("jobs_status", base))["data"] == before
        assert AggregateAPI.instances[-1].history_calls == calls


@pytest.mark.asyncio
async def test_aggregate_reference_restart_drift_policy_generation_and_expiry(
    tmp_path, monkeypatch
):
    clock = NOW
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")},
    )
    async with running(settings, AggregateAPI) as app, client(app, settings) as server:
        job = await start_job(server)
        await complete(server, "personal", job)
        base = {"profile_id": "personal", "job_id": job, "view": "aggregate"}
        original = data(await server.call_tool("jobs_results", base | {"view": "messages"}))["data"]
        reference = original["evidence_ref"]
        aggregate = data(await server.call_tool("jobs_results", base))["data"]
        adapter = AggregateAPI.instances[-1]
        calls = adapter.history_calls
        adapter.rows[0].text = "Changed after collection"
        adapter.rows[1].deleted = True
        adapter.rows[2].outgoing = True
        adapter.rows[3].sender_id = "999"
        assert data(await server.call_tool("jobs_results", base))["data"] == aggregate
        assert (
            data(await server.call_tool("jobs_results", base | {"evidence_ref": reference}))["data"]
            == aggregate
        )
        assert adapter.history_calls == calls
    async with running(settings, AggregateAPI) as app, client(app, settings) as server:
        assert data(await server.call_tool("jobs_results", base))["data"] == aggregate
        assert (
            data(await server.call_tool("jobs_results", base | {"evidence_ref": reference}))["data"]
            == aggregate
        )
        exact = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [{"chat_id": "10", "message_id": "1"}],
                },
            )
        )["data"]
        assert exact["items"][0]["text"] == "Original 10/1"
        assert adapter.history_calls == calls
        for extra in [{}, {"evidence_ref": reference}]:
            foreign = data(
                await server.call_tool("jobs_results", base | extra | {"profile_id": "work"})
            )
            assert foreign["error"]["code"] == "job_not_found"
        settings.profiles["personal"].read_mode = "selected"
        settings.profiles["personal"].read_chats = ["10"]
        for extra in [{}, {"evidence_ref": reference}]:
            revoked = data(await server.call_tool("jobs_results", base | extra))
            assert revoked["error"]["code"] == "read_not_allowed"
        settings.profiles["personal"].read_mode = "all"
        clock += timedelta(minutes=31)
        expired = data(await server.call_tool("jobs_results", base | {"evidence_ref": reference}))
        assert expired["error"]["code"] == "reference_expired"
        assert data(await server.call_tool("jobs_results", base))["data"] == aggregate
        settings.profiles["personal"].generation = "replacement-generation"
        for extra in [{}, {"evidence_ref": reference}]:
            changed = data(await server.call_tool("jobs_results", base | extra))
            assert changed["error"]["code"] == "account_changed"


class WrongIdentityAPI(AggregateAPI):
    wrong_identity = {}

    async def history(self, chat, **kwargs):
        return [
            row.model_copy(update=self.wrong_identity)
            for row in await super().history(chat, **kwargs)
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [{"profile_id": "work"}, {"chat_id": "999"}])
async def test_aggregate_preserves_rejection_of_foreign_telegram_evidence(
    tmp_path, monkeypatch, wrong
):
    monkeypatch.setattr(WrongIdentityAPI, "wrong_identity", wrong)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", read_mode="selected", read_chats=["10"])},
    )
    async with running(settings, WrongIdentityAPI) as app, client(app, settings) as server:
        job = await start_job(server, chat_ids=["10"])
        before = await complete(server, "personal", job)
        assert before["progress"] == 0
        base = {"profile_id": "personal", "job_id": job}
        result = data(await server.call_tool("jobs_results", base | {"view": "aggregate"}))["data"]
        assert result["aggregate"]["observed_count"] == 0
        assert result["aggregate"]["total"] is None
        assert result["incomplete"] is True
        assert result["coverage"]["chats"][0]["error"]["code"] == "invalid_evidence"
        assert data(await server.call_tool("jobs_status", base))["data"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["complete", "partial", "bot", "index"])
async def test_compact_coverage_and_details_share_frozen_counts_originals_and_warnings(
    tmp_path, monkeypatch, case
):
    import json

    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="bot" if case == "bot" else "user")},
    )

    class SavedAggregateAPI(AggregateAPI):
        def __init__(self, *args):
            super().__init__(*args)
            self.store.save_messages(
                [row.model_copy(update={"outgoing": row.outgoing is True}) for row in self.rows]
            )

    api = PartialAPI if case == "partial" else SavedAggregateAPI if case == "bot" else AggregateAPI
    async with running(settings, api) as app, client(app, settings) as server:
        job = await start_job(server, **({"max_requests": 2} if case == "partial" else {}))
        await complete(server, "personal", job)
        adapter = api.instances[-1]
        if case == "index":
            adapter.store.save_messages(
                [row.model_copy(update={"outgoing": row.outgoing is True}) for row in adapter.rows]
            )
            adapter.store.set_state("gap:personal", "Fictional offline gap.")
            indexed = data(
                await server.call_tool(
                    "messages_search_local",
                    {
                        "profile_id": "personal",
                        "chat_ids": ["10", "2", "3"],
                        "query": "Original",
                        "since": START,
                        "until": END,
                    },
                )
            )["data"]
            job = indexed["job_id"]
        base = {"profile_id": "personal", "job_id": job}
        full = data(await server.call_tool("jobs_results", base))["data"]
        ref = full["evidence_ref"]
        request = base | {"evidence_ref": ref, "view": "aggregate"}
        counted = data(await server.call_tool("jobs_results", request))["data"]
        response = data(await server.call_tool("jobs_results", request | {"coverage": "compact"}))
        assert response["ok"], response
        compact = response["data"]
        summary = compact["coverage"]
        assert summary["detail"] == "compact"
        assert summary["scope"] == {
            "profile_id": "personal",
            "chat_ids": ["2", "3", "10"] if case == "index" else ["10", "2", "3"],
        }
        assert summary["requested_since"] == "2025-03-31T00:00:00+00:00"
        assert summary["requested_until"] == "2025-04-03T00:00:00+00:00"
        assert compact["status"] == "completed"
        assert summary["counts"]["observed_messages"] == (2 if case == "partial" else 4)
        assert summary["counts"]["total_messages"] == (4 if case == "complete" else None)
        assert compact["aggregate"] == counted["aggregate"]
        assert compact["source_hash"] == counted["source_hash"]
        assert compact["source_version"] == full["source_version"]
        assert "chats" not in summary and "errors" not in compact
        if case == "partial":
            assert summary["counts"]["complete_chats"] == 1
            assert summary["counts"]["unknown_chats"] == 2
            assert summary["gap_counts"] == {"failed": 1, "budget_exhausted": 1}
            assert summary["stopped_reason"] == "request_budget"
            assert summary["budget_stop"] is True
        if case == "index":
            assert summary["stale_possible_chats"] == 3
            assert summary["freshness_unknown_chats"] == 3
            assert summary["gap_counts"] == {"partial": 2, "missing": 1}
            assert summary["known_gap_chats"] == 3
            assert "Fictional offline gap." in compact["warnings"]
        for warning in counted["warnings"]:
            assert warning in compact["warnings"]
        if case != "complete":
            assert compact["warnings"] and compact["incomplete"] is True

        calls = adapter.history_calls
        adapter.rows[0].text = "Changed after collection"
        adapter.rows[1].deleted = True
        adapter.store.delete_messages("personal", "10", ["1", "2"])
        details = data(await server.call_tool("jobs_results", summary["details"]))["data"]
        assert details["coverage"] == full["coverage"]
        assert details.get("errors") == full.get("errors")
        assert details["items"] == []
        assert details["source_version"] == compact["source_version"]
        exact = data(
            await server.call_tool(
                "jobs_results",
                base
                | {
                    "evidence_ref": ref,
                    "message_keys": [{"chat_id": "10", "message_id": "1"}],
                    "coverage": "compact",
                },
            )
        )["data"]
        assert exact["items"][0]["text"] == "Original 10/1"
        assert exact["source_version"] == compact["source_version"]
        assert exact["coverage"] == summary
        shown = data(
            await server.call_tool(
                "jobs_results", base | {"coverage": "compact", "fields": ["text"], "limit": 1}
            )
        )["data"]
        assert shown["coverage"]["counts"] == summary["counts"]
        assert shown["next_cursor"] and shown["projection"]["fields"]
        continued = data(
            await server.call_tool(
                "jobs_results",
                base | {"cursor": shown["next_cursor"], "coverage": "compact", "limit": 1},
            )
        )["data"]
        assert continued["evidence_ref"] == shown["evidence_ref"]
        assert continued["source_version"] == shown["source_version"]
        assert continued["coverage"] == shown["coverage"]
        # A compact aggregate issued directly also freezes a resolvable revision.
        fresh = data(
            await server.call_tool(
                "jobs_results", base | {"view": "aggregate", "coverage": "compact"}
            )
        )["data"]
        assert fresh["evidence_ref"] and fresh["source_version"] == compact["source_version"]
        assert adapter.history_calls == calls
        assert adapter.ack == adapter.sent == []
        full_bytes = len(json.dumps(counted, ensure_ascii=False, separators=(",", ":")).encode())
        compact_bytes = len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode())
        print(f"{case}: full={full_bytes}, compact={compact_bytes}")


@pytest.mark.asyncio
async def test_compact_coverage_reference_denies_all_views_together_after_restart(
    tmp_path, monkeypatch
):
    clock = NOW
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")}
    )
    async with running(settings, AggregateAPI) as app, client(app, settings) as server:
        job = await start_job(server)
        await complete(server, "personal", job)
        base = {"profile_id": "personal", "job_id": job}
        first = data(
            await server.call_tool(
                "jobs_results", base | {"view": "coverage", "coverage": "compact"}
            )
        )["data"]
        ref = first["evidence_ref"]
        empty_job = await start_job(server, chat_ids=["3"])
        await complete(server, "personal", empty_job)
        empty = data(
            await server.call_tool(
                "jobs_results",
                base | {"job_id": empty_job, "view": "aggregate", "coverage": "compact"},
            )
        )["data"]
        assert empty["coverage"]["counts"]["total_messages"] == 0
        assert empty["incomplete"] is False
    async with running(settings, AggregateAPI) as app, client(app, settings) as server:
        summary = data(
            await server.call_tool(
                "jobs_results", base | {"evidence_ref": ref, "coverage": "compact"}
            )
        )["data"]
        assert summary == first
        requests = [
            base | {"evidence_ref": ref, "coverage": "compact"},
            summary["coverage"]["details"],
            base | {"evidence_ref": ref, "view": "aggregate", "coverage": "compact"},
            base
            | {
                "evidence_ref": ref,
                "message_keys": [{"chat_id": "10", "message_id": "1"}],
                "coverage": "compact",
            },
        ]
        for override, code in [
            ({"profile_id": "work"}, "job_not_found"),
            ({"job_id": empty_job}, "invalid_reference"),
            ({"evidence_ref": "nonexistent"}, "invalid_reference"),
        ]:
            for request in requests:
                denied = data(await server.call_tool("jobs_results", request | override))
                assert denied["error"]["code"] == code
                assert denied["data"] == {}
        profile = settings.profiles["personal"]
        profile.read_mode, profile.read_chats = "selected", ["10", "2", "3"]
        assert data(await server.call_tool("jobs_results", requests[0]))["ok"]
        # Revoking one sibling chat closes even the still-readable exact key.
        profile.read_chats = ["10"]
        for request in requests:
            assert (
                data(await server.call_tool("jobs_results", request))["error"]["code"]
                == "read_not_allowed"
            )
        profile.read_mode = "all"
        generation = profile.generation
        profile.generation = "replacement-generation"
        for request in requests:
            assert (
                data(await server.call_tool("jobs_results", request))["error"]["code"]
                == "account_changed"
            )
        profile.generation = generation
        clock += timedelta(minutes=31)
        for request in requests:
            assert (
                data(await server.call_tool("jobs_results", request))["error"]["code"]
                == "reference_expired"
            )
        fresh = data(await server.call_tool("jobs_results", base | {"coverage": "compact"}))["data"]
        assert fresh["items"][0]["text"] == "Original 10/2"
        assert fresh["evidence_ref"] != ref


@pytest.mark.asyncio
async def test_compact_coverage_fits_budget_without_hiding_gaps_or_losing_originals(
    tmp_path, monkeypatch
):
    import json

    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)

    class VerboseGapAPI(PartialAPI):
        def __init__(self, *args):
            super().__init__(*args)
            self.rows[0].text = "Исходный 😀 " * 4000

        async def history(self, chat, **kwargs):
            if chat == "2":
                self.history_calls += 1
                raise TeleloomError("chat_unavailable", "Fictional unavailable history. " * 1000)
            return await super().history(chat, **kwargs)

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, VerboseGapAPI) as app, client(app, settings) as server:
        job = await start_job(server, max_requests=2, max_characters=1000000)
        await complete(server, "personal", job)
        base = {"profile_id": "personal", "job_id": job, "view": "coverage"}
        full = data(await server.call_tool("jobs_results", base))["data"]
        base["evidence_ref"] = full["evidence_ref"]
        response = data(
            await server.call_tool(
                "jobs_results",
                base | {"coverage": "compact", "max_output_bytes": 6000, "fields": ["text"]},
            )
        )
        assert response["ok"], response
        compact = response["data"]

        def normalized(value):
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

        assert normalized(compact) == compact["output_budget"]["normalized_data_bytes"] <= 6000
        assert compact["coverage"]["gap_counts"] == {"failed": 1, "budget_exhausted": 1}
        assert compact["coverage"]["stopped_reason"] == "request_budget"
        assert compact["coverage"]["observed_rpc_requests"] is None
        assert compact["warnings"] and compact["source_version"] == full["source_version"]
        assert normalized(compact) < normalized(full) * 0.1
        details = data(await server.call_tool("jobs_results", compact["coverage"]["details"]))[
            "data"
        ]
        assert details["coverage"] == full["coverage"] and details["errors"] == full["errors"]
        for mode, budget in [("full", 6000), ("compact", 1)]:
            error = data(
                await server.call_tool(
                    "jobs_results", base | {"coverage": mode, "max_output_bytes": budget}
                )
            )
            assert error["error"]["code"] == "output_budget_too_small"
            assert error["error"]["details"]["minimum_bytes"] > budget
        adapter = VerboseGapAPI.instances[-1]
        calls = adapter.history_calls
        original = adapter.rows[0].text
        adapter.rows[0].text = "Later edit"
        chunks, cursor = [], None
        while True:
            response = data(
                await server.call_tool(
                    "jobs_results",
                    {
                        **base,
                        "view": "messages",
                        "coverage": "compact",
                        "max_output_bytes": 6000,
                        "message_keys": [{"chat_id": "10", "message_id": "1"}],
                        "original_field": "text",
                        "content_cursor": cursor,
                    },
                )
            )
            assert response["ok"], response
            chunk = response["data"]
            assert normalized(chunk) <= 6000 and chunk["warnings"]
            assert chunk["coverage"] == compact["coverage"]
            assert chunk["source_version"] == full["source_version"]
            chunks.append(chunk["content"]["value"])
            cursor = chunk["next_content_cursor"]
            if cursor is None:
                break
        assert json.loads("".join(chunks)) == original
        assert adapter.history_calls == calls and adapter.ack == adapter.sent == []
        print(
            f"verbose_gap: full={normalized(full)}, compact={normalized(compact)}, chunks={len(chunks)}, cap=6000"
        )
