"""Owner-only management process: exact exposure diff, Apply and a verified restart.

It is a separate local process launched by an explicit CLI command, never by the
daemon or an MCP client, and it never holds the MCP bearer as a credential of its own.
"""

import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from filelock import FileLock, Timeout
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import __version__, control_clients
from .build_info import build_info
from .config import Settings, private_dir
from .models import TeleloomError, iso, utcnow
from .store import Store

MODES = ("all", "read-only", "selected")
KEY = "control_transition"
TERMINAL = frozenset({"verified", "failed", "abandoned"})


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def workspace_id(settings: Settings) -> str:
    return hashlib.sha256(
        json.dumps(str(settings.data_dir.resolve()), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def control_lock(settings: Settings) -> FileLock:
    private_dir(settings.data_dir)
    return FileLock(settings.data_dir / "control.lock", timeout=0)


def ownership(settings: Settings) -> FileLock:
    private_dir(settings.data_dir)
    return FileLock(settings.data_dir / "owner.lock", timeout=0)


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        kernel.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def record(settings: Settings) -> dict[str, Any] | None:
    """Durable transition record; absent until the first Apply."""
    if not (settings.data_dir / "workspace.sqlite").is_file():
        return None
    store = Store(settings.data_dir)
    try:
        return store.state(KEY)
    finally:
        store.close()


def save_record(settings: Settings, value: dict[str, Any]) -> None:
    store = Store(settings.data_dir)
    try:
        with store.db:
            store.set_state(KEY, value)
    finally:
        store.close()


def _live(value: dict[str, Any]) -> bool:
    return value.get("phase") not in TERMINAL and _alive(value.get("pid"))


def live_transition(settings: Settings) -> dict[str, Any] | None:
    """A record whose manager is still running: ordinary starts and logins must refuse."""
    value = record(settings)
    return value if value is not None and _live(value) else None


@contextmanager
def _gate(settings: Settings, handoff: str | None = None) -> Iterator[None]:
    gate = control_lock(settings)
    try:
        gate.acquire()
    except Timeout:
        raise TeleloomError(
            "transition_in_progress",
            "Owner management is active; wait for it to finish before retrying.",
        ) from None
    try:
        value = record(settings)
        if value is not None and value.get("phase") not in TERMINAL:
            if (
                value.get("handoff")
                and handoff
                and hmac.compare_digest(value["handoff"], _digest(handoff))
            ):
                value["handoff"] = None
                save_record(settings, value)
            elif _live(value):
                raise TeleloomError(
                    "transition_in_progress",
                    f"Owner management is at phase '{value['phase']}'; wait for the reported result.",
                )
            else:
                value["phase"], value["reason"] = "abandoned", "management process ended"
                save_record(settings, value)
        yield
    finally:
        gate.release()


def _owner(settings: Settings) -> FileLock:
    owner = ownership(settings)
    try:
        owner.acquire()
    except Timeout:
        raise TeleloomError(
            "owner_busy", "A daemon or authentication flow already owns this workspace."
        ) from None
    return owner


@contextmanager
def governed(settings: Settings, handoff: str | None = None) -> Iterator[None]:
    """One lock order everywhere: control.lock, then owner.lock.

    A live transition refuses every ordinary caller. Only the one-use handoff that
    `control` mints for the owner it starts may continue while a record is live.
    """
    with _gate(settings, handoff):
        owner = _owner(settings)
        try:
            yield
        finally:
            owner.release()


def claim_owner(settings: Settings, handoff: str | None = None) -> FileLock:
    """Take owner.lock under the gate, then keep only owner.lock for the process lifetime.

    The daemon must never hold control.lock: that would block every later management run.
    """
    with _gate(settings, handoff):
        return _owner(settings)


def read_config(settings: Settings) -> tuple[bytes, dict[str, Any]]:
    file = settings.data_dir / "config.json"
    raw = file.read_bytes() if file.is_file() else b""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise TeleloomError(
            "config_invalid", "config.json is not valid JSON; repair it by hand before applying."
        ) from None
    if not isinstance(data, dict):
        raise TeleloomError("config_invalid", "config.json must be a JSON object.")
    return raw, data


def revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def exposure(data: dict[str, Any]) -> dict[str, Any]:
    mode = data.get("exposure_mode", "all")
    tools = data.get("exposed_tools", [])
    return {
        "mode": mode,
        "tools": sorted(tools) if mode == "selected" and isinstance(tools, list) else [],
    }


def validated(mode: str, tools: list[str], known: set[str]) -> tuple[str, list[str]]:
    if mode not in MODES:
        raise TeleloomError("invalid_exposure", "Choose all, read-only or selected.")
    names = sorted(dict.fromkeys(name for name in tools if name))
    if mode != "selected" and names:
        raise TeleloomError("invalid_exposure", "Only the selected mode takes tool names.")
    if mode == "selected":
        if not names:
            raise TeleloomError("invalid_exposure", "Select at least one tool by exact name.")
        if not known:
            raise TeleloomError(
                "catalogue_unavailable",
                "The owner did not report its tool catalogue; restart it and reopen control.",
            )
        unknown = [name for name in names if name not in known]
        if unknown:
            raise TeleloomError(
                "unknown_tools", "Unknown tool names: " + ", ".join(unknown[:10]) + "."
            )
    return mode, names


def write_exposure(settings: Settings, mode: str, tools: list[str]) -> None:
    """Replace only the two exposure keys, preserving every other stored field.

    Unknown JSON keys survive because the raw document is edited, not a model round trip.
    """
    _, data = read_config(settings)
    data["exposure_mode"], data["exposed_tools"] = mode, tools
    write_config(settings, data)


def write_config(settings: Settings, data: dict[str, Any]) -> None:
    """One atomic writer for raw config edits made by this management process."""
    file = settings.data_dir / "config.json"
    temporary = file.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(file)


def blockers(settings: Settings) -> list[str]:
    """Active work or unknown delivery: tasks are reviewed, never cancelled to change a setting."""
    if not (settings.data_dir / "workspace.sqlite").is_file():
        return []
    store = Store(settings.data_dir)
    try:
        found = (
            [f"{len(store.active_jobs())} queued or running job(s) must finish or be paused."]
            if store.active_jobs()
            else []
        )
        uncertain = 0
        for job in store.jobs():
            uncertain += sum(
                1
                for item in store.deliveries(job["id"])
                if item.get("status") in {"sending", "unknown"}
            )
            if (job.get("payload", {}).get("receipt") or {}).get("status") in {
                "sending",
                "unknown",
            }:
                uncertain += 1
        if uncertain:
            found.append(
                f"{uncertain} delivery or transcription receipt(s) are sending/unknown; "
                "reconcile them in Teleloom first. Nothing will be replayed."
            )
        return found
    finally:
        store.close()


def diff(current: dict[str, Any], mode: str, tools: list[str]) -> list[str]:
    lines = []
    if current["mode"] != mode:
        lines.append(f"exposure_mode: {current['mode']} -> {mode}")
    if current["tools"] != tools:
        lines.append(f"exposed_tools: {current['tools']} -> {tools}")
    return lines


@dataclass
class _Session:
    csrf: str
    expires_at: float


@dataclass
class Change:
    """One exact configuration change: the write, the live projection and its proof."""

    proposed: dict[str, Any]
    current: Callable[[dict[str, Any]], dict[str, Any]]
    write: Callable[[], None]
    verify: Callable[[dict[str, Any]], list[str]]
    describe: Callable[[dict[str, Any]], str]
    rotate: bool = False


def reserved(port: int = 0) -> socket.socket:
    """The bound listener the URL is issued for; uvicorn must serve this socket, not the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(64)
    except OSError:
        sock.close()
        raise
    return sock


@asynccontextmanager
async def owner_panel(settings: Settings, token: str) -> AsyncIterator[httpx.AsyncClient]:
    base = settings.url.removesuffix("/mcp")
    async with httpx.AsyncClient(base_url=base, timeout=10, trust_env=False) as http:
        issued = await http.post("/ui/bootstrap", headers={"Authorization": "Bearer " + token})
        if issued.status_code != 200:
            raise TeleloomError("panel_unavailable", "The owner did not issue a panel session.")
        fragment = urlsplit(issued.json()["url"]).fragment
        logged = await http.post(
            "/ui/session", json={"bootstrap": fragment}, headers={"Origin": base}
        )
        if logged.status_code != 204:
            raise TeleloomError("panel_unavailable", "The owner rejected its panel session.")
        yield http


class Lifecycle:
    """The only part of control that touches the owner; bearer stays inside these calls."""

    async def stop(self, settings: Settings) -> None:
        from .daemon import stop_daemon

        await stop_daemon(settings)

    async def start(self, settings: Settings, handoff: str) -> None:
        from .daemon import ensure_daemon
        from .secrets import Secrets

        await ensure_daemon(settings, Secrets().require("", "mcp_token"), handoff=handoff)

    async def observed(self, settings: Settings) -> dict[str, Any]:
        from .daemon import session
        from .secrets import Secrets

        token = Secrets().require("", "mcp_token")
        async with owner_panel(settings, token) as http:
            health = await http.get("/health", headers={"Authorization": "Bearer " + token})
            tools = await http.get("/ui/api/tools")
            profiles = await http.get("/ui/api/profiles")
        if any(response.status_code != 200 for response in (health, tools, profiles)):
            raise TeleloomError("verify_failed", "The restarted owner did not report its status.")
        async with session(settings, auto_start=False) as client:
            listed = sorted(tool.name for tool in (await client.list_tools()).tools)
        catalogue = tools.json()
        if listed != sorted(tool["name"] for tool in catalogue["tools"] if tool["exposed"]):
            raise TeleloomError("verify_failed", "MCP discovery disagrees with the owner panel.")
        scope = profiles.json()
        return {
            **health.json(),
            "tools": listed,
            "scope": scope,
            "exposure": {key: catalogue[key] for key in ("mode", "selected_tools")},
        }


async def transition(
    lifecycle: Any,
    settings: Settings,
    operation: dict[str, Any],
    body: Callable[[], Awaitable[None]],
    *,
    verify: Callable[[dict[str, Any]], list[str]] | None = None,
) -> tuple[dict[str, Any], TeleloomError | None]:
    """Stop, run `body` under owner.lock, then restart under a one-use handoff and verify it.

    The single pause/write/restart path for every control-side profile change: active work
    refuses it, `owner.lock` covers the whole write (including a QR login's awaits), and the
    durable record is terminal only when an owner answered or the failure was recorded. Like
    Apply, it always restarts, because a profile only exists in a running owner.
    """
    if found := blockers(settings):
        return {}, TeleloomError(
            "transition_blocked",
            "Active work blocks this change; nothing was written. " + " ".join(found),
        )
    value: dict[str, Any] = {
        "phase": "drafted",
        "pid": os.getpid(),
        "executable": sys.executable,
        "started_at": utcnow().isoformat(),
        "revision": None,
        "proposed": operation,
        "owner": None,
        "reason": None,
        "handoff": None,
    }
    claimed = False
    failure: TeleloomError | None = None
    token = secrets.token_urlsafe(32)
    try:
        with _gate(settings):
            save_record(settings, value)
            claimed = True
            await lifecycle.stop(settings)
            value["phase"] = "stopped"
            save_record(settings, value)
            owner = _owner(settings)
            try:
                # Re-checked under owner.lock: a job started while the owner was stopping.
                if found := blockers(settings):
                    raise TeleloomError(
                        "transition_blocked",
                        "Active work blocks this change; nothing was written. " + " ".join(found),
                    )
                await body()
            except Exception as error:  # noqa: BLE001 - the owner still has to come back
                failure = _failure(error)
            finally:
                owner.release()
            # One-use handoff written before the gate releases: a bridge or login that
            # acquires control.lock next sees "starting" and refuses, never a terminal phase.
            value.update(
                phase="starting",
                handoff=_digest(token),
                reason=_reason(failure) if failure else None,
            )
            save_record(settings, value)
    except Exception as error:  # noqa: BLE001 - a failed change must leave durable truth
        failure = failure or _failure(error)
        if claimed:
            value.update(phase="failed", handoff=None, reason=_reason(failure))
            save_record(settings, value)
        return value, failure

    def failed(error: TeleloomError) -> tuple[dict[str, Any], TeleloomError]:
        # A body failure stays the reported reason; a restart failure is appended to it.
        combined = (
            error
            if failure is None
            else TeleloomError(failure.code, f"{failure.message} {error.message}")
        )
        value.update(phase="failed", handoff=None, reason=_reason(combined))
        save_record(settings, value)
        return value, combined

    try:
        await lifecycle.start(settings, token)
        observed = await lifecycle.observed(settings)
    except Exception as error:  # noqa: BLE001 - reported, never retried or rolled back
        return failed(_failure(error))
    try:
        mismatches = identity_mismatches(observed, settings)
        if verify is not None and failure is None and not mismatches:
            mismatches = verify(observed)
        if mismatches:
            return failed(TeleloomError("verify_failed", "; ".join(mismatches) + "."))
    except Exception as error:  # noqa: BLE001 - preserve a terminal record on verification failure
        return failed(_failure(error))
    value.update(
        phase="failed" if failure else "verified",
        handoff=None,
        owner={key: observed.get(key) for key in ("owner_id", "workspace_id", "version")},
    )
    save_record(settings, value)
    return value, failure


def _failure(error: Exception) -> TeleloomError:
    if isinstance(error, TeleloomError):
        return error
    return TeleloomError(
        "transition_failed",
        f"The owner change failed ({type(error).__name__}); the durable state is reported above.",
    )


def _reason(failure: TeleloomError) -> str:
    return f"{failure.code}: {failure.message}"


class Control:
    def __init__(
        self,
        settings: Settings,
        catalogue: list[dict[str, Any]],
        *,
        lifecycle: Any = None,
        owner: Any = None,
        port: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        bootstrap_ttl: float = 60,
        session_ttl: float = 8 * 60 * 60,
    ) -> None:
        self.settings = settings
        self.catalogue = sorted(catalogue, key=lambda item: str(item.get("name")))
        self.names = {str(item["name"]) for item in self.catalogue if item.get("name")}
        self.lifecycle = lifecycle or Lifecycle()
        self.owner = owner
        self.send_confirming = asyncio.Lock()
        self.socket = reserved(port or 0)
        self.port = self.socket.getsockname()[1]
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self.cookie = f"teleloom_control_{self.port}"
        self.clock = clock
        self.bootstrap_ttl = bootstrap_ttl
        self.session_ttl = session_ttl
        self.bootstraps: dict[str, float] = {}
        self.sessions: dict[str, _Session] = {}
        self.on_ready: Callable[[], None] | None = None
        self.app = Starlette(
            lifespan=self._lifespan,
            routes=[
                Route("/control", self.page),
                Route("/control/open", self.open_page),
                Route("/control/session", self.session, methods=["POST"]),
                Route("/control/apply", self.apply, methods=["POST"]),
                Route("/control/policy", self.policy),
                Route("/control/policy/apply", self.policy_apply, methods=["POST"]),
                Route("/control/proxies", self.proxies),
                Route("/control/proxies/check", self.proxy_check, methods=["POST"]),
                Route("/control/proxies/apply", self.proxies_apply, methods=["POST"]),
                Route("/control/engines", self.engines),
                Route("/control/engines/apply", self.engines_apply, methods=["POST"]),
                Route("/control/send", self.send),
                Route("/control/send/preview", self.send_preview, methods=["POST"]),
                Route("/control/send/confirm", self.send_confirm, methods=["POST"]),
                Route("/control/send/plan/{plan_id}", self.send_plan),
                Route("/control/send/job/{job_id}", self.send_job),
                Route("/control/clients", self.clients_page),
                Route("/control/clients/apply", self.clients_apply, methods=["POST"]),
            ],
        )
        # Imported here: both modules build on this one's locks, records and session.
        from .control_bot import BotOnboarding
        from .control_user import Onboarding

        self.user, self.bot = Onboarding(self), BotOnboarding(self)
        self.app.routes.extend(self.user.routes() + self.bot.routes())

    @asynccontextmanager
    async def _lifespan(self, app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            task = self.user.task
            if task is not None and not task.done():
                self.user.cancelled.set()
                await task

    def issue_bootstrap(self) -> dict[str, Any]:
        now = self.clock()
        self.bootstraps = {key: end for key, end in self.bootstraps.items() if end > now}
        if len(self.bootstraps) >= 8:
            del self.bootstraps[min(self.bootstraps, key=self.bootstraps.__getitem__)]
        value = secrets.token_urlsafe(32)
        self.bootstraps[_digest(value)] = now + self.bootstrap_ttl
        return {
            "url": self.origin + "/control/open#" + value,
            "expires_at": iso(utcnow() + timedelta(seconds=self.bootstrap_ttl)),
            "port": self.port,
        }

    def serve(self) -> None:
        server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=self.port,
                access_log=False,
                log_level="critical",
            )
        )
        if self.on_ready is not None:
            # Reported and opened only once this process actually serves the reserved socket.
            threading.Thread(target=self._when_serving, args=(server,), daemon=True).start()
        server.run(sockets=[self.socket])

    def _when_serving(self, server: uvicorn.Server) -> None:
        for _ in range(300):
            if server.started or server.should_exit:
                break
            time.sleep(0.05)
        if server.started and self.on_ready is not None:
            self.on_ready()

    def close(self) -> None:
        self.socket.close()

    def _allowed(self, request: Request, *, mutation: bool) -> bool:
        if request.headers.get("host") != self.host:
            return False
        origin = request.headers.get("origin")
        if origin is not None and origin != self.origin:
            return False
        return not (mutation and origin != self.origin)

    def _session(self, request: Request) -> tuple[str, _Session] | None:
        now = self.clock()
        self.sessions = {
            key: value for key, value in self.sessions.items() if value.expires_at > now
        }
        value = request.cookies.get(self.cookie, "")
        if not value:
            return None
        key = _digest(value)
        session = self.sessions.get(key)
        return (key, session) if session is not None else None

    @staticmethod
    def _headers(nonce: str | None = None, *, bootstrap: bool = False) -> dict[str, str]:
        script = f"script-src 'nonce-{nonce}'; " if nonce else ""
        return {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer" if bootstrap else "same-origin",
            "Content-Security-Policy": "default-src 'none'; "
            + script
            + "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
            + "form-action 'self'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }

    def _locked(self, request: Request) -> Response:
        response = HTMLResponse(_signed_out_html(), status_code=401, headers=self._headers())
        if self.cookie in request.cookies:
            response.delete_cookie(self.cookie, path="/control")
        return response

    async def open_page(self, request: Request) -> Response:
        if not self._allowed(request, mutation=False):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        nonce = secrets.token_urlsafe(16)
        return HTMLResponse(
            BOOTSTRAP_HTML.replace("__NONCE__", nonce),
            headers=self._headers(nonce, bootstrap=True),
        )

    async def session(self, request: Request) -> Response:
        if not self._allowed(request, mutation=True):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"error": "invalid_request"}, status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            value = json.loads(body).get("bootstrap")
        except (ValueError, AttributeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if not isinstance(value, str) or not 0 < len(value) <= 256:
            return JSONResponse({"error": "invalid_bootstrap"}, status_code=401)
        expiry = self.bootstraps.pop(_digest(value), None)
        if expiry is None or expiry <= self.clock():
            return JSONResponse({"error": "invalid_bootstrap"}, status_code=401)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[_digest(token)] = _Session(csrf, self.clock() + self.session_ttl)
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            self.cookie,
            token,
            path="/control",
            httponly=True,
            samesite="strict",
            max_age=int(self.session_ttl),
        )
        return response

    async def page(self, request: Request) -> Response:
        if not self._allowed(request, mutation=False):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        found = self._session(request)
        if found is None:
            return self._locked(request)
        raw, data = read_config(self.settings)
        current = exposure(data)
        proposed = current
        reviewed = request.query_params.get("review") == "1"
        message = None
        if reviewed:
            try:
                mode = request.query_params.get("mode", "")
                tools = request.query_params.getlist("tools") if mode == "selected" else []
                mode, names = validated(mode, tools, self.names)
                proposed = {"mode": mode, "tools": names}
            except TeleloomError as error:
                reviewed = False
                message = f"{error.code}: {error.message}"
        return HTMLResponse(
            _control_html(
                settings=self.settings,
                current=current,
                proposed=proposed,
                changes=diff(current, proposed["mode"], proposed["tools"]) if reviewed else [],
                digest=revision(raw),
                catalogue=self.catalogue,
                record=record(self.settings),
                blockers=blockers(self.settings),
                owner_up=_owner_held(self.settings),
                csrf=found[1].csrf,
                message=message,
            ),
            headers=self._headers(),
        )

    async def apply(self, request: Request) -> Response:
        if not self._allowed(request, mutation=True):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        found = self._session(request)
        if found is None:
            return self._locked(request)
        form = await self._form(request)
        if isinstance(form, Response):
            return form
        if not hmac.compare_digest((form.get("csrf") or [""])[0].encode(), found[1].csrf.encode()):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403)
        try:
            mode, tools = validated(
                (form.get("mode") or [""])[0],
                list(form.get("tools") or []),
                self.names,
            )
        except TeleloomError as error:
            return self._render(422, f"{error.code}: {error.message}", found[1].csrf)
        settings = self.settings
        change = Change(
            proposed={"mode": mode, "tools": tools},
            current=exposure,
            write=lambda: write_exposure(settings, mode, tools),
            verify=lambda observed: _mismatches(observed, settings, mode, tools, self.names),
            describe=lambda observed: (
                f"Applied and verified: exposure_mode={mode}, exposed_tools={tools}. "
                f"Owner {observed.get('owner_id')} answers in this workspace with build "
                f"{str((observed.get('build') or {}).get('build_id'))[:12]} and "
                f"{len(observed.get('tools') or [])} listed tools. "
                "Reconnect every MCP client to refresh its cached tool discovery."
            ),
        )
        return await self._transition((form.get("revision") or [""])[0], change, found[1].csrf)

    async def _form(self, request: Request, limit: int = 16384) -> dict[str, list[str]] | Response:
        """Bounded form body: a missing or lying Content-Length cannot grow the read."""
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > limit:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)

    async def policy(self, request: Request) -> Response:
        from . import control_policy

        return await control_policy.page(self, request)

    async def policy_apply(self, request: Request) -> Response:
        from . import control_policy

        return await control_policy.apply(self, request)

    async def proxies(self, request: Request) -> Response:
        from . import control_proxies

        return await control_proxies.page(self, request)

    async def proxy_check(self, request: Request) -> Response:
        from .control_proxies import check

        return await check(self, request)

    async def proxies_apply(self, request: Request) -> Response:
        from . import control_proxies

        return await control_proxies.apply(self, request)

    async def engines(self, request: Request) -> Response:
        from . import control_engines

        return await control_engines.page(self, request)

    async def engines_apply(self, request: Request) -> Response:
        from . import control_engines

        return await control_engines.apply(self, request)

    async def send(self, request: Request) -> Response:
        from . import control_send

        return control_send.page(self, request)

    async def send_preview(self, request: Request) -> Response:
        from . import control_send

        return await control_send.preview(self, request)

    async def send_confirm(self, request: Request) -> Response:
        from . import control_send

        return await control_send.confirm(self, request)

    async def send_plan(self, request: Request) -> Response:
        from . import control_send

        return control_send.plan_page(self, request)

    async def send_job(self, request: Request) -> Response:
        from . import control_send

        return await control_send.job_page(self, request)

    async def _transition(self, submitted: str, change: Change, csrf: str) -> Response:
        settings = self.settings
        gate = control_lock(settings)
        try:
            gate.acquire()
        except Timeout:
            return self._render(
                409, "Another Apply is running. Wait for its reported result.", csrf
            )
        try:
            value = record(settings)
            if value is not None and value.get("phase") not in TERMINAL:
                if _live(value):
                    return self._render(
                        409,
                        f"Another transition is at phase '{value['phase']}'. Wait for its result.",
                        csrf,
                    )
                value.update(phase="abandoned", reason="management process ended")
                save_record(settings, value)
            return await self._apply_locked(submitted, change, gate, csrf)
        finally:
            if gate.is_locked:
                gate.release()

    async def _apply_locked(
        self, submitted: str, change: Change, gate: FileLock, csrf: str
    ) -> Response:
        settings = self.settings
        raw, data = read_config(settings)
        if not hmac.compare_digest(submitted.encode(), revision(raw).encode()):
            return self._render(
                409,
                "This review is stale: configuration changed on disk. Both digests differ from "
                f"your review ({revision(raw)} now). Reload the page and review again. "
                "Nothing was written.",
                csrf,
            )
        if change.current(data) == change.proposed and not change.rotate:
            return self._render(
                200, "No change: this configuration is already applied. No restart.", csrf
            )
        if message := blockers(settings):
            return self._render(409, "Refused: " + " ".join(message), csrf)
        value = {
            "phase": "drafted",
            "pid": os.getpid(),
            "executable": sys.executable,
            "started_at": utcnow().isoformat(),
            "revision": revision(raw),
            "proposed": change.proposed,
            "owner": None,
            "reason": None,
            "handoff": None,
        }
        save_record(settings, value)

        def fail(message: str) -> Response:
            return self._fail(value, message, csrf)

        try:
            await self.lifecycle.stop(settings)
        except Exception as exception:  # noqa: BLE001 - normalize shutdown transport failures
            failure = _failure(exception)
            return fail(f"stop failed ({failure.code}: {failure.message}); nothing was written.")
        value["phase"] = "stopped"
        save_record(settings, value)
        owner = ownership(settings)
        try:
            owner.acquire()
        except Timeout:
            return fail(
                "owner_busy: another login or configuration change started; nothing was written.",
            )
        try:
            try:
                fresh_raw, _ = read_config(settings)
            except (OSError, TeleloomError) as exception:
                failure = _failure(exception)
                return fail(
                    f"Cannot reread configuration after stopping ({failure.code}). "
                    "Review config.json and run teleloom serve."
                )
            expected = str(value["revision"])
            if not hmac.compare_digest(revision(fresh_raw), expected):
                return fail("stale: configuration changed while stopping; nothing was written.")
            if message := blockers(settings):
                return fail(
                    "Refused after stop: "
                    + " ".join(message)
                    + " The owner is stopped; run teleloom serve to resume without changes.",
                )
            try:
                change.write()
            except OSError as error:
                return fail(
                    f"write failed ({error.errno}); config.json is unchanged. Run teleloom serve.",
                )
            except TeleloomError as error:
                return fail(f"write refused ({error.code}: {error.message}); nothing was written.")
            value["phase"], value["revision"] = (
                "written",
                revision((settings.data_dir / "config.json").read_bytes()),
            )
            save_record(settings, value)
        finally:
            owner.release()
        token = secrets.token_urlsafe(32)
        value.update(phase="starting", handoff=_digest(token))
        save_record(settings, value)
        # The gate is released here: only the one-use handoff above lets the child start.
        gate.release()
        try:
            await self.lifecycle.start(settings, token)
            observed = await self.lifecycle.observed(settings)
        except Exception as exception:  # noqa: BLE001 - report the committed configuration
            failure = _failure(exception)
            return fail(
                f"restart failed ({failure.code}: {failure.message}). The new configuration is durable; "
                "run teleloom serve or teleloom control to recover. Nothing is replayed."
            )
        try:
            mismatches = change.verify(observed)
        except Exception as exception:  # noqa: BLE001 - write succeeded; preserve the result
            mismatches = [_failure(exception).message]
        if mismatches:
            return fail(
                "verification failed: " + "; ".join(mismatches) + ". The configuration is "
                "durable; run teleloom doctor --mode mcp and inspect the configured port."
            )
        value.update(
            phase="verified",
            handoff=None,
            owner={
                "owner_id": observed.get("owner_id"),
                "workspace_id": observed.get("workspace_id"),
                "version": observed.get("version"),
                "build": observed.get("build"),
                "exposure": observed.get("exposure"),
                "tools": len(observed.get("tools") or []),
            },
        )
        save_record(settings, value)
        return self._render(200, change.describe(observed), csrf)

    def _fail(self, value: dict[str, Any], message: str, csrf: str) -> Response:
        value.update(phase="failed", reason=message, handoff=None)
        save_record(self.settings, value)
        return self._render(500, message, csrf)

    def _render(self, status: int, message: str, csrf: str) -> Response:
        try:
            raw, data = read_config(self.settings)
        except (OSError, TeleloomError):
            return HTMLResponse(
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                "<title>Owner configuration · teleloom</title>"
                "<h1>Cannot read owner configuration</h1>"
                f'<p role="alert">{_esc(message)}</p><a href="/control">Reload control</a></html>',
                status_code=status,
                headers=self._headers(),
            )
        current = exposure(data)
        return HTMLResponse(
            _control_html(
                settings=self.settings,
                current=current,
                proposed=current,
                changes=[],
                digest=revision(raw),
                catalogue=self.catalogue,
                record=record(self.settings),
                blockers=blockers(self.settings),
                owner_up=_owner_held(self.settings),
                csrf=csrf,
                message=message,
            ),
            status_code=status,
            headers=self._headers(),
        )

    def _clients_page(
        self,
        csrf: str,
        client: str,
        target: str,
        names: list[str] | None,
        reviewed: bool,
        message: str | None = None,
        status: int = 200,
    ) -> Response:
        detail = None
        skills: list[str] = []
        try:
            skills = [folder.name for folder in control_clients.bundled_skills()]
            detail = control_clients.review(
                self.settings, client, Path(target) if target else None, names
            )
        except TeleloomError as error:
            message = message or f"{error.code}: {error.message}"
        return HTMLResponse(
            control_clients.page_html(
                csrf=csrf,
                client=client,
                target=target,
                names=names,
                skills=skills,
                review=detail,
                message=message,
                reviewed=reviewed,
            ),
            status_code=status,
            headers=self._headers(),
        )

    async def clients_page(self, request: Request) -> Response:
        if not self._allowed(request, mutation=False):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        found = self._session(request)
        if found is None:
            return self._locked(request)
        reviewed = request.query_params.get("review") == "1"
        client = request.query_params.get("client", "opencode")
        target = request.query_params.get("target", "")
        names = list(request.query_params.getlist("skills")) if reviewed else None
        return self._clients_page(found[1].csrf, client, target, names, reviewed)

    async def clients_apply(self, request: Request) -> Response:
        if not self._allowed(request, mutation=True):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        found = self._session(request)
        if found is None:
            return self._locked(request)
        form = await self._form(request)
        if isinstance(form, Response):
            return form
        csrf = found[1].csrf
        if not hmac.compare_digest((form.get("csrf") or [""])[0].encode(), csrf.encode()):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403)
        client = (form.get("client") or [""])[0]
        target = (form.get("target") or [""])[0]
        names = list(form.get("skills") or [])
        reviewed = (form.get("review_hash") or [""])[0] or None
        gate = control_lock(self.settings)
        try:
            gate.acquire()
        except Timeout:
            return self._clients_page(
                csrf, client, target, names, True, "Another management action is running.", 409
            )
        try:
            result = control_clients.install(
                self.settings,
                client,
                Path(target) if target else None,
                names,
                overwrite=bool(form.get("overwrite")),
                reviewed=reviewed,
            )
        except TeleloomError as error:
            status = (
                409
                if error.code
                in {"skills_exist", "client_config_conflict", "review_required", "review_stale"}
                else 422
            )
            return self._clients_page(
                csrf, client, target, names, True, f"{error.code}: {error.message}", status
            )
        finally:
            gate.release()
        changed = (
            "Saved the client connection."
            if result["connection_saved"]
            else "The client connection was already identical and is unchanged."
        )
        message = (
            f"{changed} Skills written: {', '.join(result['installed']) or 'none'}"
            f" (already present: {', '.join(result['already_present']) or 'none'})"
            f" in {result['destination']}. Reconnect the client to reload discovery;"
            " MCP discovery is unverified here."
        )
        return self._clients_page(csrf, client, target, names, True, message)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)


def identity_mismatches(observed: dict[str, Any], settings: Settings) -> list[str]:
    """The answering owner must be this workspace and this build before any claim is made."""
    found = []
    if observed.get("service") != "teleloom":
        found.append("the answering process is not teleloom")
    if observed.get("workspace_id") != workspace_id(settings):
        found.append("the answering owner serves a different workspace")
    if observed.get("build") != build_info():
        found.append("the answering owner runs a different build")
    return found


def _mismatches(
    observed: dict[str, Any], settings: Settings, mode: str, tools: list[str], names: set[str]
) -> list[str]:
    """What the restarted owner reported versus this workspace, this build and the chosen exposure."""
    found = identity_mismatches(observed, settings)
    value = observed.get("exposure") or {}
    if value.get("mode") != mode or list(value.get("selected_tools") or []) != tools:
        found.append(f"the owner reports exposure {value} instead of {mode}/{tools}")
    listed = set(observed.get("tools") or [])
    if mode == "selected" and listed != set(tools):
        found.append(f"tools/list has {len(listed)} tools, not the {len(tools)} selected")
    if mode == "all" and listed != names:
        found.append(f"tools/list has {len(listed)} of {len(names)} registered tools")
    if mode == "read-only" and not listed <= names:
        found.append("tools/list names tools this build does not register")
    return found


def _owner_held(settings: Settings) -> bool:
    lock = ownership(settings)
    try:
        lock.acquire()
    except Timeout:
        return True
    else:
        lock.release()
        return False


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _signed_out_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Control locked · teleloom</title>
<style>:root{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}
body{margin:0;padding:8vh 24px}main{max-width:560px;margin:auto}h1{font-size:30px}
p{line-height:1.6;color:#68665d}code{background:#e8e4da;padding:.15em .4em;border-radius:4px}
</style></head><body><main><p>teleloom / owner management</p><h1>Control locked</h1>
<p>Run <code>teleloom control</code> in a terminal on this machine. Only that explicit launch
creates a one-time management session; the panel, the MCP bearer and a passive visit grant nothing.</p>
<p><a href="/control">Check again</a></p></main></body></html>"""


def _control_html(
    *,
    settings: Settings,
    current: dict[str, Any],
    proposed: dict[str, Any],
    changes: list[str],
    digest: str,
    catalogue: list[dict[str, Any]],
    record: dict[str, Any] | None,
    blockers: list[str],
    owner_up: bool,
    csrf: str,
    message: str | None,
) -> str:
    build = build_info()
    tools = "".join(
        f'<li><label><input type="checkbox" name="tools" value="{_esc(item["name"])}"'
        f"{' checked' if item['name'] in proposed['tools'] else ''}> "
        f'<code>{_esc(item["name"])}</code> <span class="small">{_esc(item.get("summary", ""))}</span>'
        f"{' <em>writes</em>' if item.get('writes') else ''}</label></li>"
        for item in catalogue
    )
    modes = "".join(
        f'<label><input type="radio" name="mode" value="{mode}"'
        f"{' checked' if mode == proposed['mode'] else ''}> {mode}</label>"
        for mode in MODES
    )
    changed = "".join(f"<li><code>{_esc(line)}</code></li>" for line in changes)
    review = (
        '<section aria-labelledby="diff"><h2 id="diff">Exact diff</h2><ul>'
        + (changed or "<li>No difference from the current configuration.</li>")
        + "</ul>"
        + '<form method="post" action="/control/apply">'
        + f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        + f'<input type="hidden" name="revision" value="{_esc(digest)}">'
        + f'<input type="hidden" name="mode" value="{_esc(proposed["mode"])}">'
        + "".join(
            f'<input type="hidden" name="tools" value="{_esc(name)}">' for name in proposed["tools"]
        )
        + '<p><button type="submit"'
        + ("" if changes else " disabled")
        + ">Apply and restart the owner</button> "
        + ("" if changes else "Nothing to apply: this exact configuration is active.")
        + "</p></form></section>"
    )
    blocked = (
        '<section aria-labelledby="blocked"><h2 id="blocked">Blocked</h2><ul>'
        + "".join(f"<li>{_esc(item)}</li>" for item in blockers)
        + "</ul></section>"
        if blockers
        else ""
    )
    transition = ""
    if record:
        owner = record.get("owner") or {}
        transition = (
            '<section aria-labelledby="transition"><h2 id="transition">Last transition</h2>'
            f"<p>Phase: <code>{_esc(record.get('phase'))}</code>"
            + (f" · reason: {_esc(record.get('reason'))}" if record.get("reason") else "")
            + (f" · verified owner <code>{_esc(owner.get('owner_id'))}</code>" if owner else "")
            + f" · started <time>{_esc(record.get('started_at'))}</time></p></section>"
        )
    result = (
        f'<section aria-labelledby="result"><h2 id="result">Result</h2><p role="status">{_esc(message)}</p></section>'
        if message
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Owner management · teleloom</title><style>
:root{{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:6vh 24px}}main{{max-width:820px;margin:auto}}
h1{{font-size:32px;letter-spacing:-.03em}}h2{{font-size:18px;margin-top:2em}}
p,li{{line-height:1.6}}code{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;border-radius:4px}}
.small{{font-size:13px;color:#68665d}}ul{{padding-left:1.2em}}.meta{{display:grid;grid-template-columns:160px 1fr;gap:8px 20px}}
button{{font:inherit;padding:10px 16px;border-radius:6px;border:1px solid #b8b4a8;background:white;cursor:pointer}}
button:focus-visible,input:focus-visible{{outline:3px solid #24231f;outline-offset:2px}}
fieldset{{border:1px solid #d8d3c7;margin-top:1.5em}}legend{{font-weight:600}}
</style></head><body><main><p class="small">teleloom / owner management · this process is not the owner</p>
<h1>Tool exposure</h1>
<p><a href="/control/user">Add a user profile</a> · <a href="/control/bot">Connect a Bot API profile</a></p>
{result}
<dl class="meta"><dt>Current exposure</dt><dd><code>{_esc(current["mode"])}</code> {_esc(current["tools"])}</dd>
<dt>Configuration revision</dt><dd><code>{_esc(digest)}</code></dd>
<dt>Owner</dt><dd>{"running" if owner_up else "stopped"}</dd>
<dt>Build</dt><dd><code>{_esc(build.get("build_id"))}</code> · {_esc(build.get("source_commit"))}</dd>
<dt>Workspace</dt><dd><code>{_esc(workspace_id(settings))}</code></dd>
<dt>Version</dt><dd>{_esc(__version__)}</dd></dl>
{blocked}{transition}
<form method="get" action="/control"><fieldset><legend>Choose an exposure mode</legend>
<p>{modes}</p><p class="small">selected uses exactly the checked tools.</p>
<ul>{tools}</ul><input type="hidden" name="review" value="1">
<p><button type="submit">Review exact diff</button></p></fieldset></form>
{review}
<p class="small"><a href="/control/policy">Profile read policy and send, broadcast or AI grants</a> ·
<a href="/control/clients">MCP clients and skills</a> ·
<a href="/control/proxies">Named proxies and profile routes</a> ·
<a href="/control/engines">AI and transcription engines</a> ·
<a href="/control/send">Send one exact message</a></p>
<p class="small">A change stops the owner, writes only exposure_mode/exposed_tools, and starts it
again under a one-use handoff. Active jobs and unknown deliveries refuse the change; nothing is
cancelled or replayed. Apply with an unchanged configuration restarts nothing.</p>
</main></body></html>"""


BOOTSTRAP_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Opening control · teleloom</title>
<style>:root{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}
body{margin:0;padding:8vh 24px}main{max-width:560px;margin:auto}h1{font-size:30px}
p{line-height:1.6;color:#68665d}#error{color:#9c2828}code{background:#e8e4da;padding:.15em .4em;border-radius:4px}
</style></head><body><main><p>teleloom / owner management</p><h1>Opening control</h1>
<p id="progress" role="status" aria-live="polite">Exchanging the one-time management bootstrap…</p>
<p id="error" role="alert" hidden></p>
<noscript><p role="alert">JavaScript is required to exchange the one-time bootstrap.</p></noscript></main>
<script nonce="__NONCE__">
const token=location.hash.slice(1);
history.replaceState(null,'','/control');
const progress=document.querySelector('#progress'),error=document.querySelector('#error');
if(!token){progress.textContent='No bootstrap was supplied. Run teleloom control again.';}
else{fetch('/control/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bootstrap:token})})
.then(r=>{if(!r.ok)throw new Error('rejected');location.replace('/control');})
.catch(()=>{progress.hidden=true;error.hidden=false;error.textContent='This bootstrap is invalid, expired or already used. Run teleloom control again.';});}
</script></body></html>"""
