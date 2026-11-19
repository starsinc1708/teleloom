import asyncio
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn

from teleloom.config import Profile, Settings
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
        return 1710000000

    async def close(self) -> None:
        return None


def factory(adapters: list[FakeAdapter], delay: float = 0.0):
    def make(profile_id, profile, store, credentials):
        adapters.append(FakeAdapter())
        adapters[-1].delay = delay
        return adapters[-1]

    return make


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
                assert row["read_policy"] == source["capabilities"]["read_policy"]
                assert row["grants"] == source["grants"]
                assert row["state"] == "not_opened" and row["error"] is None
                assert "transcription_engines" not in row["capabilities"]

            alice, bot = data["profiles"]
            assert alice["state"] == "not_opened" and alice["limits"] == []
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

            page = await client.get("/ui/profiles")
            assert page.status_code == 200
            assert "alice" in page.text and "helper_bot" in page.text
            assert "<script>alert(1)</script>" not in page.text
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
            assert "credentials_missing" in page.text
            assert str(tmp_path) not in page.text
            assert "Telegram history" not in page.text or "saved updates only" in page.text


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
            assert "saved updates only, so live history reads are unavailable" in page.text
            assert settings.exposed_tools == ["server_status", "messages_get"]
            assert settings.exposure_mode == "selected"


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
            assert "no content is shown" in allowed.text
            assert str(tmp_path) not in allowed.text and TOKEN not in allowed.text
            assert len(adapters) == 1 and adapters[0].reads == ["111"]
            assert "1710000000" not in allowed.text


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
            assert "1710000000" not in slow.text
            assert len(adapters) == 1


@pytest.mark.asyncio
async def test_mounted_panel_keeps_guard_csp_and_mcp_boundary(tmp_path):
    settings = Settings(data_dir=tmp_path, port=free_port(), profiles=two_profiles())
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
