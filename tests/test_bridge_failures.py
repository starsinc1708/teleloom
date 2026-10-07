import asyncio
import json
import os
import socket
import sys

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from teleloom.config import Profile, Settings
from teleloom.daemon import create_application
from tests.fakes import TelegramAPI, data


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["inbox_ack", "delivery_execute"])
async def test_interrupted_mutation_is_not_replayed_by_stdio_bridge(
    tmp_path, monkeypatch, operation
):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(
        data_dir=tmp_path,
        port=port,
        read_timeout_seconds=0.1,
        profiles={"personal": Profile(kind="user", send_chats=["100"], mutation_chats=["100"])},
    )
    settings.save()
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "fault-test-token")
    mutations = []
    sent = asyncio.Event()

    class ObservedTelegramAPI(TelegramAPI):
        async def acknowledge(self, *args, **kwargs):
            mutations.append("ack")
            sent.set()
            await super().acknowledge(*args, **kwargs)

        async def mutate_message(self, operation, random_id):
            assert operation["kind"] == "read_ack"
            await self.acknowledge(operation["chat_id"], int(operation["through_message_id"]))
            return {"accepted": True, "complete": True}

        async def send(self, *args, **kwargs):
            mutations.append("send")
            sent.set()
            return await super().send(*args, **kwargs)

    real_app = create_application(settings, "fault-test-token", ObservedTelegramAPI)
    accepted_requests = []

    async def drop_mutation_reply(scope, receive, send):
        drop = False
        failed = False
        body = b""

        async def observe_receive():
            nonlocal drop, body
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body") and body:
                    request = json.loads(body)
                    if request.get("params", {}).get("name") == operation:
                        accepted_requests.append(operation)
                        drop = True
            return message

        async def fail_after_operation(message):
            nonlocal failed
            if drop and not failed:
                failed = True
                raise ConnectionResetError("Simulated loss of reply after accepted mutation")
            await send(message)

        await real_app(scope, observe_receive, fail_after_operation)

    daemon = uvicorn.Server(
        uvicorn.Config(
            drop_mutation_reply, host="127.0.0.1", port=port, log_level="critical", access_log=False
        )
    )
    serving = asyncio.create_task(daemon.serve())
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teleloom", "mcp"],
        env={**os.environ, "TELELOOM_DATA_DIR": str(tmp_path)},
    )
    try:
        async with asyncio.timeout(5):
            while not daemon.started:
                await asyncio.sleep(0.01)
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            arguments = {"profile_id": "personal", "chat_id": "100", "through_message_id": "4"}
            if operation == "inbox_ack":
                preview = data(
                    await session.call_tool(
                        "message_operation_preview",
                        {
                            "profile_id": "personal",
                            "operation": {
                                "kind": "read_ack",
                                "chat_id": "100",
                                "through_message_id": "4",
                            },
                        },
                    )
                )["data"]
                arguments.update(
                    plan_id=preview["plan_id"], plan_hash=preview["plan_hash"], confirmed=True
                )
            if operation == "delivery_execute":
                preview = data(
                    await session.call_tool(
                        "delivery_preview",
                        {"profile_id": "personal", "recipients": ["100"], "text": "Test"},
                    )
                )["data"]
                arguments = {
                    "profile_id": "personal",
                    "plan_id": preview["plan_id"],
                    "plan_hash": preview["plan_hash"],
                    "confirmed": True,
                }
            interrupted = await asyncio.wait_for(
                session.call_tool(operation, arguments), timeout=20
            )
            assert interrupted.isError
            assert accepted_requests == [operation]
            assert data(await session.call_tool("server_status", {}))["ok"]
            if operation == "delivery_execute":
                await asyncio.wait_for(sent.wait(), timeout=2)
                jobs = data(await session.call_tool("jobs_status", {"profile_id": "personal"}))
                assert len(jobs["data"]["jobs"]) == 1
                assert mutations == ["send"]
            else:
                await asyncio.wait_for(sent.wait(), timeout=2)
                assert mutations == ["ack"]
    finally:
        daemon.should_exit = True
        await serving
