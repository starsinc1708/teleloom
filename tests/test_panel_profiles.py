import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from teleloom.cli import app as cli
from teleloom.config import Profile, Settings, TranscriptionConfig
from teleloom.daemon import create_application
from teleloom.panel import Panel
from teleloom.runtime import Runtime

TOKEN = "panel-profiles-owner-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def forbidden(*args):
    raise AssertionError("a panel page must not open a Telegram or AI connection")


class FakeAdapter:
    """Fake Telegram backend: one bounded read, never any content."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.delay = 0.0

    async def start(self) -> "FakeAdapter":
        return self

    async def diagnostic_read(self, chat: str) -> int:
        self.reads.append(chat)
        await asyncio.sleep(self.delay)
        return 1

    async def close(self) -> None:
        return None


def factory(adapters: list[FakeAdapter], delay: float = 0.0):
    def make(profile_id, profile, store, credentials):
        adapters.append(FakeAdapter())
        adapters[-1].delay = delay
        return adapters[-1]

    return make


@asynccontextmanager
async def real_owner(settings: Settings, make=forbidden):
    app = create_application(settings, TOKEN, make)
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


@asynccontextmanager
async def panel(settings: Settings, make=forbidden):
    runtime = Runtime(settings, adapter_factory=make)
    instance = Panel(settings, runtime, TOKEN)
    try:
        yield instance
    finally:
        await runtime.close()


async def login(client: httpx.AsyncClient, panel: Panel) -> str:
    issued = await client.post(
        "/ui/bootstrap", headers={"Authorization": "Bearer " + TOKEN, "Origin": panel.origin}
    )
    assert issued.status_code == 200, issued.text
    fragment = urlsplit(issued.json()["url"]).fragment
    accepted = await client.post(
        "/ui/session", json={"bootstrap": fragment}, headers={"Origin": panel.origin}
    )
    assert accepted.status_code == 204, accepted.text
    page = await client.get("/ui")
    assert page.status_code == 200
    return page.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def two_profiles(**overrides) -> dict[str, Profile]:
    profiles = {
        "alice": Profile(
            kind="user",
            identity={"id": "42", "username": "alice", "name": "<script>alert(1)</script>"},
            read_mode="selected",
            read_chats=["111"],
            send_chats=["111"],
            transcription=TranscriptionConfig(
                provider="groq", groq_model="private-model-identifier"
            ),
        ),
        "helper_bot": Profile(kind="bot", bot_backend="bot_api", identity={"id": "9"}),
    }
    profiles.update(overrides)
    return profiles


@pytest.mark.asyncio
async def test_profiles_show_identity_state_and_parity_with_existing_contract(tmp_path):
    settings = Settings(data_dir=tmp_path, port=free_port(), profiles=two_profiles())
    async with panel(settings, factory([])) as instance:
        await instance.runtime.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=instance), base_url=instance.origin
        ) as client:
            assert (await client.get("/ui/profiles")).status_code == 401
            await login(client, instance)

            api = await client.get("/ui/api/profiles")
            assert api.status_code == 200
            data = api.json()
            contract = {row["id"]: row for row in instance.runtime.profiles()["profiles"]}
            assert set(data) == {"tool_exposure", "profiles"}
            assert data["tool_exposure"] == instance.runtime.profiles()["tool_exposure"]
            for row in data["profiles"]:
                source = contract[row["id"]]
                assert row["kind"] == source["kind"] and row["backend"] == source["backend"]
                assert row["identity"] == source["identity"]
                assert row["proxy"] == source["proxy"]
                assert row["read_policy"] == source["capabilities"]["read_policy"]
                assert row["grants"] == source["grants"]
                assert row["state"] == "not_opened" and row["error"] is None
                assert "private-model-identifier" not in json.dumps(row["capabilities"])

            alice, bot = data["profiles"]
            assert alice["state"] == "not_opened" and alice["limits"] == []
            assert alice["capabilities"]["transcription_engines"]["default_provider"] == "groq"
            assert bot["limits"] and "saved updates only" in bot["limits"][0]

            instance.runtime.profile_errors["alice"] = "credentials_missing"
            failed = (await client.get("/ui/api/profiles")).json()["profiles"][0]
            assert failed["state"] == "credential"
            assert failed["error"] == "credentials_missing"
            assert failed["next_action"]

            instance.runtime.profile_errors["helper_bot"] = "webhook_conflict"
            rows = {
                row["id"]: row for row in (await client.get("/ui/api/profiles")).json()["profiles"]
            }
            assert rows["helper_bot"]["state"] == "error"
            assert "disable polling" in rows["helper_bot"]["next_action"]

            for code, action in (
                ("rate_limited", "Wait for Telegram"),
                ("invalid_proxy", "repair the local proxy"),
                ("connection_error", "Check connectivity"),
                ("https://example.invalid/private-session", "Inspect local configuration"),
            ):
                instance.runtime.profile_errors["helper_bot"] = code
                failed = (await client.get("/ui/api/profiles")).json()["profiles"][1]
                assert failed["state"] == "error"
                assert action in failed["next_action"]
                assert "private-session" not in json.dumps(failed)

            page = await client.get("/ui/profiles")
            assert page.status_code == 200
            assert "alice" in page.text and "helper_bot" in page.text
            assert "<script>alert(1)</script>" not in page.text
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
            assert "credentials_missing" in page.text
            assert str(tmp_path) not in page.text
            assert "<dt>Telegram history</dt><dd>Available</dd>" in page.text
            assert "<dt>Telegram history</dt><dd>Unavailable</dd>" in page.text
            assert "<dt>Scheduled send</dt><dd>Available</dd>" in page.text
            assert "<dt>Bot commands</dt><dd>Available</dd>" in page.text
            assert "<dt>Send</dt><dd>111</dd>" in page.text
            assert "read: 2 chat" not in page.text


@pytest.mark.asyncio
async def test_tool_catalogue_shows_hidden_tools_and_changes_nothing(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles=two_profiles(),
        exposure_mode="selected",
        exposed_tools=["server_status", "messages_get"],
    )
    async with panel(settings, factory([])) as instance:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=instance), base_url=instance.origin
        ) as client:
            assert (await client.get("/ui/api/tools")).status_code == 401
            await login(client, instance)

            data = (await client.get("/ui/api/tools")).json()
            tools = {tool["name"]: tool for tool in data["tools"]}
            assert data["mode"] == "selected" and data["selected_tools"] == [
                "messages_get",
                "server_status",
            ]
            assert tools["server_status"]["exposed"] is True
            assert tools["messages_get"]["exposed"] is True
            assert tools["delivery_execute"]["exposed"] is False
            assert data["exposed"] == 2 and data["count"] == len(tools) > 2
            assert tools["messages_get"]["needs_chat"] is True
            assert tools["messages_get"]["needs_profile"] is True

            filtered = (await client.get("/ui/api/tools", params={"q": "message"})).json()
            assert {tool["name"] for tool in filtered["tools"]} <= set(tools)
            assert "messages_get" in {tool["name"] for tool in filtered["tools"]}

            page = await client.get("/ui/tools", params={"profile": "helper_bot"})
            assert page.status_code == 200
            assert "delivery_execute" in page.text
            assert "Hidden by the owner&#x27;s tool exposure" in page.text
            assert "Saved updates only" in page.text
            send = (
                await client.get(
                    "/ui/tools",
                    params={
                        "profile": "helper_bot",
                        "q": "delivery_preview",
                    },
                )
            ).text
            assert "live history" not in send
            assert settings.exposed_tools == ["server_status", "messages_get"]
            assert settings.exposure_mode == "selected"
            csrf = await login(client, instance)
            form = {"profile_id": "alice", "chat_id": "111", "csrf": csrf}
            assert (
                "Selected read succeeded"
                in (
                    await client.post("/ui/probe", data=form, headers={"Origin": instance.origin})
                ).text
            )
            settings.exposed_tools.remove("messages_get")
            hidden = await client.post("/ui/probe", data=form, headers={"Origin": instance.origin})
            assert "tool_not_exposed" in hidden.text


@pytest.mark.asyncio
async def test_passive_pages_and_snapshot_start_no_connection(tmp_path):
    settings = Settings(data_dir=tmp_path, port=free_port(), profiles=two_profiles())
    async with panel(settings) as instance:
        await instance.runtime.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=instance), base_url=instance.origin
        ) as client:
            await login(client, instance)
            for path in ("/ui", "/ui/profiles", "/ui/tools", "/ui/api/profiles", "/ui/api/tools"):
                assert (await client.get(path)).status_code == 200, path
            assert instance.runtime.adapters == {}


@pytest.mark.asyncio
async def test_probe_needs_csrf_exact_chat_and_returns_no_content(tmp_path):
    adapters: list[FakeAdapter] = []
    settings = Settings(data_dir=tmp_path, port=free_port(), profiles=two_profiles())
    async with panel(settings, factory(adapters)) as instance:
        await instance.runtime.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=instance), base_url=instance.origin
        ) as client:
            csrf = await login(client, instance)
            form = {"profile_id": "alice", "chat_id": "111", "timeout_seconds": "3"}

            assert (
                await client.post("/ui/probe", data=form, headers={"Origin": instance.origin})
            ).status_code == 403
            assert adapters == []

            assert (
                await client.post(
                    "/ui/probe",
                    data={**form, "csrf": csrf},
                    headers={"Origin": "https://evil.invalid"},
                )
            ).status_code == 403
            assert adapters == []

            denied = await client.post(
                "/ui/probe",
                data={**form, "csrf": csrf, "chat_id": "222"},
                headers={"Origin": instance.origin},
            )
            assert denied.status_code == 200
            assert "read_not_allowed" in denied.text and adapters == []

            allowed = await client.post(
                "/ui/probe",
                data={**form, "csrf": csrf},
                headers={"Origin": instance.origin},
            )
            assert allowed.status_code == 200
            assert "Selected read succeeded" in allowed.text
            assert "Returned 1 message" in allowed.text
            assert str(tmp_path) not in allowed.text and TOKEN not in allowed.text
            assert len(adapters) == 1 and adapters[0].reads == ["111"]
            assert "<dt>Returned</dt><dd>1 message</dd>" in allowed.text


@pytest.mark.asyncio
async def test_probe_reports_bot_limit_and_bounded_timeout(tmp_path):
    adapters: list[FakeAdapter] = []
    settings = Settings(data_dir=tmp_path, port=free_port(), profiles=two_profiles())
    async with panel(settings, factory(adapters, delay=2)) as instance:
        await instance.runtime.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=instance), base_url=instance.origin
        ) as client:
            csrf = await login(client, instance)

            bot = await client.post(
                "/ui/probe",
                data={"profile_id": "helper_bot", "chat_id": "111", "csrf": csrf},
                headers={"Origin": instance.origin},
            )
            assert "unsupported_capability" in bot.text
            assert "Choose a user profile for live history timing" in bot.text
            assert adapters == []

            started = asyncio.get_running_loop().time()
            slow = await client.post(
                "/ui/probe",
                data={
                    "profile_id": "alice",
                    "chat_id": "111",
                    "timeout_seconds": "0.05",
                    "csrf": csrf,
                },
                headers={"Origin": instance.origin},
            )
            assert asyncio.get_running_loop().time() - started < 1.5
            assert "<code>timeout</code>" in slow.text
            assert "Wait for connection cleanup" in slow.text
            assert "<dt>Returned</dt><dd>—</dd>" in slow.text
            assert len(adapters) == 1


@pytest.mark.asyncio
async def test_mounted_panel_keeps_guard_csp_and_mcp_boundary(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, port=free_port(), profiles=two_profiles())
    settings.save()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", TOKEN)
    async with real_owner(settings):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.port}") as client:
            assert (await client.get("/ui/api/profiles")).status_code == 401
            assert (await client.get("/ui/api/tools")).status_code == 401
            assert (
                await client.get("/ui/profiles", headers={"Host": "localhost"})
            ).status_code == 403
            assert (await client.get("/ui/probe")).status_code == 405
            assert (
                await client.post(
                    "/ui/probe",
                    data={"profile_id": "alice", "chat_id": "111"},
                    headers={"Origin": "https://evil.invalid"},
                )
            ).status_code == 403

            issued = await client.post(
                "/ui/bootstrap",
                headers={
                    "Authorization": "Bearer " + TOKEN,
                    "Origin": f"http://127.0.0.1:{settings.port}",
                },
            )
            assert issued.status_code == 200
            opened = await client.post(
                "/ui/session",
                json={"bootstrap": urlsplit(issued.json()["url"]).fragment},
                headers={"Origin": f"http://127.0.0.1:{settings.port}"},
            )
            assert opened.status_code == 204

            page = await client.get("/ui/profiles")
            assert page.status_code == 200
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
            assert page.headers["cache-control"] == "no-store"
            assert TOKEN not in page.text and str(tmp_path) not in page.text
            tools = await client.get("/ui/api/tools")
            assert tools.status_code == 200 and tools.json()["count"] > 0
            assert (await client.get("/mcp")).status_code == 401

            result = await asyncio.to_thread(CliRunner().invoke, cli, ["doctor", "--live"])
            assert result.exit_code == 0, result.output
            report = json.loads(result.output)
            profiles = (await client.get("/ui/api/profiles")).json()
            assert profiles["tool_exposure"] == report["live"]["data"]["tool_exposure"]
            for row, source in zip(
                profiles["profiles"], report["live"]["data"]["profiles"], strict=True
            ):
                assert all(
                    row[key] == source[key] for key in ("identity", "backend", "proxy", "grants")
                )
                assert all(
                    source["capabilities"][key] == value
                    for key, value in row["capabilities"].items()
                    if key != "transcription_engines"
                )
                assert (
                    row["capabilities"]["transcription_engines"]["default_provider"]
                    == source["capabilities"]["transcription_engines"]["default_provider"]
                )
                assert row["state"] == "not_opened" and not source["connected"]


@pytest.mark.asyncio
async def test_chromium_profile_probe_does_not_replay_on_reload(tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    settings = Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles=two_profiles(),
        exposure_mode="selected",
        exposed_tools=["messages_get", "profiles_list"],
    )
    adapters: list[FakeAdapter] = []
    origin = f"http://127.0.0.1:{settings.port}"
    async with (
        real_owner(settings, factory(adapters, delay=1.2)),
        playwright.async_playwright() as pw,
    ):
        browser = await pw.chromium.launch(executable_path=os.getenv("TELELOOM_TEST_CHROMIUM"))
        try:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(error))
            issued = await page.request.post(
                origin + "/ui/bootstrap", headers={"Authorization": "Bearer " + TOKEN}
            )
            assert issued.ok
            await page.goto((await issued.json())["url"])
            await playwright.expect(
                page.get_by_role("heading", name="Workspace status")
            ).to_be_visible()
            profile_page = await page.goto(origin + "/ui/profiles")
            assert profile_page is not None and profile_page.ok
            await playwright.expect(page.get_by_text("Not opened yet", exact=False)).to_have_count(
                2
            )
            assert "<script>alert(1)</script>" in await page.locator("main").inner_text()
            await page.get_by_text("Backend capabilities", exact=True).first.press("Enter")
            await playwright.expect(
                page.get_by_text("Telegram history", exact=True).first
            ).to_be_visible()
            await page.goto(origin + "/ui/tools")
            await page.get_by_label("Search tools").fill("messages_get")
            await page.get_by_label("Explain limits for").select_option("helper_bot")
            await page.get_by_role("button", name="Filter").press("Enter")
            await playwright.expect(
                page.get_by_text("Saved updates only", exact=False)
            ).to_be_visible()
            assert adapters == []

            await page.goto(origin + "/ui/profiles")
            await page.get_by_label("Profile", exact=True).select_option("alice")
            chat = page.get_by_label("Chat ID", exact=False)
            await chat.fill("222")
            probe = page.get_by_role("button", name="Run bounded probe")
            await probe.press("Enter")
            await playwright.expect(
                page.get_by_text("read_not_allowed", exact=True)
            ).to_be_visible()
            assert adapters == []
            await page.get_by_label("Profile", exact=True).select_option("alice")
            await chat.fill("111")
            await page.get_by_label("Timeout").select_option("1")
            await page.evaluate("""() => {
                const status = document.querySelector('#probe-status');
                new MutationObserver(() => {
                    if (status.textContent === 'Running one bounded read…') {
                        window.probeLoadingSeen = true;
                    }
                }).observe(status, {childList: true});
            }""")
            await probe.press("Enter")
            await playwright.expect(page.get_by_text("timeout", exact=True)).to_be_visible()
            assert await page.evaluate("window.probeLoadingSeen === true")
            assert len(adapters) == 1 and adapters[0].reads == ["111"]
            assert page.url == origin + "/ui/profiles"
            await page.reload()
            await playwright.expect(
                page.get_by_role("heading", name="Last probe result")
            ).to_have_count(0)
            assert len(adapters) == 1 and adapters[0].reads == ["111"]
            assert errors == []
        finally:
            await browser.close()
