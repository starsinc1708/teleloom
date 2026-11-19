"""Read-only owner panel with a short-lived, explicit browser bootstrap."""

import hashlib
import hmac
import html
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import Settings
from .models import iso, utcnow
from .runtime import Runtime
from .status import status_snapshot


@dataclass
class _Session:
    csrf: str
    expires_at: float


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Panel:
    def __init__(
        self,
        settings: Settings,
        runtime: Runtime,
        token: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        bootstrap_ttl: float = 60,
        session_ttl: float = 8 * 60 * 60,
    ) -> None:
        self.settings = settings
        self.runtime = runtime
        self.token = token
        self.clock = clock
        self.bootstrap_ttl = bootstrap_ttl
        self.session_ttl = session_ttl
        self.host = f"127.0.0.1:{settings.port}"
        self.origin = f"http://{self.host}"
        # Cookies ignore ports; a port-qualified name avoids clobbering another panel.
        self.cookie = f"teleloom_ui_{settings.port}"
        self.bootstraps: dict[str, float] = {}
        self.sessions: dict[str, _Session] = {}
        self.app = Starlette(
            routes=[
                Route("/ui", self.page),
                Route("/ui/", self.page),
                Route("/ui/open", self.open_page),
                Route("/ui/session", self.session, methods=["POST"]),
                Route("/ui/logout", self.logout, methods=["POST"]),
                Route("/ui/bootstrap", self.bootstrap, methods=["POST"]),
            ]
        )

    def issue_bootstrap(self) -> dict[str, Any]:
        now = self.clock()
        self.bootstraps = {key: end for key, end in self.bootstraps.items() if end > now}
        if len(self.bootstraps) >= 8:
            del self.bootstraps[min(self.bootstraps, key=self.bootstraps.__getitem__)]
        value = secrets.token_urlsafe(32)
        self.bootstraps[_digest(value)] = now + self.bootstrap_ttl
        return {
            "url": self.origin + "/ui/open#" + value,
            "expires_at": iso(utcnow() + timedelta(seconds=self.bootstrap_ttl)),
            "ttl_seconds": self.bootstrap_ttl,
        }

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
        if session is None:
            return None
        if session.expires_at <= self.clock():
            self.sessions.pop(key, None)
            return None
        return key, session

    @staticmethod
    def _headers(script_nonce: str | None = None) -> dict[str, str]:
        script = f"script-src 'nonce-{script_nonce}'; " if script_nonce else ""
        return {
            "Cache-Control": "no-store",
            # no-referrer makes Chromium send a null Origin on native form POSTs.
            "Referrer-Policy": "no-referrer" if script_nonce else "same-origin",
            "Content-Security-Policy": "default-src 'none'; "
            + script
            + "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
            + "form-action 'self'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }

    async def page(self, request: Request) -> Response:
        if not self._allowed(request, mutation=False):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        found = self._session(request)
        if found is None:
            response = HTMLResponse(_signed_out_html(), status_code=401, headers=self._headers())
            if self.cookie in request.cookies:
                response.delete_cookie(self.cookie, path="/ui")
            return response
        _, session = found
        snapshot = status_snapshot(self.settings, self.runtime)
        return HTMLResponse(
            _status_html(snapshot, session.csrf),
            headers=self._headers(),
        )

    async def open_page(self, request: Request) -> Response:
        if not self._allowed(request, mutation=False):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        nonce = secrets.token_urlsafe(16)
        return HTMLResponse(
            BOOTSTRAP_HTML.replace("__NONCE__", nonce),
            headers=self._headers(nonce),
        )

    async def bootstrap(self, request: Request) -> Response:
        if not self._allowed(request, mutation=False):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        supplied = request.headers.get("authorization", "")
        expected = "Bearer " + self.token
        if not secrets.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(self.issue_bootstrap(), headers={"Cache-Control": "no-store"})

    async def session(self, request: Request) -> Response:
        if not self._allowed(request, mutation=True):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"error": "invalid_request"}, status_code=415)
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 4096:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        body = await request.body()
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
        value = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self.sessions[_digest(value)] = _Session(
            csrf=csrf, expires_at=self.clock() + self.session_ttl
        )
        if len(self.sessions) > 32:
            oldest = min(self.sessions, key=lambda key: self.sessions[key].expires_at)
            self.sessions.pop(oldest, None)
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            self.cookie,
            value,
            path="/ui",
            httponly=True,
            samesite="strict",
            max_age=int(self.session_ttl),
        )
        return response

    async def logout(self, request: Request) -> Response:
        if not self._allowed(request, mutation=True):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403)
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 8192:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        body = await request.body()
        if len(body) > 8192:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        found = self._session(request)
        if found is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        key, session = found
        try:
            supplied = request.headers.get("x-csrf-token") or (await request.form()).get("csrf", "")
        except Exception:
            # Untrusted form bodies fail closed as a missing CSRF token.
            supplied = ""
        if not hmac.compare_digest(str(supplied).encode(), session.csrf.encode()):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403)
        self.sessions.pop(key, None)
        response = RedirectResponse("/ui", status_code=303, headers={"Cache-Control": "no-store"})
        response.delete_cookie(self.cookie, path="/ui")
        return response

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _signed_out_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel locked · teleloom</title><style>
:root{color-scheme:light;font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}
body{margin:0;padding:8vh 24px}main{max-width:560px;margin:auto}h1{font-size:32px;letter-spacing:-.03em}
p{line-height:1.6;color:#68665d}code{background:#e8e4da;padding:.15em .4em;border-radius:4px}
a:focus-visible,button:focus-visible{outline:3px solid #24231f;outline-offset:3px}
</style></head><body><main><p>teleloom / owner panel</p><h1>Panel locked</h1>
<p>Run <code>teleloom ui</code> in the terminal on this machine to create a new private
browser session. A passive visit to this page never creates access.</p>
<p><a href="/ui">Check again</a></p></main></body></html>"""


def _status_html(snapshot: dict[str, Any], csrf: str) -> str:
    build = snapshot["build"]
    errors = snapshot["profile_errors"]
    states = {
        "starting": "The owner is starting. This snapshot may be incomplete.",
        "error": "Some profiles could not connect. Review the errors below.",
        "empty": "No profiles are configured. Add one with teleloom auth user or auth bot.",
        "ready": "The owner is ready.",
    }
    error_rows = "".join(
        f"<li><code>{_esc(name)}</code> <span>{_esc(code)}</span></li>"
        for name, code in errors.items()
    )
    error_section = (
        f'<section aria-labelledby="errors"><h2 id="errors">Connection errors</h2>'
        f"<ul>{error_rows}</ul></section>"
        if errors
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workspace status · teleloom</title><style>
:root{{color-scheme:light;font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:7vh 24px}}main{{max-width:720px;margin:auto}}
p{{line-height:1.6;color:#68665d}}h1{{font-size:36px;letter-spacing:-.04em;margin:.2em 0}}
h2{{font-size:18px;margin-top:2em}}code{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;border-radius:4px}}
dl{{display:grid;grid-template-columns:minmax(150px,auto) 1fr;gap:12px 24px;margin:2em 0}}
dt{{font-weight:600}}dd{{margin:0;overflow-wrap:anywhere}}section{{margin-top:2em}}
.state{{border-left:4px solid #68665d;padding:.8em 1em;background:#ece8df}}
.state[data-state=error]{{border-color:#9c2828}}ul{{padding-left:1.2em}}
form{{margin-top:2.5em}}button{{font:inherit;padding:10px 16px;border-radius:6px;border:1px solid #b8b4a8;background:white;cursor:pointer}}
a:focus-visible,button:focus-visible{{outline:3px solid #24231f;outline-offset:3px}}
.small{{font-size:14px}}footer{{margin-top:3em;border-top:1px solid #d8d3c7;padding-top:1em}}
</style></head><body><main><p class="small">teleloom / owner panel</p><h1>Workspace status</h1>
<p class="state" data-state="{_esc(snapshot["state"])}" role="status">{_esc(states[snapshot["state"]])}</p>
<dl><dt>Owner ID</dt><dd><code>{_esc(snapshot["owner_id"])}</code></dd>
<dt>Workspace</dt><dd><code>{_esc(snapshot["workspace_id"])}</code></dd>
<dt>Version</dt><dd>{_esc(snapshot["version"])}</dd>
<dt>Build ID</dt><dd><code>{_esc(build["build_id"])}</code></dd>
<dt>Source commit</dt><dd><code>{_esc(build["source_commit"])}</code></dd>
<dt>Build type</dt><dd>{_esc(build["build_type"])}</dd>
<dt>Local readiness</dt><dd>{_esc(snapshot["health"]["local"])}</dd>
<dt>Configuration</dt><dd>{_esc(snapshot["configuration_status"])}</dd>
<dt>Profiles</dt><dd>{_esc(snapshot["profiles"])}</dd>
<dt>Snapshot time</dt><dd><time datetime="{_esc(snapshot["snapshot_at"])}">{_esc(snapshot["snapshot_at"])}</time></dd></dl>
{error_section}
<p><a href="/ui">Refresh status</a></p>
<form method="post" action="/ui/logout"><input type="hidden" name="csrf" value="{_esc(csrf)}">
<button type="submit">Sign out of this panel</button></form>
<footer class="small">Read-only inspector. Refreshing this page does not read Telegram or call AI.</footer>
</main></body></html>"""


BOOTSTRAP_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Opening panel · teleloom</title><style>
:root{color-scheme:light;font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}
body{margin:0;padding:8vh 24px}main{max-width:560px;margin:auto}h1{font-size:32px;letter-spacing:-.03em}
p{line-height:1.6;color:#68665d}#error{color:#9c2828}code{background:#e8e4da;padding:.15em .4em;border-radius:4px}
</style></head><body><main><p>teleloom / owner panel</p><h1>Opening your panel</h1>
<p id="progress" role="status" aria-live="polite">Preparing your private panel session…</p>
<p id="error" role="alert" hidden></p>
<p>If this does not open, run <code>teleloom ui</code> again.</p>
<noscript><p role="alert">JavaScript is required to exchange the one-time bootstrap.</p></noscript></main>
<script nonce="__NONCE__">
const token=location.hash.slice(1);
history.replaceState(null,'','/ui');
const progress=document.querySelector('#progress'),error=document.querySelector('#error');
if(!token){progress.textContent='No bootstrap was supplied. Run teleloom ui again.';}
else{fetch('/ui/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bootstrap:token})})
.then(r=>{if(!r.ok)throw new Error('rejected');location.replace('/ui');})
.catch(()=>{progress.hidden=true;error.hidden=false;error.textContent='This bootstrap is invalid, expired or already used. Run teleloom ui again.';});}
</script></body></html>"""
