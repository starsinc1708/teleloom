import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from teleloom.config import Profile, Settings
from teleloom.daemon import create_application
from tests.fakes import TelegramAPI


@asynccontextmanager
async def running(settings, adapter_factory=TelegramAPI, *, network=False):
    app = create_application(settings, "test-owner-token", adapter_factory)
    if not network:
        async with app.app.router.lifespan_context(app.app):
            yield app
        return
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
        await asyncio.wait_for(task, timeout=10)


@asynccontextmanager
async def client(app, settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": "Bearer test-owner-token"},
        timeout=10,
    ) as http:
        async with streamable_http_client(settings.url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@pytest.mark.asyncio
async def test_actual_http_mcp_has_one_owner_and_rejects_unauthenticated_requests(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as browser:
            assert (await browser.get(settings.url)).status_code == 401
            assert (
                await browser.get(settings.url, headers={"Origin": "https://evil.invalid"})
            ).status_code == 403
        async with client(app, settings) as first, client(app, settings) as second:
            a = await first.call_tool("server_status", {})
            b = await second.call_tool("server_status", {})
            assert (
                a.structuredContent["data"]["owner_id"] == b.structuredContent["data"]["owner_id"]
            )
            assert "folders_list" in {tool.name for tool in (await first.list_tools()).tools}
            assert (await second.call_tool("profiles_list", {})).structuredContent["data"][
                "profiles"
            ] == []
            assert not (await first.call_tool("server_status", {})).isError


@pytest.mark.asyncio
@pytest.mark.parametrize("version_header", [False, True])
async def test_http_modern_discovery_reports_supported_handshake_instead_of_empty_catalog(
    tmp_path, version_header
):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={
                "Authorization": "Bearer test-owner-token",
                "Accept": "application/json, text/event-stream",
            },
        ) as http:
            if version_header:
                http.headers["MCP-Protocol-Version"] = "2026-07-28"
            response = await http.post(
                settings.url,
                json={
                    "jsonrpc": "2.0",
                    "id": "discovery",
                    "method": "server/discover",
                    "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
                },
            )
            body = response.json()
            assert "result" not in body
            assert body["id"] == "discovery"
            assert body["error"]["code"] == -32601
            assert "legacy" in body["error"]["message"]
            assert "initialize" in body["error"]["message"]
            assert "tools/list" in body["error"]["message"]


@pytest.mark.asyncio
async def test_http_protocol_guard_keeps_auth_and_sdk_parse_errors(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            denied = await http.post(
                settings.url, json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
            )
            assert denied.status_code == 401
            assert denied.json() == {"error": "unauthorized"}
            http.headers.update(
                {
                    "Authorization": "Bearer test-owner-token",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                }
            )
            malformed = await http.post(settings.url, content='{"jsonrpc":')
            assert malformed.status_code == 400
            assert malformed.json()["error"]["code"] == -32700
            assert (await http.get(settings.url.removesuffix("/mcp") + "/sse")).status_code == 404


@pytest.mark.asyncio
async def test_http_empty_selected_catalog_is_an_explicit_owner_choice(tmp_path):
    settings = Settings(data_dir=tmp_path, exposure_mode="selected")
    async with running(settings) as app, client(app, settings) as session:
        assert (await session.list_tools()).tools == []
        hidden = await session.call_tool("delivery_execute", {})
        assert hidden.isError
        assert "Unknown tool: delivery_execute" in hidden.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2099-01-01"]
)
async def test_http_legacy_negotiation_lists_complete_catalog_and_dispatches(tmp_path, protocol):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings) as app:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={
                "Authorization": "Bearer test-owner-token",
                "Accept": "application/json, text/event-stream",
            },
        ) as http:
            initialized = await http.post(
                settings.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": protocol,
                        "capabilities": {},
                        "clientInfo": {"name": "compatibility-check", "version": "1"},
                    },
                },
            )
            negotiated = protocol if protocol != "2099-01-01" else "2025-11-25"
            assert initialized.json()["result"]["protocolVersion"] == negotiated
            assert "tools" in initialized.json()["result"]["capabilities"]
            http.headers["MCP-Protocol-Version"] = negotiated
            notified = await http.post(
                settings.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            assert notified.status_code == 202
            listed = await http.post(
                settings.url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            tools = listed.json()["result"]["tools"]
            assert len(tools) == 67
            assert all(tool["inputSchema"] and tool["outputSchema"] for tool in tools)
            called = await http.post(
                settings.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "messages_get",
                        "arguments": {"profile_id": "personal", "chat_id": "100"},
                    },
                },
            )
            result = called.json()["result"]
            assert not result["isError"]
            assert result["structuredContent"]["data"]["items"][0]["text"] == "Decision 5"


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["header", "body", "both"])
async def test_http_rejects_modern_list_and_dispatch_even_without_version_header(tmp_path, marker):
    calls = []

    class ObservedTelegramAPI(TelegramAPI):
        async def history(self, *args, **kwargs):
            calls.append("history")
            return await super().history(*args, **kwargs)

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, ObservedTelegramAPI) as app:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={
                "Authorization": "Bearer test-owner-token",
                "Accept": "application/json, text/event-stream",
            },
        ) as http:
            if marker in {"header", "both"}:
                http.headers["MCP-Protocol-Version"] = "2026-07-28"
            for method, params in [
                ("tools/list", {}),
                (
                    "tools/call",
                    {
                        "name": "messages_get",
                        "arguments": {"profile_id": "personal", "chat_id": "100"},
                    },
                ),
            ]:
                if marker in {"body", "both"}:
                    params["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
                response = await http.post(
                    settings.url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                assert response.status_code == 400
                body = response.json()
                assert "result" not in body
                assert body["error"]["code"] == -32600
                assert "legacy" in body["error"]["message"]
                assert "2025-11-25" in body["error"]["message"]
            assert calls == []
