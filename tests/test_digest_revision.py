"""Caller-owned digest grounding through frozen MCP export and offline CLI."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teleloom.cli import app
from teleloom.config import Profile, Settings
from teleloom.models import Message
from tests.fakes import TelegramAPI, data
from tests.test_jobs import complete
from tests.test_transport import client, running

NOW = datetime(2025, 7, 1, tzinfo=UTC)
FIXTURE = Path(__file__).resolve().parents[1] / "skills" / "teleloom-digest" / "references"


class DigestAPI(TelegramAPI):
    instances = []

    def __init__(self, *args):
        super().__init__(*args)
        self.history_calls = 0
        self.rows = [
            Message(
                profile_id=self.profile,
                chat_id=chat,
                id=id,
                date=NOW - timedelta(hours=hour),
                text=text,
            )
            for chat, id, hour, text in (
                ("100", "1", 5, "Decision: ship on Friday."),
                ("100", "2", 2, "Cancel Friday launch. Wait for QA approval."),
                ("200", "1", 4, "QA: Friday is unsafe; the blocker is unresolved."),
                ("200", "2", 1, "Who owns the blocker? No owner confirmed."),
            )
        ]
        self.rows.append(
            Message(
                profile_id=self.profile,
                chat_id="100",
                id="3",
                date=NOW - timedelta(minutes=30),
                text="Readable launch notes",
                text_source="reconstructed",
                original_text="Launch\nnotes  ",
            )
        )
        self.instances.append(self)

    async def history(self, chat, **kwargs):
        self.history_calls += 1
        return await super().history(chat, **kwargs)


def revision_for(result):
    revision = json.loads((FIXTURE / "fixture-revision.json").read_text(encoding="utf-8"))
    revision["source_manifest"] = result["manifest"]
    revision["export_sha256"] = result["sha256"]
    for claim in revision["claims"]:
        for citation in claim["supporting"] + claim["contradicting"]:
            citation["source_version"] = result["manifest"]["source_version"]
    return revision


@pytest.mark.asyncio
async def test_digest_cli_traces_decision_cancellation_conflict_and_exact_originals(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    monkeypatch.setattr("teleloom.runtime.utcnow", lambda: NOW)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, DigestAPI) as application, client(application, settings) as server:
        started = data(
            await server.call_tool(
                "digest_context_many_start",
                {
                    "profile_id": "personal",
                    "chat_ids": ["100", "200", "999"],
                    "since": (NOW - timedelta(days=1)).isoformat(),
                    "until": NOW.isoformat(),
                },
            )
        )["data"]
        await complete(server, "personal", started["job_id"])
        page = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": started["job_id"],
                    "coverage": "compact",
                },
            )
        )["data"]
        export = data(
            await server.call_tool(
                "export_start",
                {
                    "profile_id": "personal",
                    "source": {
                        "kind": "frozen_evidence",
                        "job_id": started["job_id"],
                        "evidence_ref": page["evidence_ref"],
                    },
                },
            )
        )["data"]
        exported = (await complete(server, "personal", export["job_id"]))["result"]
        copied = tmp_path / "copied-evidence.jsonl"
        copied.write_bytes(Path(exported["path"]).read_bytes())
        revision = revision_for(exported)
        manifest = tmp_path / "revision.json"
        manifest.write_text(json.dumps(revision, ensure_ascii=False, indent=2), encoding="utf-8")
        api = DigestAPI.instances[-1]
        calls = api.history_calls
        # Copied files are historical; offline validation cannot check a current ACL.
        settings.profiles["personal"].read_mode = "selected"
        settings.profiles["personal"].read_chats = []
        monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path / "unused-owner"))
        result = CliRunner().invoke(
            app,
            [
                "digest-validate",
                str(manifest),
                "--export",
                str(copied),
                "--sha256",
                exported["sha256"],
            ],
        )
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report["validation"] == "schema_tracing_quotes"
        assert report["semantic_quality"] == "NOT MEASURED"
        assert report["current_access"] == "NOT CHECKED"
        assert report["source_version"] == page["source_version"]
        assert report["source_manifest"] == exported["manifest"]
        assert report["claims"] == revision["claims"]
        assert report["final_claim_ids"] == ["launch", "cancel", "blocker", "owner", "notes"]
        assert report["messages"] == 5
        assert report["max_chunk_messages"] == 2
        assert report["max_reduce_fan_in"] == 2
        assert api.history_calls == calls and api.sent == [] and api.ack == []
        assert not (tmp_path / "unused-owner").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "quote",
        "reconstruction_quote",
        "colliding_chat",
        "profile",
        "version",
        "coverage",
        "missing_claim",
        "missing_chunk",
        "unclaimed_original",
        "duplicate_original",
        "chunk_messages",
        "chunk_bytes",
        "reduce_bytes",
        "reduce_claims",
        "fan_in",
        "model_without_policy",
        "latest",
        "unknown_field",
        "unknown_retraction",
        "file_hash",
    ],
)
def test_offline_digest_rejects_broken_grounding_or_recipe(tmp_path, damage):
    revision = json.loads((FIXTURE / "fixture-revision.json").read_text(encoding="utf-8"))
    if damage == "quote":
        revision["claims"][0]["supporting"][0]["text"] = "Decision: ship on Saturday."
    elif damage == "reconstruction_quote":
        revision["claims"][4]["supporting"][0]["rendering"] = "verbatim"
    elif damage == "colliding_chat":
        revision["claims"][0]["supporting"][0]["chat_id"] = "200"
    elif damage == "profile":
        revision["claims"][0]["supporting"][0]["profile_id"] = "work"
    elif damage == "version":
        revision["claims"][0]["contradicting"][0]["source_version"] = "0" * 64
    elif damage == "coverage":
        revision["source_manifest"]["incomplete"] = False
        revision["source_manifest"]["unavailable"] = []
    elif damage == "missing_claim":
        revision["reductions"][0]["claim_ids"].remove("cancel")
    elif damage == "missing_chunk":
        revision["reductions"].pop()
    elif damage == "unclaimed_original":
        revision["claims"].pop()
        revision["chunks"].pop()
        revision["reductions"].pop()
    elif damage == "duplicate_original":
        revision["chunks"][0]["message_keys"].append({"chat_id": "100", "message_id": "3"})
        revision["budgets"]["max_chunk_messages"] = 3
    elif damage == "chunk_messages":
        revision["budgets"]["max_chunk_messages"] = 1
    elif damage == "chunk_bytes":
        revision["budgets"]["max_chunk_bytes"] = 1
    elif damage == "reduce_bytes":
        revision["budgets"]["max_reduce_bytes"] = 1
    elif damage == "reduce_claims":
        revision["budgets"]["max_reduce_claims"] = 3
    elif damage == "fan_in":
        revision["reductions"] = [
            {
                "id": "final",
                "inputs": ["c1", "c2", "c3"],
                "claim_ids": [claim["id"] for claim in revision["claims"]],
            }
        ]
    elif damage == "model_without_policy":
        revision["model_run"] = {
            "provider": "fake",
            "model": "fake",
            "prompt_sha256": "0" * 64,
            "budget": "caller budget",
            "attribution": "caller run",
        }
    elif damage == "latest":
        revision["mode"] = "latest"
    elif damage == "unknown_field":
        revision["claims"][0]["verbatim"] = True
    elif damage == "unknown_retraction":
        revision["claims"][1]["retracts"] = ["not-registered"]
    elif damage == "file_hash":
        revision["export_sha256"] = "0" * 64
    manifest = tmp_path / "revision.json"
    manifest.write_text(json.dumps(revision), encoding="utf-8")
    pinned = json.loads((FIXTURE / "fixture-revision.json").read_text())["export_sha256"]
    result = CliRunner().invoke(
        app,
        [
            "digest-validate",
            str(manifest),
            "--export",
            str(FIXTURE / "fixture-export.jsonl"),
            "--sha256",
            pinned,
        ],
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.stderr)["error"]["code"] == "invalid_digest"
    assert "Decision: ship" not in result.output


def test_installed_digest_fixture_validates_offline_without_owner_or_upload(tmp_path, monkeypatch):
    from teleloom.secrets import Secrets

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline validation must not access credentials or the owner")

    monkeypatch.setattr(Secrets, "get", forbidden)
    monkeypatch.setattr(Settings, "load", forbidden)
    runner = CliRunner()
    installed = runner.invoke(app, ["skills", "install", "--target", str(tmp_path)])
    assert installed.exit_code == 0, installed.output
    fixture = tmp_path / "teleloom-digest" / "references"
    revision = json.loads((fixture / "fixture-revision.json").read_text(encoding="utf-8"))
    result = runner.invoke(
        app,
        [
            "digest-validate",
            str(fixture / "fixture-revision.json"),
            "--export",
            str(fixture / "fixture-export.jsonl"),
            "--sha256",
            revision["export_sha256"],
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["source_manifest"]["incomplete"] is True
    assert report["source_manifest"]["unavailable"][0]["chat_id"] == "999"
    assert report["source_manifest"]["coverage"]["observed_rpc_requests"] is None
    assert report["claims"][1]["retracts"] == ["launch"]
    assert report["claims"][0]["contradicting"][1]["chat_id"] == "200"
    assert report["claims"][3]["uncertainty"] == "Owner unknown."
    assert report["semantic_quality"] == "NOT MEASURED"


@pytest.mark.parametrize(
    "damage", ["tampered_file", "duplicate_json_key", "malformed_json", "invalid_original_id"]
)
def test_digest_cli_rejects_damaged_local_inputs_without_echoing_content(tmp_path, damage):
    original = FIXTURE / "fixture-export.jsonl"
    revision = json.loads((FIXTURE / "fixture-revision.json").read_text(encoding="utf-8"))
    manifest = tmp_path / "revision.json"
    export = tmp_path / "evidence.jsonl"
    content = original.read_bytes()
    pinned = revision["export_sha256"]
    if damage == "tampered_file":
        content = content.replace(b"ship on Friday", b"ship on Sunday")
    elif damage == "duplicate_json_key":
        # Both values equal: duplicate schema keys are still ambiguous input.
        manifest.write_text(
            json.dumps(revision).replace('"mode":', '"mode":"historical_as_of","mode":'),
            encoding="utf-8",
        )
    elif damage == "malformed_json":
        content = b"malformed evidence"
        pinned = hashlib.sha256(content).hexdigest()
        revision["export_sha256"] = pinned
    elif damage == "invalid_original_id":
        lines = [json.loads(line) for line in content.splitlines()]
        lines[1]["id"] = "01"
        content = ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
        pinned = hashlib.sha256(content).hexdigest()
        revision["export_sha256"] = pinned
    if not manifest.exists():
        manifest.write_text(json.dumps(revision), encoding="utf-8")
    export.write_bytes(content)
    result = CliRunner().invoke(
        app, ["digest-validate", str(manifest), "--export", str(export), "--sha256", pinned]
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.stderr)["error"]["code"] == "invalid_digest"
    assert "Decision: ship" not in result.output


def test_digest_tracing_does_not_claim_semantic_quality(tmp_path):
    revision = json.loads((FIXTURE / "fixture-revision.json").read_text(encoding="utf-8"))
    # The quote/identity still trace, but this synthesis ignores the cancellation.
    revision["claims"][0]["text"] = "Friday launch remains approved."
    manifest = tmp_path / "revision.json"
    manifest.write_text(json.dumps(revision), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "digest-validate",
            str(manifest),
            "--export",
            str(FIXTURE / "fixture-export.jsonl"),
            "--sha256",
            revision["export_sha256"],
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["semantic_quality"] == "NOT MEASURED"


class IncrementalAPI(DigestAPI):
    """External adapter ingress; caller assertions go through MCP and the recipe."""

    def __init__(self, *args):
        super().__init__(*args)
        self.reads = []

    async def history(self, chat, **kwargs):
        self.reads.append((chat, kwargs.get("ids")))
        return await super().history(chat, **kwargs)

    def observe(self, chat, id, kind):
        from teleloom.events import ingest

        row = next(row for row in self.rows if row.chat_id == chat and row.id == id)
        if kind == "edit":
            row.text = "Changed decision: launch on Sunday."
            row.edited_at = NOW
        elif kind == "delete":
            self.rows.remove(row)
        ingest(self.store, self.profile, self.config, row, kind)


async def copied_revision(server, tmp_path):
    from tests.test_events_transcription import call

    started = await call(
        server,
        "digest_context_many_start",
        chat_ids=["100", "200", "999"],
        since=(NOW - timedelta(days=1)).isoformat(),
        until=NOW.isoformat(),
    )
    await complete(server, "personal", started["job_id"])
    page = await call(server, "jobs_results", job_id=started["job_id"])
    export = await call(
        server,
        "export_start",
        source={
            "kind": "frozen_evidence",
            "job_id": started["job_id"],
            "evidence_ref": page["evidence_ref"],
        },
    )
    exported = (await complete(server, "personal", export["job_id"]))["result"]
    copied = tmp_path / "copied.jsonl"
    copied.write_bytes(Path(exported["path"]).read_bytes())
    manifest = tmp_path / "revision.json"
    revision = revision_for(exported)
    manifest.write_text(json.dumps(revision), encoding="utf-8")
    return manifest, copied, exported["sha256"], revision


@pytest.mark.asyncio
async def test_incremental_recipe_invalidates_both_citation_roles_and_reuses_unchanged(
    tmp_path,
    monkeypatch,
):
    from teleloom.digest import refresh_revision
    from tests.test_events_transcription import call

    now = NOW
    for module in ("jobs", "runtime", "events", "digest"):
        monkeypatch.setattr(f"teleloom.{module}.utcnow", lambda: now)
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="user",
                event_chats=["100", "200", "999"],
            )
        },
    )
    async with (
        running(settings, IncrementalAPI) as application,
        client(application, settings) as server,
    ):
        manifest, copied, pinned, revision = await copied_revision(server, tmp_path)
        original_bytes = manifest.read_bytes(), copied.read_bytes()
        call_tool = server.call_tool
        tools_called = []

        async def recorded_call(name, arguments):
            tools_called.append(name)
            return await call_tool(name, arguments)

        monkeypatch.setattr(server, "call_tool", recorded_call)
        keys = [key for chunk in revision["chunks"] for key in chunk["message_keys"]]
        bootstrap = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            after_sequence=0,
            timeout_seconds=0.05,
        )
        now += timedelta(seconds=1)
        await complete(server, "personal", bootstrap["job_id"])
        first = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            {},
            event_job_id=bootstrap["job_id"],
            reconcile_keys=keys,
        )
        assert first["checkpoint"]["event_epoch"] is not None
        assert first["checkpoint"]["positions"] == {"100": 0, "200": 0, "999": 0}
        assert first["reusable_claim_ids"] == ["launch", "cancel", "blocker", "owner", "notes"]
        assert first["current_access"] == "CHECKED_THIS_INVOCATION"
        api = IncrementalAPI.instances[-1]
        api.observe("100", "1", "edit")
        api.observe("200", "2", "delete")
        delta = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            cursor=first["checkpoint"]["event_cursor"],
            max_events=100,
        )
        await complete(server, "personal", delta["job_id"])
        before = len(api.reads)
        changed = await refresh_revision(
            server, manifest, copied, pinned, first["checkpoint"], event_job_id=delta["job_id"]
        )
        assert changed["invalidated_claim_ids"] == ["launch", "blocker", "owner"]
        assert changed["reusable_claim_ids"] == ["cancel", "notes"]
        assert changed["processed_events"] == 2
        assert changed["telegram_completeness"] == "UNKNOWN"
        assert changed["claims"] == revision["claims"]
        frozen = await call(
            server,
            "jobs_results",
            job_id=revision["source_manifest"]["source"]["job_id"],
            evidence_ref=revision["source_manifest"]["source"]["evidence_ref"],
            message_keys=[
                {"chat_id": "100", "message_id": "1"},
                {"chat_id": "200", "message_id": "2"},
            ],
        )
        assert [row["text"] for row in frozen["items"]] == [
            "Decision: ship on Friday.",
            "Who owns the blocker? No owner confirmed.",
        ]
        assert frozen["source_version"] == changed["source_version"]
        assert len(api.reads) == before  # no full or exact-source reread for unchanged facts
        assert original_bytes == (manifest.read_bytes(), copied.read_bytes())
        assert set(tools_called) == {
            "jobs_results",
            "jobs_status",
            "messages_get",
            "events_wait_start",
        }
        saved = json.loads(json.dumps(changed["checkpoint"]))
    async with (
        running(settings, IncrementalAPI) as application,
        client(application, settings) as server,
    ):
        replay = await refresh_revision(
            server, manifest, copied, pinned, saved, event_job_id=delta["job_id"]
        )
        assert replay["processed_events"] == 0 and replay["checkpoint"] == saved
        assert replay["invalidated_claim_ids"] == changed["invalidated_claim_ids"]
        assert replay["reusable_claim_ids"] == []  # restart needs fresh delta/reconciliation
        fresh = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            cursor=saved["event_cursor"],
            timeout_seconds=0.05,
        )
        # Explicit clock advance finishes a bounded empty delta in the restarted owner.
        now += timedelta(seconds=1)
        await complete(server, "personal", fresh["job_id"])
        gap = await refresh_revision(
            server, manifest, copied, pinned, saved, event_job_id=fresh["job_id"]
        )
        assert gap["processed_events"] == 0 and gap["reusable_claim_ids"] == []
        assert gap["checkpoint"]["unresolved_gap"] and gap["checkpoint"]["event_coverage"]["gap"]
        reconciled = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            gap["checkpoint"],
            reconcile_keys=[{"chat_id": "100", "message_id": "3"}],
        )
        assert reconciled["reusable_claim_ids"] == ["notes"]
        assert reconciled["invalidated_claim_ids"] == ["launch", "blocker", "owner"]
        assert IncrementalAPI.instances[-1].reads == [("100", [3])]
        following = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            cursor=reconciled["checkpoint"]["event_cursor"],
            timeout_seconds=0.05,
        )
        now += timedelta(seconds=1)
        await complete(server, "personal", following["job_id"])
        unchanged = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            reconciled["checkpoint"],
            event_job_id=following["job_id"],
        )
        assert unchanged["reusable_claim_ids"] == ["notes"]
        assert IncrementalAPI.instances[-1].reads == [("100", [3])]
        assert all(api.sent == [] and api.ack == [] for api in IncrementalAPI.instances)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "generation", "expiry", "scope", "period", "offline"])
async def test_incremental_reuse_requires_actual_current_access_and_fresh_evidence(
    tmp_path,
    monkeypatch,
    change,
):
    from teleloom.digest import refresh_revision

    now = NOW
    for module in ("jobs", "runtime", "events", "digest"):
        monkeypatch.setattr(f"teleloom.{module}.utcnow", lambda: now)
    profile = Profile(kind="user", event_chats=["100", "200", "999"])
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with (
        running(settings, IncrementalAPI) as application,
        client(application, settings) as server,
    ):
        manifest, copied, pinned, revision = await copied_revision(server, tmp_path)
        keys = [key for chunk in revision["chunks"] for key in chunk["message_keys"]]
        first = await refresh_revision(server, manifest, copied, pinned, {}, reconcile_keys=keys)
        assert len(first["reusable_claim_ids"]) == 5
        kwargs = {}
        if change == "revoke":
            profile.read_mode, profile.read_chats = "selected", ["100"]
        elif change == "generation":
            profile.generation = "replacement-generation"
        elif change == "expiry":
            now += timedelta(minutes=31)
        elif change in {"scope", "period"}:
            kwargs["scope"] = json.loads(json.dumps(first["scope"]))
            if change == "scope":
                kwargs["scope"]["chat_ids"] = ["100"]
            else:
                kwargs["scope"]["period"]["until"] = (NOW + timedelta(days=1)).isoformat()
        api = IncrementalAPI.instances[-1]
        before = len(api.reads)
        stale = await refresh_revision(
            None if change == "offline" else server,
            manifest,
            copied,
            pinned,
            first["checkpoint"],
            **kwargs,
        )
        assert stale["reusable_claim_ids"] == []
        assert stale["current_access"] == "NOT CHECKED"
        assert len(api.reads) == before
        if change != "offline":
            expected = {
                "revoke": "read_not_allowed",
                "generation": "account_changed",
                "expiry": "reference_expired",
                "scope": "scope_or_period_changed",
                "period": "scope_or_period_changed",
            }[change]
            assert stale["blocked_reason"] == expected
        # Restoring access or changing a caller string cannot resurrect the old revision.
        profile.read_mode, profile.read_chats = "all", []
        profile.generation = revision["source_manifest"]["profile_generation"]
        now = NOW
        if change != "offline":
            restored = await refresh_revision(
                server, manifest, copied, pinned, stale["checkpoint"], reconcile_keys=keys
            )
            assert restored["reusable_claim_ids"] == [] and restored["mcp_calls"] == 0
        historical = CliRunner().invoke(
            app, ["digest-validate", str(manifest), "--export", str(copied), "--sha256", pinned]
        )
        assert historical.exit_code == 0, historical.output
        assert json.loads(historical.stdout)["current_access"] == "NOT CHECKED"
        if change == "revoke":
            fresh_dir = tmp_path / "new-authorized-evidence"
            fresh_dir.mkdir()
            fresh_manifest, fresh_copy, fresh_hash, fresh_revision = await copied_revision(
                server, fresh_dir
            )
            refreshed = await refresh_revision(
                server, fresh_manifest, fresh_copy, fresh_hash, {}, reconcile_keys=keys
            )
            assert len(refreshed["reusable_claim_ids"]) == 5
            assert refreshed["source_version"] != first["source_version"]
        assert api.sent == [] and api.ack == []


@pytest.mark.asyncio
async def test_incremental_gap_needs_explicit_bounded_reconciliation_and_preserves_unknowns(
    tmp_path,
    monkeypatch,
):
    from teleloom.digest import refresh_revision
    from tests.test_events_transcription import call

    now = NOW
    for module in ("jobs", "runtime", "events", "digest"):
        monkeypatch.setattr(f"teleloom.{module}.utcnow", lambda: now)
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="user",
                event_chats=["100", "200", "999"],
                event_retention_hours=1,
            )
        },
    )
    async with (
        running(settings, IncrementalAPI) as application,
        client(application, settings) as server,
    ):
        manifest, copied, pinned, revision = await copied_revision(server, tmp_path)
        keys = [key for chunk in revision["chunks"] for key in chunk["message_keys"]]
        first = await refresh_revision(server, manifest, copied, pinned, {}, reconcile_keys=keys)
        api = IncrementalAPI.instances[-1]
        api.observe("100", "1", "edit")
        # Expire the edit in the observed journal, without expiring the 30-minute evidence ref.
        # Retention pruning also has a 1000-event ceiling: real ingress exceeds that ceiling.
        from teleloom.events import ingest

        for i in range(1001):
            ingest(
                api.store,
                "personal",
                settings.profile("personal"),
                Message(
                    profile_id="personal",
                    chat_id="100",
                    id=str(i + 100),
                    date=NOW,
                    text="Observed outside the digest period",
                ),
                "new",
            )
        delta = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            after_sequence=0,
            max_events=1,
        )
        await complete(server, "personal", delta["job_id"])
        before = len(api.reads)
        gap = await refresh_revision(
            server, manifest, copied, pinned, first["checkpoint"], event_job_id=delta["job_id"]
        )
        assert gap["reusable_claim_ids"] == [] and gap["checkpoint"]["unresolved_gap"]
        assert len(api.reads) == before  # no automatic history recovery
        assert "sender_id" in gap["checkpoint"]["unknown_facts"]
        assert gap["checkpoint"]["event_coverage"]["gap"] is True
        assert gap["processed_events"] == 1
        reconciled = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            gap["checkpoint"],
            reconcile_keys=[
                {"chat_id": "100", "message_id": "3"},
                {"chat_id": "100", "message_id": "1"},
            ],
        )
        assert reconciled["reusable_claim_ids"] == ["notes"]
        assert reconciled["invalidated_claim_ids"] == ["launch", "blocker"]
        assert api.reads[before:] == [("100", [3, 1])]
        assert reconciled["checkpoint"]["unresolved_gap"]
        assert reconciled["checkpoint"]["unknown_facts"] == gap["checkpoint"]["unknown_facts"]
        assert (
            reconciled["telegram_completeness"] == "UNKNOWN" and not reconciled["latest_complete"]
        )
        assert reconciled["checkpoint"]["reconciliation"]["keys"] == ["100/3", "100/1"]
        assert reconciled["source_manifest"]["unavailable"][0]["chat_id"] == "999"
        # A repeated job never resets newly reconciled sources or duplicates its delta.
        replay = await refresh_revision(
            server, manifest, copied, pinned, reconciled["checkpoint"], event_job_id=delta["job_id"]
        )
        assert replay["processed_events"] == 0 and replay["checkpoint"] == reconciled["checkpoint"]
        assert api.sent == [] and api.ack == []


@pytest.mark.asyncio
async def test_incremental_missing_sources_filters_and_bounds_never_claim_latest(
    tmp_path,
    monkeypatch,
):
    from teleloom.digest import refresh_revision
    from teleloom.models import TeleloomError
    from tests.test_events_transcription import call

    now = NOW
    for module in ("jobs", "runtime", "events", "digest"):
        monkeypatch.setattr(f"teleloom.{module}.utcnow", lambda: now)
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="user",
                event_chats=["100", "200", "999"],
            )
        },
    )
    async with (
        running(settings, IncrementalAPI) as application,
        client(application, settings) as server,
    ):
        manifest, copied, pinned, revision = await copied_revision(server, tmp_path)
        keys = [key for chunk in revision["chunks"] for key in chunk["message_keys"]]
        first = await refresh_revision(server, manifest, copied, pinned, {}, reconcile_keys=keys)
        without_delta = await refresh_revision(
            server, manifest, copied, pinned, first["checkpoint"]
        )
        assert without_delta["current_access"] == "CHECKED_THIS_INVOCATION"
        assert without_delta["reusable_claim_ids"] == []
        api = IncrementalAPI.instances[-1]
        api.rows = [row for row in api.rows if (row.chat_id, row.id) != ("200", "1")]
        missing = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            first["checkpoint"],
            reconcile_keys=[{"chat_id": "200", "message_id": "1"}],
        )
        assert missing["reusable_claim_ids"] == [] and missing["invalidated_claim_ids"] == []
        assert (
            missing["checkpoint"]["sources"]["200/1"]["reason"]
            == "current_source_missing_or_incomplete"
        )
        assert missing["checkpoint"]["reconciliation"]["results"][0]["coverage"][
            "missing_message_ids"
        ] == ["1"]
        delta = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            filter={"kinds": ["edit"]},
            timeout_seconds=0.05,
        )
        now += timedelta(seconds=1)
        await complete(server, "personal", delta["job_id"])
        filtered = await refresh_revision(
            server, manifest, copied, pinned, first["checkpoint"], event_job_id=delta["job_id"]
        )
        assert filtered["error"] == "invalid_digest" and filtered["reusable_claim_ids"] == []
        assert filtered["checkpoint"]["unresolved_gap"]
        skipped = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            after_sequence=1000,
            timeout_seconds=0.05,
        )
        now += timedelta(seconds=1)
        await complete(server, "personal", skipped["job_id"])
        discontinuous = await refresh_revision(
            server, manifest, copied, pinned, first["checkpoint"], event_job_id=skipped["job_id"]
        )
        assert discontinuous["reusable_claim_ids"] == []
        assert "journal_discontinuity" in discontinuous["checkpoint"]["unknown_facts"]
        before = len(api.reads)
        with pytest.raises(TeleloomError, match="at most 20"):
            await refresh_revision(server, manifest, copied, pinned, {}, reconcile_keys=keys * 5)
        with pytest.raises(TeleloomError, match="distinct originals"):
            await refresh_revision(
                server, manifest, copied, pinned, {}, reconcile_keys=[keys[0], keys[0]]
            )
        assert len(api.reads) == before

        async def disconnected(*args, **kwargs):
            raise RuntimeError("Private transport detail must not enter the report")

        monkeypatch.setattr(server, "call_tool", disconnected)
        unknown = await refresh_revision(server, manifest, copied, pinned, first["checkpoint"])
        assert unknown["error"] == "mcp_unavailable" and unknown["reusable_claim_ids"] == []
        assert unknown["current_access"] == "NOT CHECKED"
        assert "Private transport" not in json.dumps(unknown)
        assert api.sent == [] and api.ack == []


@pytest.mark.asyncio
@pytest.mark.parametrize("anchored", [False, True])
async def test_incremental_restart_requires_current_journal_anchor_before_reuse(
    tmp_path,
    monkeypatch,
    anchored,
):
    from teleloom.digest import refresh_revision
    from tests.test_events_transcription import call

    now = NOW
    for module in ("jobs", "runtime", "events", "digest"):
        monkeypatch.setattr(f"teleloom.{module}.utcnow", lambda: now)
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "personal": Profile(
                kind="user",
                event_chats=["100", "200", "999"],
            )
        },
    )
    async with (
        running(settings, IncrementalAPI) as application,
        client(application, settings) as server,
    ):
        manifest, copied, pinned, revision = await copied_revision(server, tmp_path)
        keys = [key for chunk in revision["chunks"] for key in chunk["message_keys"]]
        bootstrap = None
        if anchored:
            bootstrap = await call(
                server,
                "events_wait_start",
                chat_ids=["100", "200", "999"],
                after_sequence=0,
                timeout_seconds=0.05,
            )
            now += timedelta(seconds=1)
            await complete(server, "personal", bootstrap["job_id"])
        first = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            {},
            reconcile_keys=keys,
            event_job_id=bootstrap["job_id"] if bootstrap else None,
        )
        old_delta = None
        if anchored:
            old_delta = await call(
                server,
                "events_wait_start",
                chat_ids=["100", "200", "999"],
                cursor=first["checkpoint"]["event_cursor"],
                timeout_seconds=0.05,
            )
            now += timedelta(seconds=1)
            await complete(server, "personal", old_delta["job_id"])
        assert len(first["reusable_claim_ids"]) == 5
        saved = json.loads(json.dumps(first["checkpoint"]))
        if not anchored:
            assert saved["event_epoch"] is None and saved["positions"] == {}
    async with (
        running(settings, IncrementalAPI) as application,
        client(application, settings) as server,
    ):
        api = IncrementalAPI.instances[-1]
        next(
            row for row in api.rows if (row.chat_id, row.id) == ("100", "3")
        ).text = "Unobserved changed notes during downtime"
        delta = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            after_sequence=0,
            timeout_seconds=0.05,
        )
        now += timedelta(seconds=1)
        await complete(server, "personal", delta["job_id"])
        refreshed = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            saved,
            event_job_id=old_delta["job_id"] if old_delta else delta["job_id"],
        )
        assert refreshed["reusable_claim_ids"] == []
        assert refreshed["current_access"] == "CHECKED_THIS_INVOCATION"
        assert refreshed["checkpoint"]["unresolved_gap"]
        assert "journal_discontinuity" in refreshed["checkpoint"]["unknown_facts"]
        assert refreshed["processed_events"] == 0 and api.reads == []
        assert refreshed["claims"] == revision["claims"]
        reconciled = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            refreshed["checkpoint"],
            event_job_id=delta["job_id"],
            reconcile_keys=[
                {"chat_id": "100", "message_id": "2"},
                {"chat_id": "100", "message_id": "3"},
            ],
        )
        assert reconciled["reusable_claim_ids"] == ["cancel"]
        assert reconciled["invalidated_claim_ids"] == ["notes"]
        assert api.reads == [("100", [2, 3])]
        # The new anchor plus bounded reconciliation permits subsequent unchanged reuse.
        continuation = await call(
            server,
            "events_wait_start",
            chat_ids=["100", "200", "999"],
            cursor=reconciled["checkpoint"]["event_cursor"],
            timeout_seconds=0.05,
        )
        now += timedelta(seconds=1)
        await complete(server, "personal", continuation["job_id"])
        unchanged = await refresh_revision(
            server,
            manifest,
            copied,
            pinned,
            reconciled["checkpoint"],
            event_job_id=continuation["job_id"],
        )
        assert unchanged["reusable_claim_ids"] == ["cancel"]
        assert unchanged["invalidated_claim_ids"] == ["notes"]
        assert api.reads == [("100", [2, 3])]  # anchored continuation performs no reread
        assert api.sent == [] and api.ack == []
