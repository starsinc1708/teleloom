import json
import socket
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from teleloom.cli import app
from teleloom.config import Profile, Settings, TranscriptionConfig
from teleloom.models import TeleloomError
from tests.telegram_fakes import CHAT, NOW, SDK, factory
from tests.test_transport import client, running


def test_local_doctor_has_artifact_and_safe_checks_without_creating_state(tmp_path, monkeypatch):
    data = tmp_path / "unused"
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(data))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "private-diagnostic-token")
    result = CliRunner().invoke(app, ["doctor", "--mode", "local"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["mode"] == "local"
    assert report["build"]["package_version"] == report["version"]
    assert set(report["build"]) == {"package_version", "build_id", "source_commit", "build_type"}
    assert report["daemon"]["status"] == "not_checked"
    assert report["tools"]["source"] == "installed_package"
    schemas = report["tools"]["schemas"]
    assert {"fields", "preset"} <= set(schemas["messages_get"]["parameters"])
    assert "fields" not in schemas["delivery_execute"]["parameters"]
    assert "jev" in report["optional_engines"]
    assert "private-diagnostic-token" not in result.output
    assert not data.exists()


def test_local_doctor_reports_effective_read_only_scope_without_private_paths(
    tmp_path, monkeypatch
):
    settings = Settings(
        data_dir=tmp_path,
        exposure_mode="read-only",
        profiles={
            "work": Profile(
                kind="user",
                identity={"id": "7", "username": "owner"},
                read_mode="selected",
                read_chats=["100"],
                send_chats=["100"],
                mutation_chats=["100"],
                sync_chats=["100"],
                jev_chats=["100"],
                event_chats=["100"],
                file_roots=[str(tmp_path / "private-files")],
            )
        },
    )
    settings.save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "private-diagnostic-token")
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert "delivery_execute" not in report["tools"]["schemas"]
    assert next(c for c in report["checks"] if c["name"] == "projection_tools")["ok"]
    scope = report["scope"]
    assert scope["tool_exposure"]["mode"] == "read-only"
    profile = scope["profiles"][0]
    assert profile["identity"] == {"id": "7", "username": "owner"}
    assert profile["grants"]["read"] == {"mode": "selected", "chat_ids": ["100"]}
    assert profile["grants"]["send"] == ["100"]
    assert profile["grants"]["mutation"] == ["100"]
    assert profile["grants"]["sync"] == ["100"]
    assert profile["grants"]["ai"] == ["100"]
    assert profile["grants"]["event"] == ["100"]
    assert report["health"]["telegram"] == "not_checked"
    assert str(tmp_path) not in result.output
    assert "private-diagnostic-token" not in result.output
    assert not (tmp_path / "workspace.sqlite").exists()


async def test_server_status_exposes_only_safe_artifact_build_contract(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as owner, client(owner, settings) as mcp:
        result = await mcp.call_tool("server_status", {})
        info = result.structuredContent["data"]
        assert info["build"]["package_version"] == info["version"]
        assert info["build"]["build_type"] in {"release", "local", "dev", "unknown"}
        assert str(tmp_path) not in json.dumps(info)
        assert result.structuredContent == json.loads(result.content[0].text)


async def test_selected_status_probe_reads_once_and_reports_separate_timings(tmp_path, monkeypatch):
    from telethon import functions, types

    sdk = SDK()
    sdk.session.process_entities([types.InputPeerChannel(100, 123456789)])
    sdk.response = SimpleNamespace(
        messages=[
            types.Message(
                id=5,
                peer_id=types.PeerChannel(100),
                date=NOW,
                message="Private content is never returned by diagnostics",
            )
        ],
        users=[],
        chats=[],
    )
    clock = iter([10.0, 10.25, 11.0, 11.5, 12.0, 12.125])
    monkeypatch.setattr("teleloom.diagnostics.perf_counter", lambda: next(clock), raising=False)
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user", identity={"id": "1"}, read_mode="selected", read_chats=[CHAT]
            )
        },
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        local = (await mcp.call_tool("server_status", {})).structuredContent["data"]
        assert local["health"]["telegram"] == "not_checked"
        assert sdk.calls == []
        result = await mcp.call_tool("server_status", {"profile_id": "work", "chat_id": CHAT})
        report = result.structuredContent["data"]
        probe = report["probe"]
        assert probe["status"] == "ok"
        assert probe["profile_id"] == "work" and probe["chat_id"] == CHAT
        assert probe["timings_seconds"] == {"connect": 0.25, "read": 0.5, "local_processing": 0.125}
        assert probe["logical_requests"] == {"connect": 1, "read": 1}
        assert probe["observed_rpc_requests"] is None
        assert probe["returned"] == 1
        assert len(sdk.calls) == 1
        assert isinstance(sdk.calls[0], functions.messages.GetHistoryRequest)
        assert sdk.calls[0].limit == 1
        assert "Private content" not in result.content[0].text
        assert "123456789" not in result.content[0].text
        assert str(tmp_path) not in result.content[0].text
        assert report["health"]["telegram"] == "selected_read_ok"


@pytest.mark.parametrize(
    ("arguments", "exposure", "kind", "code"),
    [
        ({"profile_id": "missing", "chat_id": CHAT}, "all", "user", "profile_not_found"),
        ({"profile_id": "work", "chat_id": "100"}, "all", "user", "read_not_allowed"),
        ({"profile_id": "work", "chat_id": CHAT}, "selected", "user", "tool_not_exposed"),
        ({"profile_id": "work", "chat_id": CHAT}, "all", "bot", "unsupported_capability"),
        ({"profile_id": "work"}, "all", "user", "invalid_probe"),
        ({"profile_id": "work", "chat_id": "C:/private/token"}, "all", "user", "invalid_id"),
    ],
)
async def test_status_probe_checks_scope_before_connection(
    tmp_path, arguments, exposure, kind, code
):
    created = []

    def no_connection(*args):
        created.append(args[0])
        raise AssertionError("Access must be checked before creating a Telegram adapter")

    settings = Settings(
        data_dir=tmp_path,
        profiles={"work": Profile(kind=kind, read_mode="selected", read_chats=[CHAT])},
        exposure_mode=exposure,
        exposed_tools=["server_status"],
    )
    async with running(settings, no_connection) as owner, client(owner, settings) as mcp:
        result = await mcp.call_tool("server_status", arguments)
        probe = result.structuredContent["data"]["probe"]
        assert probe["status"] == "failed"
        assert probe["error"]["code"] == code
        assert probe["error"]["phase"] == "access"
        assert probe["error"]["next_action"]
        assert probe["logical_requests"] == {"connect": 0, "read": 0}
        assert probe["observed_rpc_requests"] is None
        assert created == []
        assert "C:/private/token" not in result.content[0].text


@pytest.mark.parametrize("phase", ["connect", "read"])
async def test_probe_timeout_has_safe_recovery_and_no_automatic_retry(tmp_path, phase):
    import asyncio

    from telethon import types

    class StalledSDK(SDK):
        connections = 0
        disconnects = 0
        reads = 0
        stall = True

        async def connect(self):
            self.connections += 1
            if phase == "connect" and self.stall:
                await asyncio.Event().wait()

        async def disconnect(self):
            self.disconnects += 1

        async def __call__(self, request, *args, **kwargs):
            self.reads += 1
            if phase == "read" and self.stall:
                await asyncio.Event().wait()
            return await super().__call__(request, *args, **kwargs)

    sdk = StalledSDK()
    sdk.session.process_entities([types.InputPeerChannel(100, 123456789)])
    sdk.response = SimpleNamespace(messages=[], users=[], chats=[])
    settings = Settings(data_dir=tmp_path, profiles={"work": Profile(kind="user")})
    arguments = {"profile_id": "work", "chat_id": CHAT, "timeout_seconds": 0.05}
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        result = await asyncio.wait_for(mcp.call_tool("server_status", arguments), 2)
        probe = result.structuredContent["data"]["probe"]
        assert probe["status"] == "failed"
        assert probe["error"]["phase"] == phase
        assert probe["error"]["code"] == "timeout"
        assert "unknown delivery" in probe["error"]["next_action"]
        assert 0.04 <= sum(value or 0 for value in probe["timings_seconds"].values()) < 1
        assert sdk.connections == 1
        assert sdk.reads == (1 if phase == "read" else 0)
        sdk.stall = False
        again = (await mcp.call_tool("server_status", arguments)).structuredContent["data"]
        assert again["probe"]["status"] == "ok"
        assert again["probe"]["connection"] == ("reused" if phase == "read" else "opened")
        assert sdk.connections == (1 if phase == "read" else 2)
        assert sdk.reads == (2 if phase == "read" else 1)


@pytest.mark.parametrize("problem", ["revoked", "busy", "raw"])
async def test_probe_redacts_auth_ownership_and_raw_errors(tmp_path, problem):
    from telethon import errors

    from teleloom.session_state import acquire_session

    class BrokenSDK(SDK):
        async def is_user_authorized(self):
            if problem == "revoked":
                raise errors.SessionRevokedError(request="sensitive-rpc-value")
            raise TeleloomError(
                "raw-secret-code",
                "C:/private/session sensitive-rpc-value",
                details={"token": "private-token"},
            )

    sdk = BrokenSDK()
    settings = Settings(data_dir=tmp_path, profiles={"work": Profile(kind="user")})
    lease = acquire_session(sdk.session) if problem == "busy" else None
    try:
        async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
            result = await mcp.call_tool("server_status", {"profile_id": "work", "chat_id": CHAT})
            probe = result.structuredContent["data"]["probe"]
            expected = {"revoked": "auth_required", "busy": "session_in_use", "raw": "unavailable"}[
                problem
            ]
            assert probe["error"]["code"] == expected
            assert probe["error"]["phase"] == "connect"
            assert probe["logical_requests"]["read"] == 0
            assert sdk.calls == []
            assert all(
                secret not in result.content[0].text
                for secret in ("C:/private", "raw-secret", "sensitive-rpc", "private-token")
            )
            if problem == "revoked":
                assert "--replace" in probe["error"]["next_action"]
            if problem == "raw":
                # Legacy doctor --live includes this public snapshot unchanged.
                profiles = await mcp.call_tool("profiles_list", {})
                assert profiles.structuredContent["data"]["profiles"][0]["error"] == "unavailable"
                assert "raw-secret-code" not in profiles.content[0].text
    finally:
        if lease:
            lease.release()


async def test_probe_rechecks_read_policy_after_connect(tmp_path):
    from telethon import types

    settings = Settings(
        data_dir=tmp_path,
        profiles={"work": Profile(kind="user", read_mode="selected", read_chats=[CHAT])},
    )

    class RevokingSDK(SDK):
        async def connect(self):
            settings.profiles["work"].read_chats.clear()

    sdk = RevokingSDK()
    sdk.session.process_entities([types.InputPeerChannel(100, 123456789)])
    sdk.response = SimpleNamespace(messages=[], users=[], chats=[])
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        result = await mcp.call_tool("server_status", {"profile_id": "work", "chat_id": CHAT})
        assert result.structuredContent["data"]["probe"]["error"]["code"] == "read_not_allowed"
        assert sdk.calls == []


async def test_probe_requires_exact_saved_peer_and_never_reads_another_chat(tmp_path):
    from telethon import types

    sdk = SDK()
    # Native MemorySession may resolve an unmarked positive ID to another peer type.
    sdk.session.process_entities([types.InputPeerChannel(100, 123456789)])
    settings = Settings(
        data_dir=tmp_path,
        profiles={"work": Profile(kind="user", read_mode="selected", read_chats=["100"])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        result = await mcp.call_tool("server_status", {"profile_id": "work", "chat_id": "100"})
        assert result.structuredContent["data"]["probe"]["error"]["code"] == "peer_unavailable"
        assert sdk.calls == []


@pytest.mark.parametrize("missing_peer", [False, True])
async def test_cli_selected_probe_uses_running_owner_without_dialog_scan(
    tmp_path, monkeypatch, missing_peer
):
    import asyncio

    from telethon import types

    sdk = SDK()
    if not missing_peer:
        sdk.session.process_entities([types.InputPeerChannel(100, 123456789)])
    sdk.response = SimpleNamespace(messages=[], users=[], chats=[])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings = Settings(
            data_dir=tmp_path, port=sock.getsockname()[1], profiles={"work": Profile(kind="user")}
        )
    settings.save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "test-owner-token")
    async with running(settings, factory(sdk), network=True):
        result = await asyncio.to_thread(
            CliRunner().invoke, app, ["doctor", "--profile", "work", "--chat", CHAT]
        )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["mode"] == "mcp"
    assert report["daemon"]["status"] == "running"
    assert report["scope"]["source"] == "running_owner"
    assert report["probe"]["logical_requests"] == {"connect": 1, "read": 1}
    assert report["probe"]["observed_rpc_requests"] is None
    if missing_peer:
        assert report["failure"]["code"] == "peer_unavailable"
        assert sdk.calls == []
    else:
        assert report["health"]["telegram"] == "selected_read_ok"
        assert report["probe"]["returned"] == 0
        assert len(sdk.calls) == 1
    assert str(tmp_path) not in result.output
    assert "test-owner-token" not in result.output


async def test_cli_mcp_health_rejects_other_credentials_without_secret_output(
    tmp_path, monkeypatch
):
    import asyncio

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings = Settings(data_dir=tmp_path, port=sock.getsockname()[1])
    settings.save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "wrong-private-token")
    async with running(settings, network=True):
        result = await asyncio.to_thread(CliRunner().invoke, app, ["doctor", "--mode", "mcp"])
    report = json.loads(result.output)
    assert report["failure"]["code"] == "daemon_mismatch"
    assert report["failure"]["phase"] == "owner"
    assert report["health"]["telegram"] == "not_checked"
    assert all(
        secret not in result.output
        for secret in ("wrong-private-token", "test-owner-token", str(tmp_path))
    )


def test_cli_health_timeout_is_not_reported_as_stopped_owner(tmp_path, monkeypatch):
    import httpx

    settings = Settings(data_dir=tmp_path)
    settings.save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "private-token")
    original = httpx.AsyncClient

    def stalled(request):
        raise httpx.ReadTimeout("C:/private/workspace raw-rpc private-token", request=request)

    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(stalled), **kwargs),
    )
    result = CliRunner().invoke(app, ["doctor", "--mode", "mcp"])
    report = json.loads(result.output)
    assert report["daemon"]["status"] == "timeout"
    assert report["failure"]["phase"] == "owner"
    assert "reconcile unknown delivery" in report["failure"]["next_action"].lower()
    assert all(secret not in result.output for secret in ("C:/private", "raw-rpc", "private-token"))


def test_local_selected_exposure_is_a_deliberate_projection_skip(tmp_path, monkeypatch):
    Settings(data_dir=tmp_path, exposure_mode="selected", exposed_tools=["server_status"]).save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "private-token")
    result = CliRunner().invoke(app, ["doctor"])
    report = json.loads(result.output)
    assert set(report["tools"]["schemas"]) == {"server_status"}
    assert next(c for c in report["checks"] if c["name"] == "projection_tools")["ok"] is None
    assert not any(
        "projection" in action or "field selection" in action
        for action in report["recovery_actions"]
    )


async def test_cli_legacy_live_redacts_local_model_and_identity_extras(tmp_path, monkeypatch):
    import asyncio

    model = tmp_path / "private-model"
    model.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (model / name).write_bytes(b"local fixture; never loaded")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings = Settings(
            data_dir=tmp_path,
            port=sock.getsockname()[1],
            profiles={
                "work": Profile(
                    kind="user",
                    identity={"id": "7", "phone": "private-phone", "session": "private-session"},
                    transcription=TranscriptionConfig(local_model_path=str(model)),
                )
            },
        )
    settings.save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "test-owner-token")
    sdk = SDK()
    async with running(settings, factory(sdk), network=True):
        result = await asyncio.to_thread(CliRunner().invoke, app, ["doctor", "--live"])
    report = json.loads(result.output)
    assert report["health"]["telegram"] == "not_checked"
    profile = report["live"]["data"]["profiles"][0]
    assert profile["identity"] == {"id": "7"}
    assert (
        profile["capabilities"]["transcription_engines"]["providers"]["local"]["model"]
        == "configured_local_model"
    )
    assert sdk.calls == []
    assert all(
        secret not in result.output
        for secret in (str(tmp_path), "private-phone", "private-session", "test-owner-token")
    )


def test_mcp_doctor_reports_stopped_owner_without_starting_it(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings = Settings(data_dir=tmp_path, port=sock.getsockname()[1])
    settings.save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "isolated-doctor-token")
    result = CliRunner().invoke(app, ["doctor", "--mode", "mcp"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["daemon"]["status"] == "not_running"
    assert report["tools"]["count"] == 0
    assert any("teleloom serve" in action for action in report["recovery_actions"])
    assert not (tmp_path / "workspace.sqlite").exists()


def test_mcp_doctor_reports_busy_owner_without_starting_or_killing_it(tmp_path, monkeypatch):
    from teleloom.daemon import ownership

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings = Settings(data_dir=tmp_path, port=sock.getsockname()[1])
    settings.save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "isolated-doctor-token")
    with ownership(settings):
        result = CliRunner().invoke(app, ["doctor", "--mode", "mcp"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["daemon"]["status"] == "owner_busy"
        assert report["failure"]["phase"] == "owner"
        assert "existing owner" in report["failure"]["next_action"]
        assert report["health"]["telegram"] == "not_checked"
    assert str(tmp_path) not in result.output
    assert not (tmp_path / "workspace.sqlite").exists()


def test_doctor_reports_invalid_configuration_with_recovery(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        '{"private": "sensitive-configuration",', encoding="utf-8"
    )
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "isolated-doctor-token")
    result = CliRunner().invoke(app, ["doctor", "--mode", "mcp"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["configuration_status"] == "invalid"
    assert report["daemon"]["status"] == "not_checked"
    assert any("config.json" in action for action in report["recovery_actions"])
    assert "sensitive-configuration" not in result.output


@pytest.mark.parametrize("mode_arguments", [["--mode", "mcp"], ["--live"]])
async def test_mcp_doctor_discovers_running_owner_and_build_without_reads(tmp_path, mode_arguments):
    import asyncio
    import os
    import sys

    import uvicorn

    from teleloom.daemon import create_application

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings = Settings(data_dir=tmp_path, port=sock.getsockname()[1])
    settings.save()
    owner = uvicorn.Server(
        uvicorn.Config(
            create_application(settings, "isolated-doctor-token"),
            host="127.0.0.1",
            port=settings.port,
            log_level="critical",
        )
    )
    task = asyncio.create_task(owner.serve())
    try:
        async with asyncio.timeout(10):
            while not owner.started:
                await asyncio.sleep(0.01)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "teleloom",
            "doctor",
            *mode_arguments,
            env={
                **os.environ,
                "TELELOOM_DATA_DIR": str(tmp_path),
                "TELELOOM_MCP_TOKEN": "isolated-doctor-token",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
        report = json.loads(stdout)
        assert report["daemon"]["status"] == "running"
        assert report["daemon"]["profiles"] == 0
        assert report["daemon"]["build"] == report["build"]
        assert report["tools"]["source"] == "running_mcp"
        assert "response_fields_select" in report["tools"]["schemas"]
        if mode_arguments == ["--live"]:
            assert report["live"]["ok"] is True
            assert report["live"]["data"]["profiles"] == []
        assert "isolated-doctor-token" not in stdout.decode()
    finally:
        owner.should_exit = True
        await asyncio.wait_for(task, 10)
