import asyncio
import contextlib
import html
import io
import json
import secrets
import socket
import sys
import time
import webbrowser
from datetime import UTC, datetime
from typing import Any

import qrcode
import qrcode.image.svg
import typer
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from telethon import errors

from .models import TeleloomError


def identity(user: Any) -> dict[str, str]:
    return {"id": str(user.id), "username": user.username or "", "name": user.first_name or ""}


class LoginFlow:
    """One bounded Telethon authentication flow shared by browser and terminal."""

    def __init__(self, client: Any, timeout: float = 600) -> None:
        self.client = client
        self.timeout = timeout
        self.cancelled = asyncio.Event()
        self.passwords: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self.state: dict[str, Any] = {"phase": "starting", "message": "Connecting…"}
        self.qr: Any = None
        self.deadline = 0.0

    async def _await(self, operation: Any, timeout: float | None = None) -> Any:
        task = asyncio.ensure_future(operation)
        cancel = asyncio.create_task(self.cancelled.wait())
        remaining = max(0, self.deadline - time.monotonic())
        try:
            done, _ = await asyncio.wait(
                {task, cancel},
                timeout=min(remaining, timeout) if timeout is not None else remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel in done:
                raise TeleloomError("auth_cancelled", "Login cancelled.")
            if task not in done:
                raise TimeoutError
            return await task
        finally:
            for pending in (task, cancel):
                if not pending.done():
                    pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending

    async def run(self) -> dict[str, str]:
        self.deadline = time.monotonic() + self.timeout
        try:
            self.qr = await self._await(self.client.qr_login())
            while True:
                waiter = asyncio.create_task(self.qr.wait())
                await asyncio.sleep(
                    0
                )  # Install Telethon's update listener before publishing the QR.
                expires = self.qr.expires.astimezone(UTC)
                self.state = {
                    "phase": "qr",
                    "expires": expires.isoformat(),
                    "qr_svg": qr_svg(self.qr.url),
                    "message": "Scan in Telegram → Settings → Devices.",
                }
                try:
                    await self._await(
                        waiter, max(0.01, (expires - datetime.now(UTC)).total_seconds())
                    )
                    break
                except TimeoutError:
                    if time.monotonic() >= self.deadline:
                        raise TeleloomError(
                            "auth_timeout", "Login timed out. Start a new login."
                        ) from None
                    await self._await(self.qr.recreate())
                except errors.SessionPasswordNeededError:
                    await self._password_login()
                    break
            if not await self._await(self.client.is_user_authorized()):
                raise TeleloomError("auth_failed", "Telegram did not confirm authorization.")
            result = identity(await self._await(self.client.get_me()))
            self.state = {
                "phase": "done",
                "identity": result,
                "message": "Connected. You can close this page.",
            }
            return result
        except TimeoutError:
            self.state = {"phase": "failed", "message": "Login timed out."}
            raise TeleloomError("auth_timeout", "Login timed out. Start a new login.") from None
        except TeleloomError as exc:
            self.state = {
                "phase": "cancelled" if exc.code == "auth_cancelled" else "failed",
                "message": exc.message,
            }
            raise
        except errors.FloodWaitError as exc:
            self.state = {
                "phase": "failed",
                "message": f"Telegram requires waiting {exc.seconds} seconds.",
            }
            raise TeleloomError(
                "rate_limited", "Telegram temporarily limited login.", retry_after=exc.seconds
            ) from None
        except asyncio.CancelledError:
            self.state = {"phase": "cancelled", "message": "Login cancelled."}
            raise
        except Exception:
            self.state = {
                "phase": "failed",
                "message": "Login failed. Check connection and API credentials.",
            }
            raise TeleloomError("auth_failed", self.state["message"]) from None

    async def _password_login(self) -> None:
        self.state = {"phase": "password", "message": "Enter your Telegram cloud password."}
        while True:
            password = await self._await(self.passwords.get())
            self.state = {"phase": "password_wait", "message": "Checking password…"}
            try:
                await self._await(self.client.sign_in(password=password))
                return
            except errors.PasswordHashInvalidError:
                self.state = {"phase": "password", "message": "Incorrect password. Try again."}
            finally:
                password = ""


def qr_svg(url: str) -> str:
    qr = qrcode.QRCode(border=4, box_size=8)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


class BrowserLogin(LoginFlow):
    def __init__(self, client: Any, *, origin: str, timeout: float = 600) -> None:
        super().__init__(client, timeout)
        self.origin = origin
        self.nonce = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.entry_path = f"/auth/{self.nonce}"
        self.app = Starlette(
            routes=[
                Route(self.entry_path, self.page),
                Route("/state", self.status),
                Route("/password", self.password, methods=["POST"]),
                Route("/cancel", self.cancel, methods=["POST"]),
            ]
        )

    def allowed(self, request: Request, *, entry: bool = False, write: bool = False) -> bool:
        if request.headers.get("host") != self.origin.removeprefix("http://"):
            return False
        origin = request.headers.get("origin")
        if origin is not None and origin != self.origin:
            return False
        if not entry and not secrets.compare_digest(
            request.cookies.get("teleloom_auth", "").encode(), self.nonce.encode()
        ):
            return False
        return not (
            write
            and (
                origin != self.origin
                or not secrets.compare_digest(
                    request.headers.get("x-csrf-token", "").encode(), self.csrf.encode()
                )
            )
        )

    @staticmethod
    def response(data: dict[str, Any], status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    async def page(self, request: Request) -> Response:
        if not self.allowed(request, entry=True):
            return self.response({"error": "forbidden"}, 403)
        response = HTMLResponse(
            LOGIN_HTML.replace("__CSRF__", html.escape(self.csrf)),
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; script-src 'nonce-"
                + self.csrf
                + "'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )
        response.set_cookie(
            "teleloom_auth", self.nonce, httponly=True, samesite="strict", max_age=600
        )
        return response

    async def status(self, request: Request) -> Response:
        if not self.allowed(request):
            return self.response({"error": "forbidden"}, 403)
        return self.response(self.state)

    async def password(self, request: Request) -> Response:
        if not self.allowed(request, write=True):
            return self.response({"error": "forbidden"}, 403)
        if self.state["phase"] != "password" or not self.passwords.empty():
            return self.response({"error": "password_not_requested"}, 409)
        body = await request.body()
        if len(body) > 8192:
            return self.response({"error": "request_too_large"}, 413)
        try:
            password = json.loads(body).get("password")
        except (ValueError, AttributeError):
            return self.response({"error": "invalid_request"}, 400)
        if not isinstance(password, str) or not password or len(password) > 1024:
            return self.response({"error": "invalid_password"}, 400)
        self.passwords.put_nowait(password)
        return self.response({"accepted": True}, 202)

    async def cancel(self, request: Request) -> Response:
        if not self.allowed(request, write=True):
            return self.response({"error": "forbidden"}, 403)
        self.cancelled.set()
        return self.response({"cancelled": True})


async def terminal_ui(flow: LoginFlow) -> None:
    last: tuple[str, str] | None = None
    while flow.state["phase"] not in {"done", "failed", "cancelled"}:
        state = flow.state.copy()
        marker = (state["phase"], state.get("expires", state.get("message", "")))
        if state["phase"] == "qr" and marker != last:
            typer.echo("\nScan using Telegram → Settings → Devices → Link Desktop Device")
            qr = qrcode.QRCode(border=4)
            qr.add_data(flow.qr.url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            typer.echo(f"QR expires: {state['expires']}")
        elif state["phase"] == "password" and flow.passwords.empty():
            typer.echo(state["message"])
            try:
                password = await console_prompt("Telegram cloud password")
            except BaseException:
                flow.cancelled.set()
                raise
            if flow.state["phase"] == "password":
                await flow.passwords.put(password)
            password = ""
        last = marker
        await asyncio.sleep(0.15)


async def qr_login(client: Any, ui: str = "browser", timeout: float = 600) -> dict[str, str]:
    renderer: asyncio.Task[None] | None
    if ui == "terminal":
        flow = LoginFlow(client, timeout)
        if not sys.stdin.isatty():
            raise TeleloomError(
                "terminal_required",
                "Terminal QR login requires an interactive terminal. Use --ui browser.",
            )
        renderer = asyncio.create_task(terminal_ui(flow))
        try:
            return await flow.run()
        finally:
            renderer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renderer
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    browser = BrowserLogin(client, origin=origin, timeout=timeout)
    server = uvicorn.Server(
        uvicorn.Config(
            browser.app, host="127.0.0.1", port=port, log_level="critical", access_log=False
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if serving.done():
                await serving
                raise TeleloomError(
                    "browser_unavailable", "Local authorization server could not start."
                )
            if server.started:
                break
            await asyncio.sleep(0.02)
        try:
            opened = await asyncio.to_thread(webbrowser.open, origin + browser.entry_path)
        except webbrowser.Error:
            opened = False
        renderer = None
        if not opened:
            if not sys.stdin.isatty():
                raise TeleloomError(
                    "browser_unavailable",
                    "Browser could not open; retry --ui terminal in an interactive terminal.",
                )
            typer.echo("Browser could not open. Showing QR in this terminal.")
            renderer = asyncio.create_task(terminal_ui(browser))
        try:
            result = await browser.run()
            # Give the page time to display successful authentication before shutting down.
            await asyncio.sleep(1.5)
            return result
        finally:
            if renderer:
                renderer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renderer
    finally:
        server.should_exit = True
        await serving
        sock.close()


async def code_login(client: Any, timeout: float = 600) -> dict[str, str]:
    async with asyncio.timeout(timeout):
        phone = await console_prompt("Phone number including country code", hidden=False)
        await client.send_code_request(phone)
        while True:
            code = await console_prompt("Telegram login code")
            try:
                await client.sign_in(phone=phone, code=code)
                break
            except errors.PhoneCodeInvalidError:
                typer.echo("Invalid code. Try again.")
            except errors.SessionPasswordNeededError:
                while True:
                    password = await console_prompt("Telegram cloud password")
                    try:
                        await client.sign_in(password=password)
                        break
                    except errors.PasswordHashInvalidError:
                        typer.echo("Incorrect password. Try again.")
                    finally:
                        password = ""
                break
        if not await client.is_user_authorized():
            raise TeleloomError("auth_failed", "Telegram did not confirm authorization.")
        return identity(await client.get_me())


async def console_prompt(label: str, hidden: bool = True) -> str:
    """Cancelable console input, without a blocked executor thread after the auth deadline."""
    if not sys.stdin.isatty():
        raise TeleloomError("terminal_required", "This login step needs an interactive terminal.")
    typer.echo(label + ": ", nl=False)
    saved = None
    if sys.platform != "win32":
        import termios
        import tty

        saved = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        attributes = termios.tcgetattr(sys.stdin.fileno())
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attributes)
    chars: list[str] = []
    try:
        while True:
            character = None
            if sys.platform == "win32":
                import msvcrt

                if msvcrt.kbhit():
                    character = msvcrt.getwch()
                    if character in {"\x00", "\xe0"}:
                        msvcrt.getwch()
                        character = None
            else:
                import select

                if select.select([sys.stdin], [], [], 0)[0]:
                    character = sys.stdin.read(1)
            if character in {"\r", "\n"}:
                if chars:
                    typer.echo()
                    return "".join(chars)
            elif character in {"\x03", "\x04"}:
                raise TeleloomError("auth_cancelled", "Login cancelled.")
            elif character in {"\b", "\x7f"}:
                if chars:
                    chars.pop()
                    if not hidden:
                        typer.echo("\b \b", nl=False)
            elif character and character.isprintable() and len(chars) < 1024:
                chars.append(character)
                if not hidden:
                    typer.echo(character, nl=False)
            await asyncio.sleep(0.025)
    finally:
        chars.clear()
        if saved is not None and sys.platform != "win32":
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved)


LOGIN_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect · teleloom</title>
<style>
:root{color-scheme:light;font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}
body{margin:0;padding:7vh 24px}main{max-width:440px;margin:auto}small{letter-spacing:.15em;text-transform:uppercase;color:#68665d}
h1{font-size:36px;letter-spacing:-.04em;margin:24px 0 12px}p{line-height:1.6;color:#68665d}
#qr{background:white;border-radius:12px;min-height:280px;display:flex;align-items:center;justify-content:center;margin:28px 0}
#qr svg{width:280px;height:280px}input,button{font:inherit;padding:14px;border-radius:8px;border:1px solid #b8b4a8}
input{box-sizing:border-box;width:100%;background:white}button{cursor:pointer;background:#24231f;color:white;margin-top:12px}
#cancel{background:transparent;color:#68665d;border:0;padding-left:0}#password{display:none}#error{color:#9c2828}
</style><main><small>teleloom / account connection</small><h1>Connect your account</h1>
<p>On your phone, open Telegram → Settings → Devices → Link Desktop Device and scan this code.</p>
<div id="qr" aria-label="Telegram login QR">Preparing QR…</div><p id="status" role="status" aria-live="polite"></p>
<form id="password"><label for="secret">Telegram cloud password</label><input id="secret" type="password" autocomplete="current-password" required maxlength="1024"><button>Connect</button></form>
<p id="error" role="alert"></p><button id="cancel" type="button">Cancel connection</button></main>
<script nonce="__CSRF__">
const csrf='__CSRF__'; let finished=false;
const status=document.querySelector('#status'),qr=document.querySelector('#qr'),form=document.querySelector('#password');
async function poll(){if(finished)return;try{const r=await fetch('/state',{cache:'no-store'});if(!r.ok)throw Error('Session unavailable');const s=await r.json();
status.textContent=s.message;form.style.display=s.phase==='password'?'block':'none';
if(s.phase==='qr'){qr.style.display='flex';qr.innerHTML=s.qr_svg;const left=Math.max(0,Math.ceil((Date.parse(s.expires)-Date.now())/1000));status.textContent+=' Expires in '+left+'s; refreshes automatically.';}
else{qr.replaceChildren();qr.style.display='none';}
if(s.phase==='done'){finished=true;document.querySelector('h1').textContent='Connected';status.textContent='Connected as '+(s.identity.username||s.identity.name||s.identity.id)+'. You can close this page.';document.querySelector('#cancel').hidden=true;}
if(['failed','cancelled'].includes(s.phase)){finished=true;document.querySelector('#cancel').hidden=true;}
}catch(e){document.querySelector('#error').textContent='Local connection unavailable. Check the terminal.';}if(!finished)setTimeout(poll,400);}
form.addEventListener('submit',async e=>{e.preventDefault();const input=document.querySelector('#secret');let password=input.value;input.value='';try{await fetch('/password',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({password})});}finally{password='';}});
document.querySelector('#cancel').onclick=()=>fetch('/cancel',{method:'POST',headers:{'X-CSRF-Token':csrf}});poll();
</script></html>"""
