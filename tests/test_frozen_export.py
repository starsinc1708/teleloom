"""T10: collected originals exported through MCP without another history read."""

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from teleloom.config import Profile, Settings
from teleloom.models import Message
from tests.fakes import TelegramAPI, data
from tests.test_jobs import complete
from tests.test_transport import client, running

NOW = datetime(2025, 7, 1, tzinfo=UTC)


class ExportAPI(TelegramAPI):
    instances = []

    def __init__(self, *args):
        super().__init__(*args)
        self.history_calls = 0
        self.rows = [
            Message(
                profile_id=self.profile,
                chat_id="100",
                id="1",
                date=NOW - timedelta(days=1),
                text="Readable reconstruction",
                text_source="reconstructed",
                original_text="Exact caption\nwith whitespace  ",
                rich_text={"blocks": [{"_": "Text", "text": "Original block"}]},
            ),
            Message(
                profile_id=self.profile,
                chat_id="200",
                id="1",
                date=NOW - timedelta(days=2),
                text="Other chat original",
            ),
        ]
        self.instances.append(self)

    async def history(self, chat, **kwargs):
        self.history_calls += 1
        return await super().history(chat, **kwargs)


async def source(server, **extra):
    started = data(
        await server.call_tool(
            "digest_context_many_start",
            {
                "profile_id": "personal",
                "chat_ids": ["100", "200", "999"],
                "since": (NOW - timedelta(days=20)).isoformat(),
                "until": NOW.isoformat(),
                **extra,
            },
        )
    )["data"]
    await complete(server, "personal", started["job_id"])
    page = data(
        await server.call_tool(
            "jobs_results", {"profile_id": "personal", "job_id": started["job_id"]}
        )
    )["data"]
    return {
        "kind": "frozen_evidence",
        "job_id": started["job_id"],
        "evidence_ref": page["evidence_ref"],
    }, page


@pytest.mark.asyncio
async def test_export_preserves_frozen_originals_manifest_and_scope_without_history(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")},
    )
    async with running(settings, ExportAPI) as app, client(app, settings) as server:
        frozen, page = await source(server)
        api = ExportAPI.instances[-1]
        calls = api.history_calls
        api.rows[0].text = "Later live edit"
        api.store.save_messages([api.rows[0]])
        api.store.delete_messages("personal", "200", ["1"])
        started = data(
            await server.call_tool("export_start", {"profile_id": "personal", "source": frozen})
        )
        assert started["ok"], started
        finished = await complete(server, "personal", started["data"]["job_id"])
        assert finished["status"] == "completed", finished
        result = finished["result"]
        output = Path(result["path"])
        assert output.parent == tmp_path / "exports"
        assert not output.with_suffix(output.suffix + ".part").exists()
        content = output.read_bytes()
        assert hashlib.sha256(content).hexdigest() == result["sha256"]
        records = [json.loads(line) for line in content.splitlines()]
        manifest = records[0]["manifest"]
        assert manifest == result["manifest"]
        assert manifest["source_version"] == page["source_version"]
        assert manifest["source_sha256"] == page["source_version"]
        assert manifest["source_revision"] == page["result_snapshot_at"]
        assert manifest["profile_generation"] == settings.profiles["personal"].generation
        assert manifest["period"] == {
            "since": (NOW - timedelta(days=20)).isoformat(),
            "until": NOW.isoformat(),
        }
        assert manifest["coverage"] == page["coverage"]
        assert manifest["incomplete"] is True
        assert manifest["unavailable"][0]["chat_id"] == "999"
        assert [(row["chat_id"], row["id"]) for row in records[1:]] == [("100", "1"), ("200", "1")]
        assert records[1]["text_source"] == "reconstructed"
        assert records[1]["original_text"] == "Exact caption\nwith whitespace  "
        assert records[1]["rich_text"]["blocks"] == [{"_": "Text", "text": "Original block"}]
        assert records[2]["text"] == "Other chat original"
        assert "outside" in result["revocation_boundary"]
        assert api.history_calls == calls
        assert api.ack == [] and api.sent == []
        assert settings.profiles["personal"].sync_chats == []
        assert "Exact caption" not in json.dumps(finished)
        denied = data(
            await server.call_tool("export_start", {"profile_id": "work", "source": frozen})
        )
        assert denied["error"]["code"] == "job_not_found"
        denied_path = data(
            await server.call_tool(
                "jobs_status", {"profile_id": "work", "job_id": started["data"]["job_id"]}
            )
        )
        assert denied_path["error"]["code"] == "job_not_found"


class ArchiveAPI(ExportAPI):
    def __init__(self, *args):
        super().__init__(*args)
        self.rows = [
            Message(
                profile_id=self.profile,
                chat_id="100",
                id=str(i),
                date=NOW - timedelta(hours=1),
                text=f"Frozen archive {i}",
            )
            for i in range(1, 251)
        ]


async def paused_export(server, frozen, format="jsonl"):
    started = data(
        await server.call_tool(
            "export_start", {"profile_id": "personal", "source": frozen, "format": format}
        )
    )["data"]
    job = started["job_id"]
    for _ in range(100):
        status = data(
            await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
        )["data"]
        if status["progress"]:
            break
        await asyncio.sleep(0.01)
    assert status["progress"] == 100 and status["result"] is None, status
    paused = data(
        await server.call_tool(
            "jobs_control", {"profile_id": "personal", "job_id": job, "action": "pause"}
        )
    )
    assert paused["data"]["status"] == "paused"
    return job, status


@pytest.mark.asyncio
async def test_checkpoint_resume_after_restart_truncates_uncommitted_bytes(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ArchiveAPI) as app, client(app, settings) as server:
        frozen, page = await source(server, chat_ids=["100"])
        job, status = await paused_export(server, frozen)
        partial = tmp_path / "exports" / f"{job}.jsonl.part"
        final = partial.with_suffix("")
        assert not final.exists()
        checkpoint = partial.read_bytes()
        assert hashlib.sha256(checkpoint).hexdigest() == status["payload"]["sha256"]
        assert (
            status["payload"]["pin"]["expires_at"]
            <= datetime.fromisoformat(page["reference_expires_at"]).timestamp()
        )
        with partial.open("ab") as stream:
            stream.write(b"Uncommitted crash suffix")
        calls = sum(api.history_calls for api in ExportAPI.instances)

    async with running(settings, ArchiveAPI) as app, client(app, settings) as server:
        paused = data(
            await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
        )["data"]
        assert paused["status"] == "paused" and paused["progress"] == 100
        resumed = data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )
        assert resumed["ok"]
        finished = await complete(server, "personal", job)
        assert finished["status"] == "completed", finished
        content = Path(finished["result"]["path"]).read_bytes()
        records = [json.loads(line) for line in content.splitlines()]
        assert [row["id"] for row in records[1:]] == [str(i) for i in range(250, 0, -1)]
        assert len({(row["chat_id"], row["id"]) for row in records[1:]}) == 250
        assert content.startswith(checkpoint) and b"Uncommitted" not in content
        assert not partial.exists()
        second = data(
            await server.call_tool("export_start", {"profile_id": "personal", "source": frozen})
        )["data"]
        comparison = await complete(server, "personal", second["job_id"])
        assert Path(comparison["result"]["path"]).read_bytes() == content
        assert comparison["result"]["sha256"] == finished["result"]["sha256"]
        assert sum(api.history_calls for api in ExportAPI.instances) == calls


@pytest.mark.asyncio
async def test_completed_file_tampering_denies_path_and_cleans_derivative(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ExportAPI) as app, client(app, settings) as server:
        frozen, _ = await source(server)
        started = data(
            await server.call_tool("export_start", {"profile_id": "personal", "source": frozen})
        )["data"]
        finished = await complete(server, "personal", started["job_id"])
        output = Path(finished["result"]["path"])
        output.write_bytes(b"Changed file")
        denied = data(
            await server.call_tool(
                "jobs_status", {"profile_id": "personal", "job_id": started["job_id"]}
            )
        )
        assert denied["error"]["code"] == "source_changed"
        assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("revoke", ["scope", "policy", "expiry"])
async def test_revocation_reclaims_completed_and_paused_exports_and_preserves_originals(
    tmp_path, monkeypatch, paused, revoke
):
    clock = NOW
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)
    profile = Profile(kind="user")
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, ArchiveAPI) as app, client(app, settings) as server:
        frozen, _ = await source(server)
        if paused:
            job, _ = await paused_export(server, frozen)
            path = tmp_path / "exports" / f"{job}.jsonl.part"
        else:
            job = data(
                await server.call_tool("export_start", {"profile_id": "personal", "source": frozen})
            )["data"]["job_id"]
            finished = await complete(server, "personal", job)
            path = Path(finished["result"]["path"])
        assert path.exists()
        if revoke == "expiry":
            clock += timedelta(minutes=31)
            error = "reference_expired"
        else:
            profile.read_mode = "selected"
            profile.read_chats = ["100"] if revoke == "scope" else ["100", "200", "999"]
            error = "read_not_allowed" if revoke == "scope" else "read_policy_changed"
        # Cleanup also runs for inactive exports, without a request for their path.
        for _ in range(50):
            if not path.exists():
                break
            await asyncio.sleep(0.02)
        assert not path.exists()
        denied = data(
            await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
        )
        assert denied["error"]["code"] == error
        assert not (tmp_path / "exports" / f"{job}.jsonl").exists()
        profile.read_mode, profile.read_chats = "all", []
        original = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": frozen["job_id"]}
            )
        )["data"]
        assert original["items"][0]["text"] == "Frozen archive 250"
        assert original["source_version"]
        if revoke == "expiry":
            expired_start = data(
                await server.call_tool("export_start", {"profile_id": "personal", "source": frozen})
            )
            assert expired_start["error"]["code"] in {"reference_expired", "invalid_reference"}
        else:
            old = data(
                await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
            )
            assert old["error"]["code"] == error  # Restoring policy cannot resurrect old bytes.


@pytest.mark.asyncio
async def test_cancel_and_account_replacement_reclaim_private_export_files(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ArchiveAPI) as app, client(app, settings) as server:
        frozen, _ = await source(server, chat_ids=["100"])
        cancelled, _ = await paused_export(server, frozen)
        partial = tmp_path / "exports" / f"{cancelled}.jsonl.part"
        assert partial.exists()
        result = data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": cancelled, "action": "cancel"}
            )
        )
        assert result["data"]["status"] == "cancelled" and not partial.exists()
        stale, _ = await paused_export(server, frozen)
        partial = tmp_path / "exports" / f"{stale}.jsonl.part"

    replaced = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(replaced, ArchiveAPI) as app, client(app, replaced) as server:
        denied = data(
            await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": stale})
        )
        assert denied["error"]["code"] == "account_changed"
        assert not partial.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["edit", "missing", "hardlink"])
async def test_resume_rejects_damaged_partial_files_without_publication(tmp_path, damage):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ArchiveAPI) as app, client(app, settings) as server:
        frozen, _ = await source(server, chat_ids=["100"])
        job, _ = await paused_export(server, frozen)
        partial = tmp_path / "exports" / f"{job}.jsonl.part"
        content = partial.read_bytes()
        outside = tmp_path / "outside.txt"
        if damage == "edit":
            partial.write_bytes(b"?" + content[1:])
        elif damage == "missing":
            partial.unlink()
        else:
            partial.unlink()
            outside.write_bytes(content)
            os.link(outside, partial)
        await server.call_tool(
            "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
        )
        finished = await complete(server, "personal", job)
        assert finished["status"] == "failed" and finished["result"] is None
        assert finished["error"]["code"] == (
            "unsafe_file_path" if damage == "hardlink" else "source_changed"
        )
        assert not partial.exists() and not partial.with_suffix("").exists()
        if damage == "hardlink":
            assert outside.read_bytes() == content


@pytest.mark.asyncio
async def test_markdown_private_acl_and_exclusive_typed_source(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ExportAPI) as app, client(app, settings) as server:
        frozen, _ = await source(server)
        for extra in ({"chat_id": "300"}, {"since": NOW.isoformat()}, {"until": NOW.isoformat()}):
            rejected = data(
                await server.call_tool(
                    "export_start", {"profile_id": "personal", "source": frozen, **extra}
                )
            )
            assert rejected["error"]["code"] == "invalid_source"
        missing = data(await server.call_tool("export_start", {"profile_id": "personal"}))
        assert missing["error"]["code"] == "invalid_source"
        for source_value in (
            {**frozen, "kind": "local_index"},
            {**frozen, "destination": str(tmp_path / "public.jsonl")},
        ):
            rejected = await server.call_tool(
                "export_start", {"profile_id": "personal", "source": source_value}
            )
            assert rejected.isError
        started = data(
            await server.call_tool(
                "export_start", {"profile_id": "personal", "source": frozen, "format": "markdown"}
            )
        )["data"]
        finished = await complete(server, "personal", started["job_id"])
        output = Path(finished["result"]["path"])
        text = output.read_text(encoding="utf-8")
        assert "## Source manifest" in text
        assert '"text_source": "reconstructed"' in text
        assert '"original_text": "Exact caption\\nwith whitespace  "' in text
        assert '"text": "Original block"' in text
        if sys.platform == "win32":
            import win32api
            import win32security

            directory_sd = win32security.GetNamedSecurityInfo(
                str(output.parent),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
            )
            assert directory_sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
            file_sd = win32security.GetNamedSecurityInfo(
                str(output), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
            )
            acl = file_sd.GetSecurityDescriptorDacl()
            current = win32security.GetTokenInformation(
                win32security.OpenProcessToken(
                    win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
                ),
                win32security.TokenUser,
            )[0]
            # Windows private_dir also retains local Administrators and Owner Rights;
            # neither grants ordinary users access to the exported evidence.
            allowed = {
                win32security.ConvertSidToStringSid(current),
                "S-1-5-18",
                "S-1-5-32-544",
                "S-1-3-4",
            }
            assert {
                win32security.ConvertSidToStringSid(acl.GetAce(i)[2])
                for i in range(acl.GetAceCount())
            } <= allowed
        else:
            assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_export_source_rejects_cursor_and_other_job_reference(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ArchiveAPI) as app, client(app, settings) as server:
        frozen, page = await source(server, chat_ids=["100"])
        other, _ = await source(server, chat_ids=["100"])
        cursor = await server.call_tool(
            "export_start",
            {"profile_id": "personal", "source": {**frozen, "evidence_ref": page["next_cursor"]}},
        )
        assert cursor.isError
        denied = data(
            await server.call_tool(
                "export_start",
                {
                    "profile_id": "personal",
                    "source": {**frozen, "evidence_ref": other["evidence_ref"]},
                },
            )
        )
        assert denied["error"]["code"] == "invalid_reference"
        assert not (tmp_path / "exports").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["active", "paused", "completed"])
@pytest.mark.parametrize("revoke", ["acl", "generation", "expiry"])
async def test_cross_view_denial_precedes_busy_cleanup_and_survives_restart(
    tmp_path, monkeypatch, phase, revoke
):
    clock = NOW
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)
    profile = Profile(kind="user", send_chats=["100"])
    generation = profile.generation
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})

    class EvidenceAPI(ArchiveAPI):
        def __init__(self, *args):
            super().__init__(*args)
            self.rows[249].text = "Frozen original 😀 " * 700
            self.rows.append(self.rows[249].model_copy(update={"chat_id": "200", "id": "1"}))

    async def status(server, job_id):
        return data(
            await server.call_tool("jobs_status", {"profile_id": "personal", "job_id": job_id})
        )

    async with running(settings, EvidenceAPI) as app, client(app, settings) as server:
        frozen, _ = await source(server)
        args = {"profile_id": "personal", "job_id": frozen["job_id"]}
        durable = (await status(server, frozen["job_id"]))["data"]
        page = data(await server.call_tool("jobs_results", {**args, "limit": 1}))["data"]
        ref_args = {**args, "evidence_ref": page["evidence_ref"]}
        compact = data(await server.call_tool("jobs_results", {**ref_args, "coverage": "compact"}))[
            "data"
        ]
        key = {"chat_id": "100", "message_id": "250"}
        original_args = {**ref_args, "message_keys": [key]}
        excerpt = data(
            await server.call_tool("jobs_results", {**original_args, "max_output_bytes": 8000})
        )["data"]
        assert excerpt["items"][0]["excerpt"]["truncated"]
        chunk_args = {**original_args, "original_field": "text", "max_output_bytes": 8000}
        chunk = data(await server.call_tool("jobs_results", chunk_args))["data"]
        assert chunk["next_content_cursor"]
        direct = data(
            await server.call_tool(
                "messages_get",
                {"profile_id": "personal", "chat_id": "200", "max_output_bytes": 8000},
            )
        )["data"]

        # Keep a real immutable plan and an uncertain fake delivery alongside the export.
        plan_args = {"profile_id": "personal", "recipients": ["100"], "text": "Approved"}
        plan = data(await server.call_tool("delivery_preview", plan_args))["data"]
        api = ExportAPI.instances[-1]
        api.send_error = ConnectionError("private external response")
        execute = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        delivery = data(await server.call_tool("delivery_execute", execute))["data"]["job_id"]
        receipt = await complete(server, "personal", delivery)
        assert receipt["deliveries"][0]["status"] == "unknown"

        frozen["evidence_ref"] = page["evidence_ref"]
        if phase == "completed":
            job = data(
                await server.call_tool("export_start", {"profile_id": "personal", "source": frozen})
            )["data"]["job_id"]
            assert (await complete(server, "personal", job))["status"] == "completed"
        else:
            job, _ = await paused_export(server, frozen)
            if phase == "active":
                await server.call_tool(
                    "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
                )
                assert (await status(server, job))["data"]["status"] == "queued"
        output = tmp_path / "exports" / f"{job}.jsonl"
        temporary = output.with_suffix(".jsonl.part")
        busy = output if phase == "completed" else temporary
        other = temporary if phase == "completed" else output
        other.write_bytes(b"Uncommitted owned derivative")
        copied = busy.read_bytes()
        calls = sum(api.history_calls for api in ExportAPI.instances)
        sends = sum(len(api.sent) for api in ExportAPI.instances)
        unlink = Path.unlink
        attempts = []

        def busy_unlink(path, *args, **kwargs):
            if path in (output, temporary):
                # At the OS boundary, a separate SQLite reader must see committed denial.
                with sqlite3.connect(tmp_path / "workspace.sqlite") as db:
                    journal = json.loads(
                        db.execute("SELECT data FROM jobs WHERE id=?", (job,)).fetchone()[0]
                    )
                assert journal["result"] is None
                assert journal["payload"]["pin"]["released"] is True
                assert journal["payload"]["cleanup"]["status"] == "pending"
                attempts.append(path)
                if path == busy:
                    raise PermissionError(f"PRIVATE_BUSY_PATH: {path}")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", busy_unlink)
        if revoke == "acl":
            profile.read_mode, profile.read_chats = "selected", ["100"]
            code, direct_code = "read_not_allowed", "read_not_allowed"
        elif revoke == "generation":
            profile.generation = "replacement"
            code, direct_code = "account_changed", "invalid_reference"
        else:
            clock += timedelta(minutes=31)
            code, direct_code = "reference_expired", "reference_expired"
        denied = await status(server, job)
        assert denied["error"]["code"] == code, denied
        cleanup = denied["error"]["details"]["cleanup"]
        assert cleanup == {
            "status": "failed",
            "remaining": ["output" if phase == "completed" else "temporary"],
            "error": {
                "code": "export_cleanup_failed",
                "message": "Private export removal failed; retry jobs_control cancel.",
            },
        }
        assert busy.exists() and not other.exists() and len(attempts) == 2
        assert str(tmp_path) not in json.dumps(denied) and "PRIVATE_BUSY_PATH" not in json.dumps(
            denied
        )
        for request in (
            ref_args,
            {**ref_args, "coverage": "compact"},
            compact["coverage"]["details"],
            {**ref_args, "view": "aggregate"},
            {**ref_args, "view": "aggregate", "coverage": "compact"},
            original_args,
            {**original_args, "max_output_bytes": 8000},
            {**chunk_args, "content_cursor": chunk["next_content_cursor"]},
            {**args, "cursor": page["next_cursor"]},
        ):
            blocked = data(await server.call_tool("jobs_results", request))
            assert blocked["error"]["code"] == (
                "invalid_cursor" if revoke == "expiry" and "cursor" in request else code
            ), blocked
        direct_blocked = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "evidence_ref": direct["evidence_ref"]}
            )
        )
        assert direct_blocked["error"]["code"] == direct_code
        retry = data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "cancel"}
            )
        )
        assert retry["ok"] and retry["data"]["cleanup"] == cleanup
        summaries = data(await server.call_tool("jobs_status", {"profile_id": "personal"}))["data"][
            "jobs"
        ]
        assert (
            next(row for row in summaries if row["id"] == job)["error"]["details"]["cleanup"]
            == cleanup
        )
        profile.read_mode, profile.read_chats, profile.generation = "all", [], generation
        assert (await status(server, frozen["job_id"]))["data"] == durable
        assert (await status(server, delivery))["data"] == receipt
        assert (
            data(await server.call_tool("delivery_execute", execute))["data"]["job_id"] == delivery
        )
        assert sum(api.history_calls for api in ExportAPI.instances) == calls

    # Pending cleanup is durable and retryable under restored grants, with no replay.
    async with running(settings, EvidenceAPI) as app, client(app, settings) as server:
        denied = await status(server, job)
        assert denied["error"]["code"] == code
        assert denied["error"]["details"]["cleanup"] == cleanup
        unrelated = data(
            await server.call_tool("export_start", {"profile_id": "personal", "chat_id": "100"})
        )["data"]["job_id"]
        assert (await complete(server, "personal", unrelated))["status"] == "completed"
        assert (await status(server, delivery))["data"] == receipt
        assert (await status(server, frozen["job_id"]))["data"] == durable
        monkeypatch.setattr(Path, "unlink", unlink)
        recovered = data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "cancel"}
            )
        )["data"]
        assert recovered["cleanup"] == {"status": "completed", "remaining": [], "error": None}
        assert not busy.exists()
        assert (await status(server, job))["error"]["code"] == code
        assert (await status(server, delivery))["data"] == receipt
        assert (
            data(await server.call_tool("delivery_execute", execute))["data"]["job_id"] == delivery
        )
        originals = data(await server.call_tool("jobs_results", {**args, "limit": 1}))["data"]
        assert originals["items"][0]["text"] == "Frozen original 😀 " * 700
        assert originals["source_version"] == page["source_version"]
        assert sum(len(api.sent) for api in ExportAPI.instances) == sends
        assert all(api.ack == [] for api in ExportAPI.instances)
        assert copied  # Caller-owned bytes remain beyond the server's boundary.


@pytest.mark.asyncio
async def test_cancelled_cleanup_restarts_in_bounded_batches_without_export_replay(
    tmp_path, monkeypatch
):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    unlink = Path.unlink
    outputs = set()

    def busy_unlink(path, *args, **kwargs):
        if path in outputs:
            raise PermissionError("PRIVATE_BUSY_PATH")
        return unlink(path, *args, **kwargs)

    async with running(settings, ExportAPI) as app, client(app, settings) as server:
        frozen, _ = await source(server)
        for _ in range(10):
            job = data(
                await server.call_tool("export_start", {"profile_id": "personal", "source": frozen})
            )["data"]["job_id"]
            finished = await complete(server, "personal", job)
            outputs.add(Path(finished["result"]["path"]))
        calls = sum(api.history_calls for api in ExportAPI.instances)
        monkeypatch.setattr(Path, "unlink", busy_unlink)
        for output in outputs:
            cancelled = data(
                await server.call_tool(
                    "jobs_control",
                    {"profile_id": "personal", "job_id": output.stem, "action": "cancel"},
                )
            )["data"]
            assert cancelled["status"] == "cancelled"
            assert cancelled["cleanup"]["status"] == "failed"
            assert cancelled["cleanup"]["remaining"] == ["output"]
        assert all(output.exists() for output in outputs)

    # Observe OS operations in each event-loop batch, without replacing queue/store behavior.
    batches = []
    batch = None
    loop = asyncio.get_running_loop()

    def finish_batch():
        nonlocal batch
        batches.append(batch)
        batch = None

    def recovered_unlink(path, *args, **kwargs):
        nonlocal batch
        if path in outputs:
            if batch is None:
                batch = 0
                loop.call_soon(finish_batch)
            batch += 1
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", recovered_unlink)
    async with running(settings, ExportAPI) as app, client(app, settings) as server:
        for _ in range(50):
            summaries = data(await server.call_tool("jobs_status", {"profile_id": "personal"}))[
                "data"
            ]["jobs"]
            exports = [row for row in summaries if row["kind"] == "export"]
            if all(row["error"]["details"]["cleanup"]["status"] == "completed" for row in exports):
                break
            await asyncio.sleep(0.02)
        assert len(exports) == 10 and all(row["status"] == "cancelled" for row in exports)
        assert all(row["error"]["details"]["cleanup"]["remaining"] == [] for row in exports)
        assert batches == [8, 2]
        assert not any(output.exists() for output in outputs)
        assert sum(api.history_calls for api in ExportAPI.instances) == calls
        for output in outputs:
            denied = data(
                await server.call_tool(
                    "jobs_status", {"profile_id": "personal", "job_id": output.stem}
                )
            )
            assert denied["error"]["code"] == "export_cancelled"


@pytest.mark.asyncio
async def test_damaged_export_failure_journals_busy_cleanup_without_stopping_queue(
    tmp_path, monkeypatch
):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ArchiveAPI) as app, client(app, settings) as server:
        frozen, _ = await source(server, chat_ids=["100"])
        job, _ = await paused_export(server, frozen)
        partial = tmp_path / "exports" / f"{job}.jsonl.part"
        partial.write_bytes(b"?" + partial.read_bytes()[1:])
        unlink = Path.unlink

        def busy_unlink(path, *args, **kwargs):
            if path == partial:
                raise PermissionError(f"PRIVATE_BUSY_PATH: {path}")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", busy_unlink)
        assert data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )["ok"]
        failed = await complete(server, "personal", job)
        assert failed["status"] == "failed" and failed["result"] is None
        assert failed["error"]["code"] == "source_changed"
        assert failed["payload"]["pin"]["released"]
        assert failed["payload"]["cleanup"]["status"] == "failed"
        assert failed["payload"]["cleanup"]["remaining"] == ["temporary"]
        assert "PRIVATE_BUSY_PATH" not in json.dumps(failed)
        unrelated = data(
            await server.call_tool("export_start", {"profile_id": "personal", "chat_id": "100"})
        )["data"]["job_id"]
        assert (await complete(server, "personal", unrelated))["status"] == "completed"
        resumed = data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )
        assert resumed["error"]["code"] == "job_terminal"
        monkeypatch.setattr(Path, "unlink", unlink)
        cleaned = data(
            await server.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "cancel"}
            )
        )["data"]
        assert cleaned["status"] == "failed" and cleaned["cleanup"]["status"] == "completed"
        assert not partial.exists()
