import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn

from teleloom.config import Profile, Settings
from teleloom.daemon import create_application
from teleloom.panel import Panel
from teleloom.runtime import Runtime
from teleloom.status import status_snapshot

TOKEN = "panel-test-owner-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def origin(client: httpx.AsyncClient) -> str:
    return str(client.base_url).rstrip("/")


def forbidden(*args):
    raise AssertionError("the panel must not open a Telegram or AI connection")


@asynccontextmanager
async def panel_app(settings: Settings):
    runtime = Runtime(settings, adapter_factory=forbidden)
    panel = Panel(settings, runtime, TOKEN)
    try:
        yield panel
    finally:
        await runtime.close()


@asynccontextmanager
async def real_owner(settings: Settings):
    app = create_application(settings, TOKEN, forbidden)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=settings.port, log_level="critical", access_log=False
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


async def issue(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/ui/bootstrap", headers={"Authorization": "Bearer " + TOKEN, "Origin": origin(client)}
    )
    assert response.status_code == 200, response.text
    assert "url" in response.json() and "expires_at" in response.json()
    return response.json()["url"]


async def login(client: httpx.AsyncClient, url: str) -> None:
    fragment = urlsplit(url).fragment
    assert fragment
    response = await client.post(
        "/ui/session", json={"bootstrap": fragment}, headers={"Origin": origin(client)}
    )
    assert response.status_code == 204, response.text


@pytest.mark.asyncio
async def test_bootstrap_is_one_time_and_session_has_strict_cookie_csrf_and_logout(tmp_path):
    settings = Settings(data_dir=tmp_path, port=free_port())
    async with panel_app(settings) as panel:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel), base_url=panel.origin
        ) as client:
            passive = await client.get("/ui")
            assert passive.status_code == 401
            assert panel.cookie not in passive.headers.get("set-cookie", "")
            assert "teleloom ui" in passive.text

            url = await issue(client)
            assert "/ui/open#" in url and TOKEN not in url
            assert (await client.get("/ui/open")).headers["referrer-policy"] == "no-referrer"
            fragment = urlsplit(url).fragment

            rejected = await client.post(
                "/ui/session",
                json={"bootstrap": fragment},
                headers={"Origin": "https://evil.invalid"},
            )
            assert rejected.status_code == 403

            accepted = await client.post(
                "/ui/session", json={"bootstrap": fragment}, headers={"Origin": panel.origin}
            )
            assert accepted.status_code == 204
            cookie = accepted.headers["set-cookie"].lower()
            assert "httponly" in cookie and "samesite=strict" in cookie and "path=/ui" in cookie
            assert panel.cookie in accepted.headers["set-cookie"]
            assert "teleloom_auth" not in cookie and TOKEN not in cookie

            page = await client.get("/ui")
            assert page.status_code == 200
            assert "Workspace status" in page.text and "Snapshot time" in page.text
            assert "no-store" in page.headers["cache-control"]
            assert page.headers["referrer-policy"] == "same-origin"
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
            assert TOKEN not in page.text and str(tmp_path) not in page.text

            replay = await client.post(
                "/ui/session", json={"bootstrap": fragment}, headers={"Origin": panel.origin}
            )
            assert replay.status_code == 401

            csrf = page.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
            denied = await client.post(
                "/ui/logout", data={"csrf": "wrong"}, headers={"Origin": panel.origin}
            )
            assert denied.status_code == 403
            logout = await client.post(
                "/ui/logout", data={"csrf": csrf}, headers={"Origin": panel.origin}
            )
            assert logout.status_code == 303
            assert panel.cookie in logout.headers.get("set-cookie", "")
            assert (await client.get("/ui")).status_code == 401


@pytest.mark.asyncio
async def test_expired_and_foreign_bootstraps_are_rejected(tmp_path):
    settings = Settings(data_dir=tmp_path, port=free_port())
    clock = [0.0]
    runtime = Runtime(settings, adapter_factory=forbidden)
    panel = Panel(settings, runtime, TOKEN, clock=lambda: clock[0])
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel), base_url=panel.origin
        ) as client:
            fragment = urlsplit(await issue(client)).fragment
            clock[0] = 61
            expired = await client.post(
                "/ui/session", json={"bootstrap": fragment}, headers={"Origin": panel.origin}
            )
            assert expired.status_code == 401
    finally:
        await runtime.close()

    settings = Settings(data_dir=tmp_path, port=free_port())
    async with panel_app(settings) as first, panel_app(settings) as second:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url=first.origin
        ) as browser:
            await login(browser, await issue(browser))
            stolen = browser.cookies.get(first.cookie)
            assert stolen
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second), base_url=second.origin
            ) as other:
                other.cookies.set(first.cookie, stolen, path="/ui")
                assert (await other.get("/ui")).status_code == 401


@pytest.mark.asyncio
async def test_real_http_host_origin_and_mutation_routes(tmp_path):
    settings = Settings(data_dir=tmp_path, port=free_port())
    async with real_owner(settings):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.port}") as client:
            assert (await client.get("/ui")).status_code == 401
            assert (await client.get("/ui", headers={"Host": "localhost"})).status_code == 403
            assert (
                await client.get("/ui", headers={"Origin": "https://evil.invalid"})
            ).status_code == 403
            assert (await client.post("/ui/bootstrap")).status_code == 401
            assert (await client.get("/ui/bootstrap")).status_code == 405
            assert (await client.get("/ui/unknown")).status_code == 404
            assert (
                await client.post(
                    "/ui/session",
                    content=b"{}",
                    headers={"Origin": f"http://127.0.0.1:{settings.port}"},
                )
            ).status_code == 415
            assert (
                await client.post(
                    "/ui/session",
                    json={"bootstrap": "x"},
                    headers={"Origin": f"http://127.0.0.1:{settings.port}"},
                )
            ).status_code == 401
            # The panel cookie is never an MCP credential.
            assert (await client.get("/mcp")).status_code == 401


@pytest.mark.asyncio
async def test_status_page_reports_empty_and_error_states_with_build_parity(tmp_path):
    settings = Settings(data_dir=tmp_path, port=free_port())
    settings.save()
    async with panel_app(settings) as panel:
        assert status_snapshot(settings, panel.runtime)["state"] == "starting"
        await panel.runtime.start()
        snapshot = status_snapshot(settings, panel.runtime)
        assert snapshot["state"] == "empty" and snapshot["profiles"] == 0
        assert snapshot["build"]["package_version"] == snapshot["version"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel), base_url=panel.origin
        ) as client:
            await login(client, await issue(client))
            page = await client.get("/ui")
            assert page.status_code == 200
            assert 'data-state="empty"' in page.text
            assert "No profiles are configured" in page.text
            assert snapshot["build"]["build_id"] in page.text
            assert '<time datetime="' in page.text and "+00:00" in page.text

    settings = Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles={"work": Profile(kind="user", identity={"phone": "private-phone"})},
    )
    async with panel_app(settings) as panel:
        await panel.runtime.start()
        panel.runtime.profile_errors["work"] = "connection_error"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel), base_url=panel.origin
        ) as client:
            await login(client, await issue(client))
            page = await client.get("/ui")
            assert 'data-state="error"' in page.text
            assert "connection_error" in page.text and "<code>work</code>" in page.text
            assert "private-phone" not in page.text


@pytest.mark.asyncio
async def test_open_page_grants_nothing_and_has_no_session_cookie(tmp_path):
    settings = Settings(data_dir=tmp_path, port=free_port())
    async with panel_app(settings) as panel:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel), base_url=panel.origin
        ) as client:
            opened = await client.get("/ui/open")
            assert opened.status_code == 200
            assert "script-src 'nonce-" in opened.headers["content-security-policy"]
            assert panel.cookie not in opened.headers.get("set-cookie", "")
            assert (await client.get("/ui")).status_code == 401


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


async def run_cli(args: list[str], env: dict[str, str]) -> ProcessResult:
    process = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return ProcessResult(process.returncode or 0, stdout.decode(), stderr.decode())


@pytest.mark.process_e2e
def test_installed_ui_cli_issues_one_bootstrap_over_real_http(tmp_path):
    port = free_port()
    Settings(data_dir=tmp_path, port=port).save()
    env = {
        **os.environ,
        "TELELOOM_DATA_DIR": str(tmp_path),
        "TELELOOM_MCP_TOKEN": "isolated-panel-cli-token",
    }
    ui = asyncio.run(run_cli([sys.executable, "-m", "teleloom", "ui", "--no-open"], env))
    assert ui.returncode == 0, ui.stderr
    payload = json.loads(ui.stdout)
    assert payload["opened"] is False
    assert payload["url"].startswith(f"http://127.0.0.1:{port}/ui/open#")
    assert "isolated-panel-cli-token" not in ui.stdout
    try:

        async def health_owner() -> str:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                response = await client.get(
                    "/health", headers={"Authorization": "Bearer isolated-panel-cli-token"}
                )
                return response.json()["owner_id"]

        first_owner = asyncio.run(health_owner())
        second = asyncio.run(run_cli([sys.executable, "-m", "teleloom", "ui", "--no-open"], env))
        assert second.returncode == 0, second.stderr
        second_payload = json.loads(second.stdout)
        assert second_payload["url"] != payload["url"]
        payload = second_payload

        doctor = asyncio.run(
            run_cli([sys.executable, "-m", "teleloom", "doctor", "--mode", "mcp"], env)
        )
        assert doctor.returncode == 0, doctor.stderr
        report = json.loads(doctor.stdout)
        assert report["daemon"]["status"] == "running"
        owner_id = report["daemon"]["owner_id"]
        assert owner_id == first_owner
        assert report["daemon"]["snapshot_at"]
        assert report["daemon"]["health"]["local"] == "ok"

        async def browse() -> None:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                assert (await client.get("/ui")).status_code == 401
                response = await client.post(
                    "/ui/session",
                    json={"bootstrap": urlsplit(payload["url"]).fragment},
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                assert response.status_code == 204
                page = await client.get("/ui")
                assert page.status_code == 200
                assert owner_id in page.text
                assert report["daemon"]["build"]["build_id"] in page.text
                assert report["daemon"]["workspace_id"] in page.text
                assert report["daemon"]["profiles"] == 0
                assert "Profiles</dt><dd>0</dd>" in page.text
                assert "Local readiness</dt><dd>ok</dd>" in page.text
                assert str(tmp_path) not in page.text
                assert "isolated-panel-cli-token" not in page.text
                assert (await client.get("/mcp")).status_code == 401

        asyncio.run(browse())
    finally:
        stopped = asyncio.run(run_cli([sys.executable, "-m", "teleloom", "stop"], env))
        assert stopped.returncode == 0, stopped.stderr
