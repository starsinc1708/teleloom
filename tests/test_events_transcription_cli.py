import asyncio
import json
import os
import socket
import sys
from datetime import UTC, datetime

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telethon import events, types
from typer.testing import CliRunner

from teleloom.adapters import UserAdapter
from teleloom.cli import app
from teleloom.config import Profile, Settings
from teleloom.daemon import create_application
from tests.fakes import data
from tests.telegram_fakes import CHAT, SDK


def test_owner_cli_explicit_chat_grants_models_and_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    settings.save()
    runner = CliRunner()
    for scope in ("event", "transcription", "transcription_external"):
        result = runner.invoke(app, ["profile", "allow", "--scope", scope, "--", "personal", CHAT])
        assert result.exit_code == 0, result.output
    configured = runner.invoke(
        app,
        [
            "profile",
            "transcription",
            "personal",
            "--openai-endpoint",
            "https://transcribe.invalid/v1",
            "--external-daily-calls",
            "2",
            "--external-daily-bytes",
            "100",
        ],
    )
    assert configured.exit_code == 0, configured.output
    profile = Settings.load().profile("personal")
    assert (
        profile.event_chats
        == profile.transcription_chats
        == profile.transcription_external_chats
        == [CHAT]
    )
    assert profile.transcription.external_daily_calls == 2
    assert profile.transcription.external_daily_bytes == 100
    assert profile.send_chats == []
    revoked = runner.invoke(
        app, ["profile", "allow", "--scope", "event", "--remove", "--", "personal", CHAT]
    )
    assert revoked.exit_code == 0, revoked.output
    assert Settings.load().profile("personal").event_chats == []


@pytest.mark.asyncio
async def test_stdio_wait_and_cli_results_share_real_socket_owner(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(
        data_dir=tmp_path,
        port=port,
        profiles={"personal": Profile(kind="user", event_chats=[CHAT])},
    )
    settings.save()
    token = "fictional-event-owner"
    sdk = SDK()
    monkeypatch.setattr("teleloom.adapters.user_client", lambda *args, **kwargs: sdk)
    adapters = []

    def factory(*args):
        adapter = UserAdapter(*args)
        adapters.append(adapter)
        return adapter

    owner = create_application(settings, token, factory)
    server = uvicorn.Server(
        uvicorn.Config(owner, host="127.0.0.1", port=port, log_level="critical", access_log=False)
    )
    task = asyncio.create_task(server.serve())
    env = {**os.environ, "TELELOOM_DATA_DIR": str(tmp_path), "TELELOOM_MCP_TOKEN": token}
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "teleloom", "mcp"], env=env
    )
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert {"events_wait_start", "transcription_start", "transcription_capabilities"} <= {
                tool.name for tool in (await session.list_tools()).tools
            }
            start = data(
                await session.call_tool(
                    "events_wait_start",
                    {
                        "profile_id": "personal",
                        "chat_ids": [CHAT],
                        "timeout_seconds": 5,
                        "filter": {"sender_id": "7", "kinds": ["new"]},
                    },
                )
            )
            assert start["ok"], start
            await adapters[0]._message_event(
                events.NewMessage.Event(
                    types.Message(
                        id=77,
                        peer_id=types.PeerChannel(100),
                        date=datetime.now(UTC),
                        message="CLI evidence",
                        from_id=types.PeerUser(7),
                    )
                )
            )
            await asyncio.sleep(0.3)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "teleloom",
                "call",
                "jobs_results",
                "--args",
                json.dumps({"profile_id": "personal", "job_id": start["data"]["job_id"]}),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
                assert process.returncode == 0, stderr.decode()
                result = json.loads(stdout)
                assert result["data"]["items"][0]["id"] == "77"
                assert result["data"]["coverage"]["reason"] == "new"
                assert result["data"]["coverage"]["filter"] == {"sender_id": "7", "kinds": ["new"]}
                assert result["data"]["coverage"]["next_cursor"]
                assert result["data"]["items"][0]["observed_at"]
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
