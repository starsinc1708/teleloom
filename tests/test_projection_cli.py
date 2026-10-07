import asyncio
import json
import os
import socket
import sys
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import uvicorn
from typer.testing import CliRunner

from teleloom.cli import app
from teleloom.config import Profile, Settings
from teleloom.daemon import create_application
from teleloom.models import Message
from tests.fakes import TelegramAPI

pytestmark = pytest.mark.process_e2e


def test_call_rejects_projection_flags_that_duplicate_json_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path / "unused-owner"))
    result = CliRunner().invoke(
        app,
        [
            "call",
            "messages_get",
            "--args",
            '{"fields":["text"]}',
            "--field",
            "sender_name",
        ],
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"]["code"] == "invalid_arguments"
    assert not (tmp_path / "unused-owner").exists()


@pytest.mark.parametrize(
    ("arguments", "flags"),
    [
        ({"preset": "full"}, ["--preset", "digest"]),
        ({"fields": ["text"]}, ["--preset", "digest"]),
        ({"preset": "digest"}, ["--field", "text"]),
        ({}, ["--field", "text", "--preset", "digest"]),
    ],
)
def test_call_rejects_conflicting_projection_sources_in_args_file(
    tmp_path, monkeypatch, arguments, flags
):
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path / "unused-owner"))
    source = tmp_path / "arguments.json"
    source.write_text(json.dumps(arguments), encoding="utf-8")
    result = CliRunner().invoke(app, ["call", "messages_get", "--args-file", str(source), *flags])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"]["code"] == "invalid_arguments"
    assert not (tmp_path / "unused-owner").exists()


class ProjectionTelegramAPI(TelegramAPI):
    def __init__(self, profile, config, store, credentials):
        super().__init__(profile, config, store, credentials)
        self.rows = [
            Message(
                profile_id=profile,
                chat_id="100",
                id="5",
                date=datetime(2026, 10, 5, 8, tzinfo=UTC),
                text="Ship the agreed fix today.",
                link="https://t.me/example/5",
                sender_id="42",
                sender_name="Alex",
                views=700,
                reactions=[{"emoji": "👍", "count": 4}],
            )
        ]


@pytest_asyncio.fixture
async def cli_owner(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(data_dir=tmp_path, port=port, profiles={"personal": Profile(kind="user")})
    settings.save()
    token = "isolated-projection-cli-token"
    owner = create_application(settings, token, ProjectionTelegramAPI)
    server = uvicorn.Server(
        uvicorn.Config(owner, host="127.0.0.1", port=port, log_level="critical", access_log=False)
    )
    task = asyncio.create_task(server.serve())

    async def ready():
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)

    async def invoke(*arguments):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "teleloom",
            *arguments,
            env={
                **os.environ,
                "TELELOOM_DATA_DIR": str(tmp_path),
                "TELELOOM_MCP_TOKEN": token,
                "PYTHONIOENCODING": "utf-8",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        return process.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")

    try:
        await asyncio.wait_for(ready(), timeout=10)
        yield invoke
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_fields_command_selects_digest_evidence_through_real_owner(cli_owner):
    code, stdout, stderr = await cli_owner(
        "fields", "messages_get", "--request", "Сделай дайджест за 48 часов"
    )
    assert code == 0, stderr
    selection = json.loads(stdout)
    assert selection["ok"]
    assert selection["data"]["status"] == "disabled"
    assert "text" in selection["data"]["fields"]
    assert "reactions" in selection["data"]["omitted"]
    assert {"profile_id", "chat_id", "id", "date", "link"} <= set(selection["data"]["required"])


@pytest.mark.asyncio
async def test_call_flags_reduce_real_read_json_and_full_preserves_compatibility(
    cli_owner, tmp_path
):
    source = tmp_path / "read.json"
    source.write_text('{"profile_id":"personal","chat_id":"100"}', encoding="utf-8")
    base_args = ("call", "messages_get", "--args-file", str(source))
    code, full_stdout, stderr = await cli_owner(*base_args)
    assert code == 0, stderr
    full = json.loads(full_stdout)
    assert full["data"]["items"][0]["reactions"] == [{"emoji": "👍", "count": 4}]

    code, projected_stdout, stderr = await cli_owner(
        *base_args, "--field", "text", "--field", "sender_name"
    )
    assert code == 0, stderr
    projected = json.loads(projected_stdout)
    row = projected["data"]["items"][0]
    assert row["text"] == "Ship the agreed fix today."
    assert row["sender_name"] == "Alex"
    assert row["date"] == "2026-10-05T08:00:00Z"
    assert row["link"] == "https://t.me/example/5"
    assert row["sender_id"] == "42"
    assert "reactions" not in row and "views" not in row
    assert projected["data"]["coverage"] == full["data"]["coverage"]
    assert projected["data"]["source"] == full["data"]["source"]
    assert projected["data"]["next_cursor"] == full["data"]["next_cursor"]
    assert len(projected_stdout) < len(full_stdout)

    code, stdout, stderr = await cli_owner(*base_args, "--preset", "full")
    assert code == 0, stderr
    assert json.loads(stdout) == full


@pytest.mark.asyncio
async def test_fields_command_respects_explicit_preset_even_with_jev_opt_in(cli_owner):
    code, stdout, stderr = await cli_owner(
        "fields",
        "messages_get",
        "--request",
        "Show reaction counts",
        "--use-jev",
        "--preset",
        "digest",
    )
    assert code == 0, stderr
    selection = json.loads(stdout)["data"]
    assert selection["status"] == "explicit"
    assert "text" in selection["fields"]
    assert "reactions" in selection["omitted"]


@pytest.mark.asyncio
async def test_call_forwards_repeatable_fields_to_selector(cli_owner):
    code, stdout, stderr = await cli_owner(
        "call",
        "response_fields_select",
        "--args",
        '{"tool_name":"messages_get","request":"Show engagement"}',
        "--field",
        "text",
        "--field",
        "sender_name",
    )
    assert code == 0, stderr
    selection = json.loads(stdout)["data"]
    assert selection["status"] == "explicit"
    assert {"text", "sender_name"} <= set(selection["fields"])
    assert "reactions" in selection["omitted"]
