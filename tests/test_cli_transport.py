import asyncio
import json
import os
import sys

import pytest

from teleloom.config import Settings

pytestmark = pytest.mark.process_e2e


@pytest.mark.asyncio
async def test_stop_cli_handles_daemon_connection_closing_without_response(tmp_path):
    async def disconnect(reader, writer):
        await reader.read(4096)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(disconnect, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        Settings(data_dir=tmp_path, port=port).save()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "teleloom",
            "stop",
            env={
                **os.environ,
                "TELELOOM_DATA_DIR": str(tmp_path),
                "TELELOOM_MCP_TOKEN": "isolated-disconnect-token",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout) == {"stopped": False}
