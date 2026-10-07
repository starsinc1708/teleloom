import json
import os
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teleloom.cli import app
from teleloom.config import Settings

pytestmark = pytest.mark.process_e2e


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows GUI process behavior")
@pytest.mark.parametrize("mode", ["mcp", "cli"])
def test_gui_client_and_auto_started_daemon_do_not_allocate_a_console(tmp_path, monkeypatch, mode):
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    assert pythonw.is_file(), "Windows test environment requires a GUI Python executable."
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    directory = tmp_path / "data"
    Settings(data_dir=directory, port=port).save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(directory))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "isolated-window-test-token")
    fragment = CliRunner().invoke(app, ["config", "client", "--client", "codex"])
    assert fragment.exit_code == 0, fragment.output
    entry = tomllib.loads(fragment.output)["mcp_servers"]["teleloom"]
    command = (
        [entry["command"], *entry["args"]] if mode == "mcp" else [sys.executable, "-m", "teleloom"]
    )
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"mode": mode, "command": command}), encoding="utf-8")
    output = tmp_path / "result.json"
    probe = subprocess.run(
        [
            str(pythonw),
            str(Path(__file__).with_name("windows_gui_probe.py")),
            str(output),
            str(request),
        ],
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    assert probe.returncode == 0 and output.is_file()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert "failure" not in result, result
    assert result["public_operation"] == "passed"
    assert result["console_pids"] == [], f"Background launch allocated console(s): {result}"
