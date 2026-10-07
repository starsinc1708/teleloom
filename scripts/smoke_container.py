"""Exercise a built container over stdio with disposable state and no accounts."""

import argparse
import asyncio
import json
import os
import subprocess
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(image: str) -> None:
    volume = "teleloom-smoke-" + uuid.uuid4().hex
    environment = {**os.environ, "TELELOOM_MCP_TOKEN": "synthetic-container-smoke-only"}
    command = [
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=128m",
        "--mount",
        f"type=volume,source={volume},target=/data",
        "--env",
        "TELELOOM_MCP_TOKEN",
    ]
    try:
        owner_ids = []
        for cycle in range(2):
            async with (
                stdio_client(
                    StdioServerParameters(
                        command="docker",
                        args=[*command[1:], "-i", image],
                        env=environment,
                    )
                ) as (reader, writer),
                ClientSession(reader, writer) as client,
            ):
                await client.initialize()
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert {"profiles_list", "delivery_execute", "jobs_results"} <= names
                profiles = (await client.call_tool("profiles_list")).structuredContent
                assert profiles and profiles["data"]["profiles"] == []
                status = (await client.call_tool("server_status")).structuredContent
                assert status and status["data"]["build"]["source_commit"] != "unknown"
                owner_ids.append(status["data"]["owner_id"])
            persisted = subprocess.run(
                [*command, image, "limits", *(["--daily-messages", "123"] if cycle == 0 else [])],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            assert json.loads(persisted.stdout)["daily_messages"] == 123
        assert len(set(owner_ids)) == 2
        print(
            json.dumps(
                {
                    "container_stdio": "passed",
                    "persistent_state": "passed",
                    "telegram_calls": 0,
                    "ai_calls": 0,
                    "tools": len(names),
                }
            )
        )
    finally:
        subprocess.run(["docker", "volume", "rm", volume], check=True, capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    asyncio.run(main(parser.parse_args().image))
