"""Smoke-test an installed wheel with real stdio/CLI and no Telegram profiles."""

import asyncio
import json
import os
import socket
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from teleloom import __version__
from teleloom.config import Settings
from teleloom.daemon import stop_daemon

NEW_TOOLS = {
    "unread_export_start",
    "folder_members",
    "activity_start",
    "messages_search_many_start",
    "digest_context_many_start",
    "jobs_results",
    "topics_list",
    "topic_history",
    "thread_get",
    "comments_get",
    "messages_pinned",
    "attachment_capabilities",
    "attachments_read_start",
    "attachments_cleanup",
}


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="teleloom-wheel-smoke-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        settings = Settings(data_dir=Path(directory), port=port)
        settings.save()
        env = {
            **os.environ,
            "TELELOOM_DATA_DIR": directory,
            "TELELOOM_MCP_TOKEN": "isolated-wheel-smoke-token",
        }
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "teleloom", "mcp"], env=env
        )
        try:
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} >= NEW_TOOLS
                status = (await session.call_tool("server_status", {})).structuredContent
                assert status and status["ok"] and status["data"]["version"] == __version__
                empty = (await session.call_tool("profiles_list", {})).structuredContent
                assert empty and empty["data"]["profiles"] == []
                denied = (
                    await session.call_tool("attachment_capabilities", {"profile_id": "missing"})
                ).structuredContent
                assert denied and denied["error"]["code"] == "profile_not_found"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "teleloom",
                "call",
                "server_status",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            assert process.returncode == 0, stderr.decode()
            assert json.loads(stdout)["data"]["version"] == __version__
            bundled = files("teleloom") / "bundled_skills"
            skills = [
                child
                for child in bundled.iterdir()
                if child.is_dir() and (child / "SKILL.md").is_file()
            ]
            assert len(skills) == 6
            print(
                json.dumps(
                    {
                        "version": __version__,
                        "tools": len(tools),
                        "new_tools": len(NEW_TOOLS),
                        "bundled_skills": len(skills),
                        "stdio_discovery": "passed",
                        "cli": "passed",
                        "telegram_profiles": 0,
                    }
                )
            )
        finally:
            os.environ["TELELOOM_MCP_TOKEN"] = env["TELELOOM_MCP_TOKEN"]
            await stop_daemon(settings)


if __name__ == "__main__":
    asyncio.run(main())
