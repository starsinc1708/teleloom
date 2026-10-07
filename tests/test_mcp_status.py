import json

import pytest

from teleloom.config import Settings
from teleloom.server import create_server


@pytest.mark.asyncio
async def test_two_clients_discover_the_same_local_owner(tmp_path):
    server = create_server(Settings(data_dir=tmp_path))
    first = await server.call_tool("server_status", {})
    second = await server.call_tool("server_status", {})
    assert (
        first.structuredContent["data"]["owner_id"] == second.structuredContent["data"]["owner_id"]
    )
    assert first.structuredContent["data"]["profiles"] == 0
    assert json.loads(first.content[0].text)["ok"] is True
    tools = await server.list_tools()
    assert all(tool.outputSchema for tool in tools)
