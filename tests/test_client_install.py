import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from teleloom.cli import app


@pytest.fixture
def native_client(tmp_path, monkeypatch):
    """Fake only the external CLI; keep actual temporary config and skill files."""
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path / "owner with spaces"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("teleloom.cli.shutil.which", lambda name: name)
    config = tmp_path / "hermes home" / "profiles" / "work" / "config.yaml"
    config.parent.mkdir(parents=True)
    original = {"model": "owner-model", "mcp_servers": {"other": {"command": "other"}}}
    config.write_text(yaml.safe_dump(original), encoding="utf-8")
    opencode = tmp_path / "opencode.jsonc"
    opencode.write_text(
        json.dumps(
            {
                "model": "owner-model",
                "mcp": {"servers": {"other": {"type": "remote", "url": "https://example.test"}}},
            }
        ),
        encoding="utf-8",
    )
    calls = []
    behavior = {"add": "save"}

    def run(argv, **kwargs):
        assert isinstance(argv, list)
        assert kwargs.get("shell", False) is False
        if kwargs.get("input") is not None:
            assert argv[:4] == ["hermes", "mcp", "add", "teleloom"]
            assert kwargs["input"] == "\n"
            assert kwargs.get("stdin") is None
        else:
            assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["HERMES_HOME"] == str(tmp_path / "hermes home")
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        calls.append(argv)
        client, *args = argv
        path = config if client == "hermes" else opencode
        current = yaml.safe_load(path.read_text(encoding="utf-8"))
        if args == ["config", "path"]:
            return subprocess.CompletedProcess(argv, 0, str(config) + "\n", "")
        if args == ["config", "get", "mcp_servers", "--json"]:
            if "mcp_servers" not in current:
                return subprocess.CompletedProcess(argv, 1, "", "Config key not set: mcp_servers")
            return subprocess.CompletedProcess(argv, 0, json.dumps(current["mcp_servers"]), "")
        if args == ["debug", "config"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"type": "document", "path": str(path), "info": current}]), ""
            )
        assert args[:3] == ["mcp", "add", "teleloom"]
        if behavior["add"] == "error":
            return subprocess.CompletedProcess(argv, 7, "", "synthetic-secret")
        if behavior["add"] == "noop":
            return subprocess.CompletedProcess(argv, 0, "Cancelled", "")
        if client == "hermes" and kwargs.get("input") != "\n":
            # Native Hermes cancels _choose_tools on EOF after successful discovery.
            return subprocess.CompletedProcess(argv, 0, "Enable all tools? Cancelled.", "")
        env = dict([args[args.index("--env") + 1].split("=", 1)])
        if client == "opencode":
            assert "--global" in args
            entry = {"type": "local", "command": args[args.index("--") + 1 :], "environment": env}
            current["mcp"]["servers"]["teleloom"] = entry
        else:
            entry = {
                "command": args[args.index("--command") + 1],
                "args": args[args.index("--args") + 1 :],
                "env": env,
                "enabled": True,
                "tools": {"exclude": []},
            }
            current.setdefault("mcp_servers", {})["teleloom"] = entry
        path.write_text(
            yaml.safe_dump(current) if client == "hermes" else json.dumps(current), encoding="utf-8"
        )
        return subprocess.CompletedProcess(argv, 0, "Saved", "")

    monkeypatch.setattr(subprocess, "run", run)
    return config, opencode, calls, behavior


@pytest.mark.parametrize("client", ["opencode", "hermes"])
def test_native_client_install_preserves_config_and_is_repeatable(native_client, client):
    hermes, opencode, calls, _ = native_client
    runner = CliRunner()
    fragment = runner.invoke(app, ["config", "client", "--client", client])
    assert fragment.exit_code == 0, fragment.output
    assert calls == []  # Printing stays side-effect free.
    parsed = json.loads(fragment.output)
    expected = (
        parsed["mcp"]["servers"]["teleloom"]
        if client == "opencode"
        else parsed["mcp_servers"]["teleloom"]
    )
    args = ["config", "client", "--client", client, "--install"]
    path = opencode if client == "opencode" else hermes
    original = yaml.safe_load(path.read_text(encoding="utf-8"))
    installed = runner.invoke(app, args)
    assert installed.exit_code == 0, installed.output
    assert json.loads(installed.output)["status"] == "installed"
    env = (
        "TELELOOM_DATA_DIR="
        + (expected["environment"] if client == "opencode" else expected["env"])[
            "TELELOOM_DATA_DIR"
        ]
    )
    command = (
        expected["command"] if client == "opencode" else [expected["command"], *expected["args"]]
    )
    expected_add = (
        [client, "mcp", "add", "teleloom", "--global", "--env", env, "--", *command]
        if client == "opencode"
        else [
            client,
            "mcp",
            "add",
            "teleloom",
            "--command",
            command[0],
            "--env",
            env,
            "--args",
            *command[1:],
        ]
    )
    assert expected_add in calls
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["model"] == "owner-model"
    servers = saved["mcp"]["servers"] if client == "opencode" else saved["mcp_servers"]
    original_servers = (
        original["mcp"]["servers"] if client == "opencode" else original["mcp_servers"]
    )
    assert servers["other"] == original_servers["other"]
    before = path.read_bytes()
    repeated = runner.invoke(app, args)
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.output)["status"] == "already_configured"
    assert calls.count(expected_add) == 1
    assert path.read_bytes() == before
    for key, value in [
        ("command", "owner-command"),
        ("command" if client == "opencode" else "args", ["changed-arguments"]),
        ("environment" if client == "opencode" else "env", {"TELELOOM_DATA_DIR": "another-owner"}),
        ("disabled" if client == "opencode" else "enabled", client == "opencode"),
    ]:
        changed = json.loads(json.dumps(saved))
        changed_servers = (
            changed["mcp"]["servers"] if client == "opencode" else changed["mcp_servers"]
        )
        changed_servers["teleloom"][key] = value
        path.write_text(json.dumps(changed), encoding="utf-8")
        before = path.read_bytes()
        conflict = runner.invoke(app, args)
        assert conflict.exit_code == 1
        assert "client_config_conflict" in conflict.output
        assert path.read_bytes() == before
        assert calls.count(expected_add) == 1


@pytest.mark.parametrize("client", ["opencode", "hermes"])
@pytest.mark.parametrize("mode", ["error", "noop"])
def test_native_install_reports_failure_without_success(native_client, client, mode):
    hermes, opencode, _, behavior = native_client
    behavior["add"] = mode
    path = hermes if client == "hermes" else opencode
    before = path.read_bytes()
    result = CliRunner().invoke(app, ["config", "client", "--client", client, "--install"])
    assert result.exit_code == 1
    assert "client_install_failed" in result.output
    assert "synthetic-secret" not in result.output
    assert path.read_bytes() == before


def test_hermes_skills_follow_native_profile_and_target_wins(native_client, tmp_path):
    config, _, calls, _ = native_client
    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--client", "hermes"])
    assert result.exit_code == 0, result.output
    destination = config.parent / "skills"
    assert json.loads(result.output)["destination"] == str(destination.resolve())
    assert calls == [["hermes", "config", "path"]]
    assert len(list(destination.glob("*/SKILL.md"))) == 6
    existing = destination / "teleloom-connect" / "SKILL.md"
    existing.write_text("owner workflow", encoding="utf-8")
    assert runner.invoke(app, ["skills", "install", "--client", "hermes"]).exit_code == 1
    assert existing.read_text(encoding="utf-8") == "owner workflow"
    assert runner.invoke(app, ["skills", "install", "--client", "hermes", "--force"]).exit_code == 0
    assert existing.read_text(encoding="utf-8") != "owner workflow"
    calls.clear()
    explicit = runner.invoke(
        app, ["skills", "install", "--client", "hermes", "--target", str(tmp_path / "explicit")]
    )
    assert explicit.exit_code == 0, explicit.output
    assert calls == []


@pytest.mark.parametrize("client", ["opencode", "hermes"])
def test_missing_client_cli_is_explicit(native_client, monkeypatch, client):
    monkeypatch.setattr("teleloom.cli.shutil.which", lambda name: None)
    result = CliRunner().invoke(app, ["config", "client", "--client", client, "--install"])
    assert result.exit_code == 1
    assert "client_cli_unavailable" in result.output
    if client == "hermes":
        result = CliRunner().invoke(app, ["skills", "install", "--client", client])
        assert result.exit_code == 1
        assert "client_cli_unavailable" in result.output


@pytest.mark.parametrize("client", ["codex", "claude", "opencode-v1", "pi"])
def test_install_is_limited_to_requested_clients(native_client, client):
    result = CliRunner().invoke(app, ["config", "client", "--client", client, "--install"])
    assert result.exit_code == 1
    assert "client_install_unsupported" in result.output
    assert native_client[2] == []


def test_windows_install_keeps_windowless_interpreter(native_client, monkeypatch, tmp_path):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "platform", "win32")
    result = CliRunner().invoke(app, ["config", "client", "--client", "hermes", "--install"])
    assert result.exit_code == 0, result.output
    assert any(
        "--command" in call and call[call.index("--command") + 1] == str(pythonw)
        for call in native_client[2]
    )


def test_hermes_install_without_optional_mcp_section(native_client):
    config = native_client[0]
    config.write_text("model: owner-model\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["config", "client", "--client", "hermes", "--install"])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["model"] == "owner-model"


@pytest.mark.parametrize("mode", ["invalid_json", "command_error", "timeout"])
def test_unreadable_client_config_does_not_trigger_add(native_client, monkeypatch, mode):
    hermes, opencode, calls, _ = native_client
    before = (hermes.read_bytes(), opencode.read_bytes())

    def fail(argv, **kwargs):
        calls.append(argv)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        return subprocess.CompletedProcess(
            argv, 3 if mode == "command_error" else 0, "not-json", "synthetic-secret"
        )

    monkeypatch.setattr(subprocess, "run", fail)
    result = CliRunner().invoke(app, ["config", "client", "--client", "opencode", "--install"])
    assert result.exit_code == 1
    assert "client_config_unavailable" in result.output or "client_install_failed" in result.output
    assert "synthetic-secret" not in result.output
    assert not any("add" in call for call in calls)
    assert (hermes.read_bytes(), opencode.read_bytes()) == before


@pytest.mark.parametrize("output", ["", "relative/config.yaml", "unexpected\noutput"])
def test_invalid_hermes_path_does_not_install_skills(native_client, monkeypatch, output):
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, "")
    )
    result = CliRunner().invoke(app, ["skills", "install", "--client", "hermes"])
    assert result.exit_code == 1
    assert "client_config_unavailable" in result.output
    assert not (native_client[0].parent / "skills").exists()


@pytest.mark.parametrize("home", ["platform-install", "custom-home", "custom-home/profiles/work"])
def test_hermes_destination_uses_client_resolution_over_home_guess(
    native_client, monkeypatch, tmp_path, home
):
    resolved = tmp_path / home / "config.yaml"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, str(resolved), ""),
    )
    result = CliRunner().invoke(app, ["skills", "install", "--client", "hermes"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["destination"] == str((resolved.parent / "skills").resolve())
    assert not (tmp_path / ".hermes" / "skills").exists()


@pytest.mark.parametrize("client", ["opencode", "hermes"])
def test_windows_native_path_spelling_preserves_existing_connection(
    native_client, monkeypatch, client
):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", "C:/Teleloom/pythonw.exe")
    monkeypatch.setattr(
        "teleloom.cli.Settings.load", lambda: SimpleNamespace(data_dir="C:/Owner state")
    )
    runner = CliRunner()
    fragment = json.loads(runner.invoke(app, ["config", "client", "--client", client]).output)
    entry = (
        fragment["mcp"]["servers"]["teleloom"]
        if client == "opencode"
        else fragment["mcp_servers"]["teleloom"]
    )
    if client == "opencode":
        entry["command"][0] = entry["command"][0].replace("/", "\\\\")
        entry["environment"]["TELELOOM_DATA_DIR"] = "c:\\\\Owner state"
    else:
        entry["command"] = entry["command"].replace("/", "\\\\")
        entry["env"]["TELELOOM_DATA_DIR"] = "c:\\\\Owner state"
    config, opencode, calls, _ = native_client
    path = opencode if client == "opencode" else config
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    servers = saved["mcp"]["servers"] if client == "opencode" else saved["mcp_servers"]
    servers["teleloom"] = entry
    path.write_text(json.dumps(saved), encoding="utf-8")
    before = path.read_bytes()
    result = runner.invoke(app, ["config", "client", "--client", client, "--install"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "already_configured"
    assert path.read_bytes() == before
    assert not any("add" in call for call in calls)
