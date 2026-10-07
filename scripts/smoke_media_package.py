"""Exercise real HTTP/stdio/CLI media workflows against an external Telegram SDK fixture."""

import argparse
import asyncio
import base64
import contextlib
import io
import json
import os
import socket
import sys
import sysconfig
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image
from telethon import TelegramClient, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

import teleloom
from teleloom.adapters import UserAdapter
from teleloom.config import Profile, Settings
from teleloom.daemon import create_application, health, ownership

CHAT = "-1000000000100"


def pixels() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(output, "PNG")
    return output.getvalue()


class TelegramFixture(TelegramClient):
    def __init__(self) -> None:
        session = StringSession()
        session.auth_key = AuthKey(b"m" * 256)
        super().__init__(session, 1, "synthetic-api-hash")
        self.photo = types.Photo(
            id=111,
            access_hash=777,
            file_reference=b"PRIVATE",
            date=datetime.now(UTC),
            sizes=[types.PhotoSize("x", 40, 30, len(pixels()))],
            dc_id=4,
        )
        self.row = types.Message(
            id=1,
            peer_id=types.PeerChannel(100),
            date=datetime.now(UTC),
            message="Original caption",
            media=types.MessageMediaPhoto(photo=self.photo),
        )

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self, *args: Any, **kwargs: Any) -> Any:
        return types.User(id=1, first_name="Synthetic owner")

    async def get_input_entity(self, value: Any) -> Any:
        return types.InputPeerChannel(100, 123)

    async def get_entity(self, value: Any) -> Any:
        return types.Channel(
            id=100,
            title="Selected channel",
            date=datetime.now(UTC),
            photo=types.ChatPhotoEmpty(),
            broadcast=True,
        )

    async def iter_messages(self, entity: Any, **kwargs: Any) -> Any:
        yield self.row

    async def get_messages(self, entity: Any, **kwargs: Any) -> Any:
        assert kwargs["ids"] == 1
        return self.row

    async def download_media(self, message: Any, *, file: Any) -> None:
        file.write(pixels())

    async def iter_download(self, media: Any, **kwargs: Any) -> Any:
        assert isinstance(media, types.MessageMediaPhoto)
        assert kwargs["request_size"] == kwargs["chunk_size"] == 65536
        yield pixels()

    async def upload_file(self, file: Any, *, file_size: int, file_name: str) -> Any:
        content = file.read()
        assert len(content) == file_size and content == b"Confirmed immutable bytes"
        return types.InputFile(123, 1, file_name, "synthetic-checksum")

    async def __call__(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        assert isinstance(request, functions.messages.SendMediaRequest)
        assert request.peer.channel_id == 100 and request.random_id
        assert isinstance(request.media, types.InputMediaUploadedDocument)
        assert request.message == "Confirmed caption" and request.entities == []
        return types.UpdateShortSentMessage(id=20, pts=1, pts_count=1, date=datetime.now(UTC))


def adapter_factory(profile_id: str, profile: Profile, store: Any, credentials: Any) -> UserAdapter:
    adapter = object.__new__(UserAdapter)
    adapter.profile_id, adapter.profile, adapter.store = profile_id, profile, store
    adapter.client = TelegramFixture()
    return adapter


def wire(result: Any, *, image: bool = False) -> dict[str, Any]:
    data = json.loads(result.content[0].text)
    assert data == result.structuredContent
    if image:
        assert len(result.content) == 2 and result.content[1].type == "image", data
        with Image.open(io.BytesIO(base64.b64decode(result.content[1].data))) as rendered:
            assert rendered.format == "JPEG" and rendered.width > 0
    return data


async def main(source_checkout: bool = False) -> None:
    if not source_checkout:
        assert (
            Path(teleloom.__file__)
            .resolve()
            .is_relative_to(Path(sysconfig.get_path("purelib")).resolve())
        ), "Media smoke requires the installed wheel outside checkout."
    with tempfile.TemporaryDirectory(prefix="teleloom-media-smoke-") as directory:
        root = Path(directory)
        selected = root / "selected"
        selected.mkdir()
        source = selected / "report.txt"
        source.write_bytes(b"Confirmed immutable bytes")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        settings = Settings(
            data_dir=root,
            port=port,
            profiles={
                "personal": Profile(
                    kind="user", read_mode="selected", read_chats=[CHAT], send_chats=[CHAT]
                )
            },
        )
        settings.save()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("TELELOOM_")
            and key not in {"PYTHONPATH", "PYTHONHOME", "TYPESAFE_API_KEY"}
        }
        env.update(
            TELELOOM_DATA_DIR=directory,
            TELELOOM_MCP_TOKEN="isolated-media-token",
            PYTHONIOENCODING="utf-8",
        )
        configured = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "teleloom",
            "profile",
            "file-root",
            "personal",
            str(selected),
            "--enable",
            cwd=root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(configured.communicate(), timeout=30)
        assert configured.returncode == 0, stderr.decode(errors="replace")
        assert json.loads(stdout)["file_roots"] == [str(selected)]
        daemon = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(Path(__file__).resolve()),
            "--daemon",
            cwd=root,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(20):
                while not await health(settings, env["TELELOOM_MCP_TOKEN"]):
                    assert daemon.returncode is None, "External-fixture daemon failed to start"
                    await asyncio.sleep(0.05)
            parameters = StdioServerParameters(
                command=sys.executable, args=["-I", "-m", "teleloom", "mcp"], env=env
            )
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                assert {
                    "media_operation_preview",
                    "media_info",
                    "media_download",
                    "photo_open",
                    "photo_sheet",
                    "media_cleanup",
                } <= tools
                denied = wire(
                    await session.call_tool(
                        "photo_open", {"profile_id": "personal", "chat_id": "43", "message_id": "1"}
                    )
                )
                assert denied["error"]["code"] == "read_not_allowed"
                opened = wire(
                    await session.call_tool(
                        "photo_open", {"profile_id": "personal", "chat_id": CHAT, "message_id": "1"}
                    ),
                    image=True,
                )
                assert opened["ok"] and opened["data"]["width"] == 40
                sheet = wire(
                    await session.call_tool(
                        "photo_sheet",
                        {
                            "profile_id": "personal",
                            "chat_id": CHAT,
                            "source": "messages",
                            "limit": 1,
                        },
                    ),
                    image=True,
                )
                assert sheet["ok"] and sheet["data"]["items"] == [{"id": "1"}]
                info = wire(
                    await session.call_tool(
                        "media_info", {"profile_id": "personal", "chat_id": CHAT, "message_id": "1"}
                    )
                )
                assert info["ok"] and info["data"]["item"]["text"] == "Original caption"
                plan = wire(
                    await session.call_tool(
                        "media_operation_preview",
                        {
                            "profile_id": "personal",
                            "operation": {
                                "kind": "send_file",
                                "chat_id": CHAT,
                                "source_path": str(source),
                                "caption": "Confirmed caption",
                            },
                        },
                    )
                )
                assert plan["ok"], plan
                execute = {
                    "profile_id": "personal",
                    "plan_id": plan["data"]["plan_id"],
                    "plan_hash": plan["data"]["plan_hash"],
                }
                denied = wire(await session.call_tool("delivery_execute", execute))
                assert denied["error"]["code"] == "confirmation_required"
                job = wire(
                    await session.call_tool("delivery_execute", {**execute, "confirmed": True})
                )["data"]["job_id"]
                async with asyncio.timeout(10):
                    while True:
                        status = wire(
                            await session.call_tool(
                                "jobs_status", {"profile_id": "personal", "job_id": job}
                            )
                        )["data"]
                        if status["status"] == "completed":
                            break
                        assert status["status"] in {"queued", "running"}, status
                        await asyncio.sleep(0.05)
                assert status["deliveries"][0]["receipt"]["message_ids"] == ["20"]
                assert wire(await session.call_tool("media_cleanup", {"profile_id": "personal"}))[
                    "ok"
                ]
            completed = json.dumps(
                {
                    "media_stdio_cli": "passed",
                    "inline_images": "passed",
                    "photo_sheet": "passed",
                    "confirmed_sdk_send": "passed",
                    "installed_wheel": not source_checkout,
                }
            )
        finally:
            shutdown = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "teleloom",
                "stop",
                cwd=root,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(shutdown.wait(), timeout=5)
            if shutdown.returncode is None:
                shutdown.kill()
                await shutdown.wait()
            try:
                await asyncio.wait_for(daemon.wait(), timeout=5)
            except TimeoutError:
                daemon.kill()
                await daemon.wait()
    print(completed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--source-checkout", action="store_true")
    args = parser.parse_args()
    if args.daemon:
        import uvicorn

        from teleloom import session_state

        settings = Settings.load()
        session_state.lock_directory = lambda: settings.data_dir / "fixture-session-locks"
        with ownership(settings):
            application = create_application(
                settings,
                os.environ["TELELOOM_MCP_TOKEN"],
                adapter_factory,
                shutdown=lambda: setattr(server, "should_exit", True),
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    application,
                    host="127.0.0.1",
                    port=settings.port,
                    log_level="critical",
                    access_log=False,
                )
            )
            server.run()
    else:
        asyncio.run(main(args.source_checkout))
