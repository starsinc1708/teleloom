"""SSH-tunnel contract for the owner panel (issue #32).

The forwarder is a disposable loopback TCP proxy; the "remote" owner binds a
second loopback address so the local port can equal the remote port exactly,
as the documented OpenSSH command requires.
"""

import asyncio
import os
import re
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

from teleloom.build_info import build_info
from teleloom.config import Settings
from teleloom.daemon import create_application
from teleloom.runtime import fingerprint

TOKEN = "ssh-tunnel-test-token"
DOCS = Path(__file__).resolve().parents[1] / "docs" / "gui-ssh.md"
HOST_KEY_BYPASSES = (
    "StrictHostKeyChecking=no",
    "UserKnownHostsFile=/dev/null",
    "UpdateHostKeys=no",
)


def forbidden(*args):
    raise AssertionError("the panel must not open a Telegram or AI connection")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def documented(marker: str) -> str:
    return (
        DOCS.read_text(encoding="utf-8")
        .split(marker, 1)[1]
        .split("```bash\n", 1)[1]
        .split("```", 1)[0]
    )


@asynccontextmanager
async def forward(local_port: int, target: tuple[str, int]):
    handlers: set[asyncio.Task] = set()

    async def pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async def handle(reader, writer):
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        remote_reader, remote_writer = await asyncio.open_connection(*target)
        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))

    server = await asyncio.start_server(handle, "127.0.0.1", local_port)
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()
        for task in handlers:
            task.cancel()


@asynccontextmanager
async def remote_owner(settings: Settings):
    app = create_application(settings, TOKEN, forbidden)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.2", port=settings.port, log_level="critical", access_log=False
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield app
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


def test_documented_ssh_flow_is_loopback_equal_port_and_carries_no_secret_in_argv():
    forward = documented("<!-- test:ssh-forward -->")
    words = forward.split()
    assert "ssh" in words and "-N" in words
    assert "ExitOnForwardFailure=yes" in words
    assert not set(HOST_KEY_BYPASSES) & set(words)
    assert "-R" not in words and "-g" not in words and "0.0.0.0" not in forward

    match = re.search(r"-L (\S+)", forward)
    assert match is not None
    bind, local, target, remote = match.group(1).split(":")
    assert bind == target == "127.0.0.1"
    assert local == remote, "the forwarded local port must equal the remote owner port"

    bootstrap = documented("<!-- test:ssh-bootstrap -->")
    assert "ssh " in bootstrap and "ui --no-open" in bootstrap
    assert "TELELOOM_DATA_DIR=/srv/teleloom/data" in bootstrap and ".venv/bin/teleloom" in bootstrap
    assert "umask 077" in bootstrap
    assert "ui/open#" not in bootstrap, "the bootstrap URL must not be an ssh argument"


@pytest.mark.parametrize(
    ("ssh_result", "browser_result", "expected_browser"),
    [(0, 0, True), (255, 0, False), (0, 7, True)],
)
def test_documented_bootstrap_cleans_temporary_url(
    tmp_path, ssh_result, browser_result, expected_browser
):
    shell = os.environ.get("TELELOOM_TEST_BASH") or shutil.which("sh")
    if shell is None:
        pytest.skip("A POSIX shell is required to execute the SSH runbook")
    (tmp_path / "home").mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    fixture = f"""
export HOME="$PWD/home" TMPDIR="$PWD/temp"
ssh() {{
  test "$#" = 2 && test "$1" = vps-owner || return 99
  printf '%s' '{{"url":"http://127.0.0.1:8765/ui/open#fake-bootstrap"}}'
  return {ssh_result}
}}
python3() {{
  test "$1" = -c && test -f "$3" && grep -q fake-bootstrap "$3" || return 98
  touch browser-called
  return {browser_result}
}}
"""
    result = subprocess.run(
        [shell, "-s"],
        cwd=tmp_path,
        input=fixture + documented("<!-- test:ssh-bootstrap -->"),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == (ssh_result or browser_result)
    assert (tmp_path / "browser-called").exists() is expected_browser
    assert not list(temp.iterdir())
    assert "fake-bootstrap" not in result.stdout + result.stderr


@pytest.mark.asyncio
async def test_forwarded_owner_keeps_exact_host_and_tunnel_loss_is_not_owner_failure(tmp_path):
    port = free_port()
    settings = Settings(data_dir=tmp_path, port=port, exposure_mode="selected")
    async with remote_owner(settings):
        target = ("127.0.0.2", port)
        async with forward(port, target) as tunnel:
            assert tunnel.sockets[0].getsockname()[:2] == ("127.0.0.1", port)
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(base_url=base) as browser:
                assert (await browser.get("/ui")).status_code == 401
                mismatch = await browser.get("/ui", headers={"Host": f"127.0.0.1:{port + 1}"})
                assert mismatch.status_code == 403
                issued = await browser.post(
                    "/ui/bootstrap", headers={"Authorization": "Bearer " + TOKEN}
                )
                assert issued.status_code == 200, issued.text
                fragment = issued.json()["url"].split("#", 1)[1]
                login = await browser.post(
                    "/ui/session", json={"bootstrap": fragment}, headers={"Origin": base}
                )
                assert login.status_code == 204, login.text
                page = await browser.get("/ui")
                assert page.status_code == 200
                assert "Workspace status" in page.text and str(tmp_path) not in page.text
                assert fingerprint(str(tmp_path.resolve())) in page.text
                assert build_info()["build_id"] in page.text

                replay = await browser.post(
                    "/ui/session", json={"bootstrap": fragment}, headers={"Origin": base}
                )
                assert replay.status_code == 401 and replay.json()["error"] == "invalid_bootstrap"

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as local:
            with pytest.raises(httpx.ConnectError):
                await local.get("/ui")

        # The tunnel is gone; the owner still answers when the forwarded Host is named.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.2:{port}") as direct:
            health = await direct.get(
                "/health",
                headers={
                    "Authorization": "Bearer " + TOKEN,
                    "Host": f"127.0.0.1:{port}",
                },
            )
            assert health.status_code == 200
            assert health.json()["owner_id"]


@pytest.mark.asyncio
async def test_busy_local_port_fails_before_any_bootstrap(tmp_path):
    port = free_port()
    settings = Settings(data_dir=tmp_path, port=port)
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        with pytest.raises(OSError):
            async with forward(port, ("127.0.0.2", port)):
                pass
        async with remote_owner(settings):
            async with httpx.AsyncClient(base_url=f"http://127.0.0.2:{port}") as direct:
                probe = await direct.post("/ui/bootstrap", headers={"Host": f"127.0.0.1:{port}"})
                assert probe.status_code == 401
    finally:
        holder.close()
