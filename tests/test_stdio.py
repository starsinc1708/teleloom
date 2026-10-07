import asyncio
import os
import socket
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.message import SessionMessage
from mcp.types import (
    ClientCapabilities,
    ClientNotification,
    ClientRequest,
    Implementation,
    InitializedNotification,
    InitializeRequest,
    InitializeRequestParams,
    InitializeResult,
    JSONRPCMessage,
    JSONRPCRequest,
)

from teleloom.config import Profile, Settings
from teleloom.daemon import ownership, stop_daemon
from tests.fakes import TelegramAPI, data
from tests.test_transport import client, running

pytestmark = pytest.mark.process_e2e


@pytest.mark.asyncio
async def test_two_real_stdio_bridges_share_auto_started_daemon(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(data_dir=tmp_path, port=port)
    settings.save()
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "isolated-test-token")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teleloom", "mcp"],
        env={**os.environ, "TELELOOM_DATA_DIR": str(tmp_path)},
    )

    async def inspect():
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            discovered = {tool.name for tool in (await session.list_tools()).tools}
            assert {"folders_list", "chats_list", "messages_get", "server_status"} <= discovered
            result = data(await session.call_tool("server_status", {}))
            assert result["ok"]
            return result["data"]["owner_id"]

    try:
        first, second = await asyncio.gather(inspect(), inspect())
        assert first == second
    finally:
        await stop_daemon(settings)
    assert not await stop_daemon(settings)


@pytest.mark.asyncio
async def test_existing_stdio_bridge_recovers_after_daemon_is_stopped(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(data_dir=tmp_path, port=port)
    settings.save()
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "restart-test-token")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teleloom", "mcp"],
        env={**os.environ, "TELELOOM_DATA_DIR": str(tmp_path)},
    )
    try:
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            first = data(await session.call_tool("server_status", {}))
            assert first["ok"]
            assert await stop_daemon(settings)
            second = data(
                await asyncio.wait_for(
                    session.call_tool("server_status", {}),
                    timeout=settings.read_timeout_seconds + 20,
                )
            )
            assert second["ok"]
            assert second["data"]["owner_id"] != first["data"]["owner_id"]
    finally:
        await stop_daemon(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,count", [("all", 67), ("read-only", 54), ("selected", 3)])
async def test_stdio_protocol_exposure_and_reconnect_keep_one_telegram_owner(
    tmp_path, monkeypatch, mode, count
):
    starts, closes, sends = [], [], []

    class ObservedTelegramAPI(TelegramAPI):
        async def start(self):
            starts.append(self.profile)

        async def close(self):
            closes.append(self.profile)

        async def send(self, *args, **kwargs):
            sends.append(self.profile)
            return await super().send(*args, **kwargs)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(
        data_dir=tmp_path,
        port=port,
        profiles={"personal": Profile(kind="user")},
        exposure_mode=mode,
        exposed_tools=["server_status", "profiles_list", "messages_get"]
        if mode == "selected"
        else [],
    )
    settings.save()
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "test-owner-token")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teleloom", "mcp"],
        env={**os.environ, "TELELOOM_DATA_DIR": str(tmp_path)},
    )
    arguments = {"profile_id": "personal", "chat_id": "100"}

    async def inspect(session, expected_tools, owner_id):
        listed = (await session.list_tools()).tools
        assert [tool.model_dump() for tool in listed] == expected_tools
        assert data(await session.call_tool("server_status", {}))["data"]["owner_id"] == owner_id
        result = await session.call_tool("messages_get", arguments)
        assert not result.isError
        assert data(result)["data"]["items"][0]["text"] == "Decision 5"
        if mode != "all":
            assert "delivery_execute" not in {tool.name for tool in listed}
            hidden = await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": "hidden",
                    "plan_hash": "hidden",
                    "confirmed": True,
                },
            )
            assert hidden.isError
            assert "Unknown tool: delivery_execute" in hidden.content[0].text

    with ownership(settings):
        async with (
            running(settings, ObservedTelegramAPI, network=True) as app,
            client(app, settings) as direct,
        ):
            owner_id = data(await direct.call_tool("server_status", {}))["data"]["owner_id"]
            tools = [tool.model_dump() for tool in (await direct.list_tools()).tools]
            assert len(tools) == count
            await inspect(direct, tools, owner_id)
            async with (
                stdio_client(parameters) as (read, write),
                stdio_client(parameters) as (second_read, second_write),
            ):
                # Probe the new mode before initialization, then use its supported fallback.
                for method, params, code in [
                    ("server/discover", {}, -32601),
                    (
                        "tools/list",
                        {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
                        -32600,
                    ),
                    (
                        "tools/call",
                        {
                            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
                            "name": "messages_get",
                            "arguments": arguments,
                        },
                        -32600,
                    ),
                ]:
                    await write.send(
                        SessionMessage(
                            JSONRPCMessage(
                                JSONRPCRequest(
                                    jsonrpc="2.0", id="unsupported", method=method, params=params
                                )
                            )
                        )
                    )
                    response = await asyncio.wait_for(read.receive(), timeout=10)
                    body = response.message.model_dump(mode="json")
                    assert "result" not in body
                    assert body["id"] == "unsupported"
                    assert body["error"]["code"] == code
                    assert "legacy" in body["error"]["message"]
                    assert "initialize" in body["error"]["message"]
                async with (
                    ClientSession(read, write) as first,
                    ClientSession(second_read, second_write) as second,
                ):
                    await second.initialize()
                    for protocol in [
                        "2024-11-05",
                        "2025-03-26",
                        "2025-06-18",
                        "2025-11-25",
                        "2099-01-01",
                    ]:
                        initialized = await first.send_request(
                            ClientRequest(
                                InitializeRequest(
                                    params=InitializeRequestParams(
                                        protocolVersion=protocol,
                                        capabilities=ClientCapabilities(),
                                        clientInfo=Implementation(
                                            name="compatibility-check", version="1"
                                        ),
                                    )
                                )
                            ),
                            InitializeResult,
                        )
                        assert initialized.protocolVersion == (
                            protocol if protocol != "2099-01-01" else "2025-11-25"
                        )
                        assert initialized.capabilities.tools is not None
                        await first.send_notification(ClientNotification(InitializedNotification()))
                        await asyncio.gather(
                            inspect(first, tools, owner_id), inspect(second, tools, owner_id)
                        )
            # Closing both bridges leaves the owner and its Telegram connection available.
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write) as reconnected,
            ):
                await reconnected.initialize()
                await inspect(reconnected, tools, owner_id)
            await inspect(direct, tools, owner_id)
            assert starts == ["personal"]
            assert closes == sends == []
    assert closes == ["personal"]
