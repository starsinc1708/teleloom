"""T11: bounded public content with exact frozen originals."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from teleloom.config import Profile, Settings
from tests.fakes import TelegramAPI, data
from tests.test_jobs import complete
from tests.test_transport import client, running


class OversizedAPI(TelegramAPI):
    instances = []

    def __init__(self, *args):
        super().__init__(*args)
        self.rows = self.rows[:1]
        row = self.rows[0]
        row.date = datetime(2026, 10, 1, tzinfo=UTC)
        row.text = 'Начало 😀 "\\\n' * 4000
        row.entities = [{"type": "bold", "offset": 7, "length": 2}]
        row.rich_text = {
            "blocks": [
                {
                    "_": "PageBlockParagraph",
                    "text": row.text,
                    "document_id": "1234567890123456789",
                    "entities": [{"type": "bold", "offset": 7, "length": 2}],
                }
            ]
            * 3,
            "reconstructed_text": row.text,
            "partial": False,
            "warnings": [],
        }
        row.transcript = {
            "text": "Речь 🧑‍🚀 " * 4000,
            "provider": "local",
            "model": "fictional",
            "source_version": "audio-v1",
            "untrusted": True,
        }
        self.history_calls = 0
        self.instances.append(self)

    async def history(self, *args, **kwargs):
        self.history_calls += 1
        return await super().history(*args, **kwargs)


def normalized_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@pytest.mark.asyncio
async def test_direct_unicode_rich_transcript_budget_and_exact_continuation(tmp_path, monkeypatch):
    wire_sizes = []

    class MeasuringTransport(httpx.ASGITransport):
        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            body = json.loads(request.content) if request.content else {}
            if body.get("params", {}).get("arguments", {}).get("max_output_bytes") == 6000:
                wire_sizes.append(len(await response.aread()))
            return response

    monkeypatch.setattr(httpx, "ASGITransport", MeasuringTransport)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {"profile_id": "personal", "chat_id": "100", "limit": 1}
    async with running(settings, OversizedAPI) as app, client(app, settings) as server:
        original = data(await server.call_tool("messages_get", args))["data"]
        response = await server.call_tool("messages_get", {**args, "max_output_bytes": 6000})
        shown = data(response)["data"]
        assert normalized_bytes(shown) <= 6000
        assert shown["output_budget"]["normalized_data_bytes"] == normalized_bytes(shown)
        assert response.structuredContent == json.loads(response.content[0].text)
        print(
            "Output byte measurements:",
            {
                "normalized_data_utf8": normalized_bytes(shown),
                "text_json_utf8": len(response.content[0].text.encode("utf-8")),
                "structured_result_utf8": normalized_bytes(response.structuredContent),
                "http_json_body_utf8": wire_sizes[-1],
            },
        )
        assert shown["coverage"] == original["coverage"]
        assert shown["warnings"] == original["warnings"]
        row = shown["items"][0]
        assert row["excerpt"]["truncated"] is True
        assert {"text", "rich_text", "transcript"} <= set(row["excerpt"]["fields"])
        assert original["items"][0]["text"].startswith(row["text"])
        assert row["entities"] == []
        assert "UTF-16" in row["excerpt"]["entities_notice"]
        assert all(block.get("entities") == [] for block in row["rich_text"]["blocks"])
        assert row["excerpt"]["evidence_ref"] == shown["evidence_ref"]
        assert row["excerpt"]["source_version"] == shown["source_version"]

        adapter = OversizedAPI.instances[-1]
        calls = adapter.history_calls
        adapter.rows[0].text = "Edited afterwards"
        chunks, cursor = [], None
        while True:
            result = data(
                await server.call_tool(
                    "jobs_results",
                    {
                        "profile_id": "personal",
                        "evidence_ref": shown["evidence_ref"],
                        "message_keys": [{"chat_id": "100", "message_id": "1"}],
                        "original_field": "record",
                        "fields": ["text"],
                        "content_cursor": cursor,
                        "max_output_bytes": 20000,
                    },
                )
            )
            assert result["ok"], result
            chunk = result["data"]
            assert normalized_bytes(chunk) <= 20000
            chunks.append(chunk["content"]["value"])
            cursor = chunk["next_content_cursor"]
            if cursor is None:
                break
        assert json.loads("".join(chunks)) == original["items"][0]
        assert adapter.history_calls == calls
        invalid = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "evidence_ref": shown["evidence_ref"],
                    "view": "aggregate",
                },
            )
        )
        assert invalid["error"]["code"] == "invalid_aggregate"


@pytest.mark.asyncio
async def test_budgeted_evidence_reuses_the_same_originals_for_counts_and_export(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, OversizedAPI) as app, client(app, settings) as server:
        job = data(
            await server.call_tool(
                "digest_context_many_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                    "since": "2026-10-01T00:00:00Z",
                    "until": "2026-10-02T00:00:00Z",
                    "max_characters": 1000000,
                },
            )
        )["data"]["job_id"]
        await complete(server, "personal", job)
        args = {"profile_id": "personal", "job_id": job}
        page = data(await server.call_tool("jobs_results", {**args, "max_output_bytes": 8000}))[
            "data"
        ]
        assert page["items"][0]["excerpt"]["truncated"]
        assert normalized_bytes(page) <= 8000
        ref = page["evidence_ref"]
        api = OversizedAPI.instances[-1]
        calls = api.history_calls
        counted = data(
            await server.call_tool(
                "jobs_results", {**args, "view": "aggregate", "evidence_ref": ref}
            )
        )["data"]
        assert counted["aggregate"]["observed_count"] == 1
        assert counted["source_version"] == page["source_version"]
        exported = data(
            await server.call_tool(
                "export_start",
                {
                    "profile_id": "personal",
                    "source": {"kind": "frozen_evidence", "job_id": job, "evidence_ref": ref},
                },
            )
        )["data"]["job_id"]
        finished = await complete(server, "personal", exported)
        path = Path(finished["result"]["path"])
        manifest, original = [json.loads(line) for line in path.read_bytes().splitlines()]
        assert manifest["manifest"]["source_version"] == counted["source_version"]
        assert original["text"] == api.rows[0].text
        assert original["rich_text"] == api.rows[0].rich_text
        assert original["transcript"] == api.rows[0].transcript
        assert api.history_calls == calls and api.ack == [] and api.sent == []
        settings.profiles["personal"].read_mode = "selected"
        denied = data(
            await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": exported})
        )
        assert denied["error"]["code"] == "read_not_allowed"
        assert not path.exists()


@pytest.mark.asyncio
async def test_budget_preserves_explicit_projection_defaults_and_errors(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {"profile_id": "personal", "chat_id": "100", "limit": 1}
    async with running(settings, OversizedAPI) as app, client(app, settings) as server:
        original = data(await server.call_tool("digest_context", args))["data"]
        for _ in range(17):  # failed calls must not exhaust the 16-freeze quota
            error = data(await server.call_tool("digest_context", {**args, "max_output_bytes": 1}))
            assert error["error"]["code"] == "output_budget_too_small"
            assert error["error"]["details"]["minimum_bytes"] > 1
            assert error["data"] == {}
        thin = data(
            await server.call_tool(
                "digest_context",
                {
                    **args,
                    "max_output_bytes": error["error"]["details"]["minimum_bytes"] + 200,
                },
            )
        )["data"]
        assert thin["items"][0]["rich_text"]["blocks"]
        assert all(
            block["document_id"] == "1234567890123456789"
            for block in thin["items"][0]["rich_text"]["blocks"]
        )
        explicit = data(
            await server.call_tool(
                "digest_context",
                {
                    **args,
                    "max_output_bytes": 6000,
                    "fields": ["text"],
                },
            )
        )["data"]
        row = explicit["items"][0]
        assert "rich_text" not in row and "transcript" not in row and "entities" not in row
        assert row["excerpt"]["fields"] == ["text"]
        assert (
            explicit["projection"]["fields"]
            == data(
                await server.call_tool(
                    "digest_context",
                    {**args, "fields": ["text"]},
                )
            )["data"]["projection"]["fields"]
        )
        assert data(await server.call_tool("digest_context", args))["data"] == original
        assert "output_budget" not in original and "evidence_ref" not in original
        minimum = data(
            await server.call_tool(
                "digest_context",
                {
                    **args,
                    "fields": [],
                    "max_output_bytes": 2000,
                },
            )
        )["data"]
        assert "text" not in minimum["items"][0]
        assert "evidence_ref" not in minimum  # fitting projected responses need no freeze
        error = data(
            await server.call_tool(
                "messages_get",
                {
                    **args,
                    "profile_id": "missing",
                    "max_output_bytes": 1,
                },
            )
        )
        assert error["error"]["code"] == "profile_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,code",
    [
        ("acl", "read_not_allowed"),
        ("generation", "invalid_reference"),
        ("expiry", "reference_expired"),
        ("foreign", "invalid_reference"),
    ],
)
async def test_direct_reference_checks_current_scope_for_excerpts_and_originals(
    tmp_path,
    monkeypatch,
    change,
    code,
):
    clock = datetime(2026, 10, 7, tzinfo=UTC)
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)
    profile = Profile(kind="user", read_mode="selected", read_chats=["100"])
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": profile, "work": Profile(kind="user")}
    )
    async with running(settings, OversizedAPI) as app, client(app, settings) as server:
        page = data(
            await server.call_tool(
                "messages_get",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "max_output_bytes": 6000,
                },
            )
        )["data"]
    # Resolve through a restarted owner with only temporary SQLite evidence.
    async with running(settings, OversizedAPI) as app, client(app, settings) as server:
        args = {
            "profile_id": "personal",
            "evidence_ref": page["evidence_ref"],
            "message_keys": [{"chat_id": "100", "message_id": "1"}],
        }
        chunk = data(
            await server.call_tool(
                "jobs_results",
                {
                    **args,
                    "original_field": "text",
                    "max_output_bytes": 6000,
                },
            )
        )
        assert chunk["ok"] and chunk["data"]["source_version"] == page["source_version"]
        if change == "acl":
            profile.read_chats = []
        elif change == "generation":
            profile.generation = "replacement"
        elif change == "expiry":
            clock += timedelta(minutes=31)
        else:
            args["profile_id"] = "work"
        for options in (
            {"max_output_bytes": 6000},
            {"original_field": "text"},
            {
                "original_field": "text",
                "max_output_bytes": 6000,
                "content_cursor": chunk["data"]["next_content_cursor"],
            },
        ):
            denied = data(await server.call_tool("jobs_results", {**args, **options}))
            assert denied["error"]["code"] == code, denied


@pytest.mark.asyncio
async def test_job_budget_keeps_owned_originals_and_frozen_coverage(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, OversizedAPI) as app, client(app, settings) as server:
        started = data(
            await server.call_tool(
                "digest_context_many_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100"],
                    "since": "2026-09-30T00:00:00Z",
                    "until": "2026-10-02T00:00:00Z",
                    "max_characters": 1000000,
                },
            )
        )["data"]
        await complete(server, "personal", started["job_id"])
        args = {"profile_id": "personal", "job_id": started["job_id"]}
        page = data(await server.call_tool("jobs_results", {**args, "max_output_bytes": 6000}))[
            "data"
        ]
        assert normalized_bytes(page) <= 6000
        row = page["items"][0]
        assert row["excerpt"]["truncated"]
        version = page["source_version"]
        adapter = OversizedAPI.instances[-1]
        calls = adapter.history_calls
        adapter.rows[0].text = "Live edit"
        original = data(
            await server.call_tool(
                "jobs_results",
                {
                    **args,
                    "evidence_ref": page["evidence_ref"],
                    "message_keys": [{"chat_id": "100", "message_id": "1"}],
                    "original_field": "transcript",
                    "max_output_bytes": 5000,
                },
            )
        )["data"]
        assert original["source_version"] == version
        assert original["coverage"] == page["coverage"]
        assert "Речь" in original["content"]["value"]
        assert adapter.history_calls == calls


@pytest.mark.asyncio
async def test_transcription_job_budget_pins_within_retention_and_never_uploads_on_retrieval(
    tmp_path,
    monkeypatch,
):
    from tests.test_events_transcription import AudioAPI, external_http, external_profile, terminal

    clock = datetime(2026, 10, 7, tzinfo=UTC)
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)
    monkeypatch.setattr("teleloom.transcription.utcnow", lambda: clock)
    monkeypatch.setattr("teleloom.secrets.Secrets.backend", staticmethod(lambda: None))
    monkeypatch.setenv("TELELOOM_PERSONAL_OPENAI_TRANSCRIPTION_KEY", "fictional-key")
    uploads = []
    original_text = "Речь 😀 " * 3000

    async def endpoint(request):
        uploads.append(request.url.path)
        return httpx.Response(200, json={"text": original_text})

    external_http(monkeypatch, endpoint)
    settings = Settings(data_dir=tmp_path, profiles={"personal": external_profile("openai")})
    async with running(settings, AudioAPI) as app, client(app, settings) as server:
        job = data(
            await server.call_tool(
                "transcription_start",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "message_id": "1",
                    "provider": "openai",
                    "allow_external_upload": True,
                    "max_bytes": 8,
                    "retention_hours": 1,
                },
            )
        )["data"]["job_id"]
        assert (await terminal(server, job))["status"] == "completed"
        clock += timedelta(minutes=50)
        page = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "max_output_bytes": 5000,
                },
            )
        )["data"]
        assert normalized_bytes(page) <= 5000
        assert page["items"][0]["excerpt"]["truncated"]
        assert page["reference_expires_at"] == "2026-10-07T01:00:00+00:00"
        direct = data(
            await server.call_tool(
                "messages_get",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "message_ids": ["1"],
                    "max_output_bytes": 5000,
                },
            )
        )["data"]
        assert direct["reference_expires_at"] == "2026-10-07T01:00:00+00:00"
        args = {
            "profile_id": "personal",
            "job_id": job,
            "evidence_ref": page["evidence_ref"],
            "message_keys": [{"chat_id": "100", "message_id": "1"}],
        }
        original = data(await server.call_tool("jobs_results", {**args, "original_field": "text"}))[
            "data"
        ]
        assert json.loads(original["content"]["value"]) == original_text
        assert uploads == ["/v1/audio/transcriptions"]
        settings.profiles["personal"].transcription_chats = []
        denied = data(await server.call_tool("jobs_results", {**args, "max_output_bytes": 5000}))
        assert denied["error"]["code"] == "transcription_not_allowed"
        denied_direct = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "evidence_ref": direct["evidence_ref"],
                    "message_keys": [{"chat_id": "100", "message_id": "1"}],
                    "original_field": "transcript",
                },
            )
        )
        assert denied_direct["error"]["code"] == "transcription_not_allowed"
        settings.profiles["personal"].transcription_chats = ["100"]
        cleaned = data(
            await server.call_tool(
                "attachments_cleanup",
                {
                    "profile_id": "personal",
                    "job_id": job,
                },
            )
        )
        assert cleaned["ok"]
        discarded_direct = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "evidence_ref": direct["evidence_ref"],
                    "message_keys": [{"chat_id": "100", "message_id": "1"}],
                    "original_field": "transcript",
                },
            )
        )
        assert discarded_direct["error"]["code"] == "reference_expired"
        clock += timedelta(minutes=11)
        expired = data(await server.call_tool("jobs_results", {**args, "original_field": "text"}))
        assert expired["error"]["code"] == "reference_expired"
        expired_direct = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "evidence_ref": direct["evidence_ref"],
                    "message_keys": [{"chat_id": "100", "message_id": "1"}],
                    "max_output_bytes": 5000,
                },
            )
        )
        assert expired_direct["error"]["code"] == "reference_expired"
        assert uploads == ["/v1/audio/transcriptions"]


@pytest.mark.asyncio
async def test_direct_freezes_are_bounded_without_silent_eviction(tmp_path, monkeypatch):
    clock = datetime(2026, 10, 7, tzinfo=UTC)
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    args = {"profile_id": "personal", "chat_id": "100", "max_output_bytes": 6000}
    async with running(settings, OversizedAPI) as app, client(app, settings) as server:
        first = data(await server.call_tool("messages_get", args))["data"]
        for _ in range(15):
            assert data(await server.call_tool("messages_get", args))["ok"]
        refused = data(await server.call_tool("messages_get", args))
        assert refused["error"]["code"] == "output_freeze_limit"
        intact = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "evidence_ref": first["evidence_ref"],
                },
            )
        )
        assert intact["ok"] and intact["data"]["source_version"] == first["source_version"]
        clock += timedelta(minutes=31)
        assert data(await server.call_tool("messages_get", args))["ok"]
        adapter = OversizedAPI.instances[-1]
        adapter.rows[0].rich_text["blocks"] *= 8
        oversized = data(await server.call_tool("messages_get", args))
        assert oversized["error"]["code"] == "output_freeze_limit"
        assert oversized["error"]["details"]["max_bytes"] == 2097152


@pytest.mark.asyncio
async def test_output_budget_can_follow_a_default_attachment_page_cursor(tmp_path):
    from tests.test_attachments_v02 import AttachmentAPI
    from tests.test_attachments_v02 import complete as attachments_complete

    class Files(AttachmentAPI):
        payloads = {"1": b"First original", "2": ("Длинный файл 😀 " * 1000).encode("utf-8")}

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, Files) as app, client(app, settings) as server:
        job = data(
            await server.call_tool(
                "attachments_read_start",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "message_ids": ["1", "2"],
                },
            )
        )["data"]["job_id"]
        assert (await attachments_complete(server, "personal", job))["status"] == "completed"
        args = {"profile_id": "personal", "job_id": job, "limit": 1}
        first = data(await server.call_tool("jobs_results", args))["data"]
        assert first["evidence_ref"] is None
        page = data(
            await server.call_tool(
                "jobs_results",
                {
                    **args,
                    "cursor": first["next_cursor"],
                    "max_output_bytes": 5000,
                },
            )
        )["data"]
        assert isinstance(page["evidence_ref"], str)
        assert page["source_version"] == first["source_version"]
        assert normalized_bytes(page) <= 5000
        unsupported = data(
            await server.call_tool(
                "jobs_results",
                {**args, "evidence_ref": page["evidence_ref"], "coverage": "compact"},
            )
        )
        assert unsupported["error"]["code"] == "unsupported_job_results"
        original = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": page["evidence_ref"],
                    "message_keys": [{"chat_id": "100", "message_id": "2"}],
                    "original_field": "text",
                },
            )
        )["data"]
        assert json.loads(original["content"]["value"]) == Files.payloads["2"].decode("utf-8")
        cleaned = data(
            await server.call_tool(
                "attachments_cleanup",
                {
                    "profile_id": "personal",
                    "job_id": job,
                },
            )
        )
        assert cleaned["ok"]
        discarded = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": page["evidence_ref"],
                    "message_keys": [{"chat_id": "100", "message_id": "2"}],
                    "original_field": "text",
                },
            )
        )
        assert discarded["error"]["code"] == "reference_expired"


@pytest.mark.asyncio
async def test_direct_freeze_refuses_two_observed_versions_of_the_same_exact_key(tmp_path):
    class DriftingContext(OversizedAPI):
        def __init__(self, *args):
            super().__init__(*args)
            self.rows[0].text = "Old caption"
            self.rows.append(
                self.rows[0].model_copy(
                    update={
                        "id": "2",
                        "text": "Neighbor",
                        "reply_to_message_id": "1",
                        "rich_text": None,
                        "transcript": None,
                    }
                )
            )

        async def context_window(self, chat, target, size):
            observed = [row.model_copy(deep=True) for row in self.rows]
            self.rows[0].text = "Caption edited during reply lookup"
            return {"items": observed, "has_older": False, "has_newer": False}

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, DriftingContext) as app, client(app, settings) as server:
        response = data(
            await server.call_tool(
                "context_get",
                {
                    "profile_id": "personal",
                    "chat_id": "100",
                    "message_id": "2",
                    "max_output_bytes": 6000,
                },
            )
        )
        assert response["error"]["code"] == "output_freeze_conflict"
        assert response["data"] == {}
