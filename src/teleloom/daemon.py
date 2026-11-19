import asyncio
import logging
import os
import secrets
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import uvicorn
from filelock import FileLock, Timeout
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    CallToolResult,
    ErrorData,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCRequest,
)
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .adapters import make_adapter
from .build_info import build_info
from .config import Settings
from .control import claim_owner, control_lock, live_transition, ownership
from .models import TeleloomError
from .panel import Panel
from .runtime import AdapterFactory, Runtime, fingerprint
from .secrets import Secrets
from .server import create_server, output


def protocol_error(
    message: JSONRPCMessage, header_version: str | None = None
) -> JSONRPCError | None:
    request = message.root
    if not isinstance(request, JSONRPCRequest):
        return None
    meta = (request.params or {}).get("_meta", {})
    body_version = (
        meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None
    )
    discovery = request.method == "server/discover"
    if discovery or any(
        version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS
        for version in (header_version, body_version)
    ):
        return JSONRPCError(
            jsonrpc="2.0",
            id=request.id,
            error=ErrorData(
                code=METHOD_NOT_FOUND if discovery else INVALID_REQUEST,
                message=(
                    "Unsupported MCP mode: server/discover is unavailable. "
                    if discovery
                    else "Unsupported MCP protocol version. "
                )
                + "Use legacy mode: initialize, notifications/initialized, tools/list, tools/call "
                + "over teleloom mcp (stdio) or authenticated /mcp (Streamable HTTP). "
                + "Supported revisions: "
                + ", ".join(SUPPORTED_PROTOCOL_VERSIONS),
            ),
        )
    return None


class LocalAuth:
    def __init__(self, app: ASGIApp, token: str, port: int, panel: Panel | None = None) -> None:
        self.app, self.token, self.host = app, token, f"127.0.0.1:{port}"
        self.panel = panel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if self.panel is not None and (path == "/ui" or path.startswith("/ui/")):
            # The panel enforces its own session, Host/Origin and CSRF policy.
            await self.panel(scope, receive, send)
            return
        request = Request(scope, receive)
        if (
            request.headers.get("host") != self.host
            or request.headers.get("origin", "http://" + self.host) != "http://" + self.host
        ):
            await JSONResponse({"error": "forbidden_origin"}, status_code=403)(scope, receive, send)
            return
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), ("Bearer " + self.token).encode()):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        if request.url.path == "/mcp" and request.method == "POST":
            body = await request.body()
            try:
                error = protocol_error(
                    JSONRPCMessage.model_validate_json(body),
                    request.headers.get("mcp-protocol-version"),
                )
            except ValidationError:
                error = None  # The SDK retains its malformed-request handling.
            if error:
                await JSONResponse(
                    error.model_dump(mode="json", exclude_none=True),
                    status_code=200 if error.error.code == METHOD_NOT_FOUND else 400,
                )(scope, receive, send)
                return
            pending = True

            async def replay_body() -> Message:
                nonlocal pending
                if pending:
                    pending = False
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay_body, send)
            return
        await self.app(scope, receive, send)


def create_application(
    settings: Settings,
    token: str,
    adapter_factory: AdapterFactory = make_adapter,
    shutdown: Callable[[], None] | None = None,
) -> ASGIApp:
    runtime = Runtime(settings, adapter_factory)
    server = create_server(settings, runtime=runtime)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await runtime.start()
        try:
            async with server.session_manager.run():
                yield
        finally:
            await runtime.close()

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": "teleloom",
                "owner_id": runtime.owner_id,
                "version": __version__,
                "workspace_id": fingerprint(str(settings.data_dir.resolve())),
                "build": build_info(),
            }
        )

    async def stop(request: Request) -> JSONResponse:
        if shutdown is None:
            return JSONResponse({"error": "shutdown_unavailable"}, status_code=409)
        shutdown()
        return JSONResponse({"stopping": True})

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/shutdown", stop, methods=["POST"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    return LocalAuth(app, token, settings.port, Panel(settings, runtime, token))


def installed_interpreter() -> str:
    """Use the interpreter that runs this installation, preferring windowless Windows Python."""
    if sys.platform == "win32":
        windowless = Path(sys.executable).with_name("pythonw.exe")
        if windowless.is_file():
            return str(windowless)
    return sys.executable


def serve(settings: Settings) -> None:
    token = Secrets().require("", "mcp_token")
    try:
        # A management handoff is the one legitimate start during a live transition.
        owner = claim_owner(settings, handoff=os.environ.get("TELELOOM_CONTROL_HANDOFF"))
        try:
            # Avoid third-party exception/request logs carrying sensitive inputs.
            logging.basicConfig(level=logging.CRITICAL)
            server: uvicorn.Server
            app = create_application(
                settings, token, shutdown=lambda: setattr(server, "should_exit", True)
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=settings.port,
                    access_log=False,
                    log_level="critical",
                )
            )
            server.run()
        finally:
            owner.release()
    except Timeout:
        raise TeleloomError(
            "owner_busy", "A daemon or authentication flow already owns this workspace."
        ) from None


async def health(settings: Settings, token: str, *, report_timeout: bool = False) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            result = await client.get(
                settings.url.removesuffix("/mcp") + "/health",
                headers={"Authorization": "Bearer " + token},
            )
            if result.status_code in {401, 403}:
                raise TeleloomError(
                    "daemon_mismatch",
                    "Port is occupied by a daemon with different credentials; inspect configuration.",
                )
            if result.status_code == 200 and result.json().get("service") == "teleloom":
                if result.json().get("workspace_id") != fingerprint(
                    str(settings.data_dir.resolve())
                ):
                    raise TeleloomError(
                        "daemon_mismatch", "Port is owned by a different local workspace."
                    )
                return True
            raise TeleloomError("port_occupied", "Configured port belongs to another service.")
    except httpx.ConnectTimeout:
        return False
    except httpx.TimeoutException:
        if report_timeout:
            raise TeleloomError(
                "daemon_timeout", "The local owner health check timed out."
            ) from None
        return False
    except httpx.TransportError:
        return False


async def ensure_daemon(settings: Settings, token: str, *, handoff: str | None = None) -> None:
    def check_transition() -> None:
        if handoff is None and (pending := live_transition(settings)) is not None:
            raise TeleloomError(
                "transition_in_progress",
                f"Owner management is at phase '{pending['phase']}'; the owner is started by it.",
            )

    check_transition()
    if await health(settings, token):
        return
    gate = control_lock(settings)
    lock = FileLock(settings.data_dir / "startup.lock", timeout=15)
    try:
        # Nonblocking polling avoids holding the event loop in FileLock.acquire.
        deadline = time.monotonic() + 15
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                if time.monotonic() >= deadline:
                    raise TeleloomError(
                        "startup_busy", "Another client is starting the daemon; retry shortly."
                    ) from None
                await asyncio.sleep(0.1)
        try:
            # Only the serialized starter probes the management lock. Ordinary
            # bridge callers must not contend with each other or the owner booting.
            try:
                with gate:
                    check_transition()
            except Timeout:
                raise TeleloomError(
                    "transition_in_progress",
                    "Owner management is active; retry after it reports.",
                ) from None
            if await health(settings, token):
                return
            env = {
                **os.environ,
                "TELELOOM_DATA_DIR": str(settings.data_dir),
                "TELELOOM_MCP_TOKEN": token,
            }
            if handoff:
                env["TELELOOM_CONTROL_HANDOFF"] = handoff
            kwargs: dict[str, Any] = {
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(
                [installed_interpreter(), "-m", "teleloom", "serve"], **kwargs
            )
            for _ in range(150):
                if await health(settings, token):
                    return
                if process.poll() is not None:
                    raise TeleloomError(
                        "daemon_start_failed",
                        "Daemon could not start. Run teleloom serve to inspect setup.",
                    )
                await asyncio.sleep(0.1)
            raise TeleloomError(
                "daemon_start_failed",
                "Daemon startup timed out. Run teleloom serve to inspect setup.",
            )
        finally:
            lock.release()
    except Timeout:
        raise TeleloomError("startup_busy", "Another client is starting the daemon.") from None


async def stop_daemon(settings: Settings) -> bool:
    token = Secrets().require("", "mcp_token")
    if not await health(settings, token):
        return False
    async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
        response = await http.post(
            settings.url.removesuffix("/mcp") + "/shutdown",
            headers={"Authorization": "Bearer " + token},
        )
        if response.status_code != 200:
            raise TeleloomError("stop_failed", "Local daemon rejected shutdown.")
    for _ in range(300):
        try:
            with ownership(settings):
                return True
        except Timeout:
            await asyncio.sleep(0.1)
    raise TeleloomError(
        "stop_pending", "Daemon shutdown is still in progress; wait before authenticating."
    )


@asynccontextmanager
async def session(settings: Settings, auto_start: bool = True) -> AsyncIterator[ClientSession]:
    token = Secrets().require("", "mcp_token")
    if auto_start:
        await ensure_daemon(settings, token)
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=60
    ) as http:
        async with streamable_http_client(settings.url, http_client=http) as (read, write, _):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=settings.read_timeout_seconds + 15),
            ) as client:
                await client.initialize()
                yield client


async def bridge(settings: Settings) -> None:
    async with session(settings) as upstream:
        discovered_tools = (await upstream.list_tools()).tools

    server: Server[Any] = Server("teleloom-bridge")

    @server.list_tools()
    async def list_tools() -> Any:
        return discovered_tools

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            # Fresh sessions survive daemon restarts; failed requests are never replayed.
            async with asyncio.timeout(settings.read_timeout_seconds + 15):
                async with session(settings) as upstream:
                    return await upstream.call_tool(name, arguments)
        except TimeoutError:
            return output(
                error=TeleloomError(
                    "daemon_timeout",
                    "Local MCP request timed out; inspect job status before retrying delivery. No request was replayed.",
                )
            )
        except Exception:
            return output(
                error=TeleloomError(
                    "daemon_unavailable",
                    "Local MCP connection failed; inspect job status before retrying delivery.",
                )
            )

    async with stdio_server() as (read, write):
        forwarded, compatible = anyio.create_memory_object_stream[SessionMessage | Exception](0)

        async def check_protocol() -> None:
            async with forwarded:
                async for message in read:
                    error = (
                        protocol_error(message.message)
                        if isinstance(message, SessionMessage)
                        else None
                    )
                    if error:
                        await write.send(SessionMessage(JSONRPCMessage(error)))
                    else:
                        await forwarded.send(message)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(check_protocol)
            await server.run(compatible, write, server.create_initialization_options())
            tasks.cancel_scope.cancel()
