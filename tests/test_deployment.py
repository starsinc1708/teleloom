"""Deployment entry points use real archives, state, installed wheels and MCP."""

import json
import os
import shutil
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.fakes import data

pytestmark = pytest.mark.process_e2e

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_desktop_bundle_runs_installed_wheel_over_stdio_without_checkout(tmp_path):
    artifacts = tmp_path / "artifacts"
    subprocess.run(
        [shutil.which("uv"), "build", "--wheel", "--out-dir", str(artifacts), str(ROOT)],
        check=True,
        capture_output=True,
    )
    wheel = next(artifacts.glob("*.whl"))
    bundle = tmp_path / "teleloom.mcpb"
    built = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "package_desktop.py"),
            "--wheel",
            str(wheel),
            "--output",
            str(bundle),
        ],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    extracted = tmp_path / "desktop with spaces"
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        assert names == {"manifest.json", "pyproject.toml", "uv.lock", "server.py", wheel.name}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["server"]["type"] == "uv"
        assert manifest["user_config"]["data_directory"]["type"] == "directory"
        assert "session" not in json.dumps(manifest).lower()
        assert archive.read(wheel.name) == wheel.read_bytes()
        archive.extractall(extracted)
    state = tmp_path / "private state"
    state.mkdir()
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    (state / "config.json").write_text(json.dumps({"port": port}), encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TELELOOM_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment.update(TELELOOM_DATA_DIR=str(state), TELELOOM_MCP_TOKEN="deployment-test-only")
    foreign_environment = tmp_path / "foreign environment"
    environment["UV_PROJECT_ENVIRONMENT"] = str(foreign_environment)
    environment.update(
        {
            key: value.replace("${__dirname}", str(extracted)).replace(
                "${user_config.data_directory}", str(state)
            )
            for key, value in manifest["server"]["mcp_config"]["env"].items()
        }
    )
    parameters = StdioServerParameters(
        command=shutil.which("uv"),
        args=["run", "--frozen", "--directory", str(extracted), "server.py"],
        cwd=str(tmp_path),
        env=environment,
    )
    try:
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert {"profiles_list", "delivery_execute", "jobs_results"} <= names
                assert data(await client.call_tool("profiles_list"))["data"]["profiles"] == []
                status = data(await client.call_tool("server_status"))["data"]
                assert status["build"]["source_commit"] != "unknown"
                assert not foreign_environment.exists()
                assert (extracted / ".venv").is_dir()
    finally:
        subprocess.run(
            [
                shutil.which("uv"),
                "run",
                "--frozen",
                "--directory",
                str(extracted),
                "teleloom",
                "stop",
            ],
            env=environment,
            check=True,
            capture_output=True,
        )


def test_container_platform_secrets_feed_real_cli_without_persisting_values(tmp_path):
    secrets = tmp_path / "platform secrets"
    secrets.mkdir()
    credential = "synthetic-platform-token-never-persist"
    (secrets / "TELELOOM_MCP_TOKEN").write_text(credential, encoding="utf-8")
    state = tmp_path / "state"
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("TELELOOM_")
    }
    environment.update(TELELOOM_DATA_DIR=str(state), TELELOOM_CONTAINER_SECRET_DIR=str(secrets))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "container_entry.py"), "doctor", "--mode", "local"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert credential not in result.stdout + result.stderr
    assert json.loads(result.stdout)["mcp_token_available"] is True
    assert all(
        credential.encode() not in path.read_bytes() for path in state.rglob("*") if path.is_file()
    )


@pytest.mark.parametrize("contents", [b"credential\x00bad", b"x" * 65537], ids=["nul", "oversize"])
def test_container_rejects_invalid_secret_before_launch_without_echoing_it(tmp_path, contents):
    secrets = tmp_path / "platform"
    secrets.mkdir()
    (secrets / "TELELOOM_MCP_TOKEN").write_bytes(contents)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "container_entry.py"), "doctor"],
        env={**os.environ, "TELELOOM_CONTAINER_SECRET_DIR": str(secrets)},
        capture_output=True,
    )
    assert result.returncode != 0
    assert result.stderr.decode().strip() == "Platform credential mount is unavailable or invalid."
    assert not result.stdout


def test_compose_resolves_platform_secrets_without_credential_values(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI config validation is also exercised by the Linux container CI job")
    secret = "synthetic-compose-value-must-not-appear"
    names = (
        "TELELOOM_MCP_TOKEN",
        "TELELOOM_PERSONAL_API_ID",
        "TELELOOM_PERSONAL_API_HASH",
        "TELELOOM_PERSONAL_SESSION",
    )
    for name in names:
        (tmp_path / name).write_text(secret, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TELELOOM_", "COMPOSE_"))
    }
    environment.update({name + "_FILE": str(tmp_path / name) for name in names})
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(empty_env),
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "compose.user.yaml"),
            "config",
            "--format",
            "json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert secret not in result.stdout + result.stderr
    config = json.loads(result.stdout)
    assert {Path(item["file"]) for item in config["secrets"].values()} == {
        tmp_path / name for name in names
    }
    assert all("environment" not in item for item in config["secrets"].values())
    service = config["services"]["teleloom"]
    assert service["read_only"] and service["cap_drop"] == ["ALL"]
    assert "ports" not in service
    assert {item["target"] for item in service["secrets"]} == {
        "TELELOOM_MCP_TOKEN",
        "TELELOOM_PERSONAL_API_ID",
        "TELELOOM_PERSONAL_API_HASH",
        "TELELOOM_PERSONAL_SESSION",
    }
    assert config["volumes"]["session_locks"]["name"] == "teleloom-session-locks"
