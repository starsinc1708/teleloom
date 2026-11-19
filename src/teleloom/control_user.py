"""Owner-only named user-profile onboarding inside the control process.

The browser never gets a second backend or session owner: this module uses the
control session/CSRF boundary, the one `control.lock` -> `owner.lock` order, and the
same bounded `LoginFlow` that `BrowserLogin` drives for QR/2FA, expiry, cancel and
timeout. Name, existing profile and API-credential prerequisites are validated
before the owner is stopped; a session string goes to the OS credential store only
after Telegram confirms the identity, and a password is only ever queued.
"""

import asyncio
import hmac
import html
import json
import secrets
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .auth import LoginFlow
from .config import Profile, Settings, validate_name
from .control import transition
from .models import TeleloomError
from .secrets import Secrets

TERMINAL_PHASES = frozenset({"done", "failed", "cancelled"})


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


class Onboarding:
    """One explicit named profile login at a time, owned by the control process."""

    def __init__(
        self,
        control: Any,
        *,
        credentials: Any = None,
        connector: Callable[[str, Profile, Any], Any] | None = None,
        timeout: float = 600,
    ) -> None:
        self.control = control
        self.credentials = credentials or Secrets()
        self.connector = connector or self._connect
        self.timeout = timeout
        self.flow: LoginFlow | None = None
        self.task: asyncio.Task[Any] | None = None
        self.plan: dict[str, Any] | None = None
        self.phase = "idle"
        self.message: str | None = None
        self.identity: dict[str, str] | None = None

    @property
    def settings(self) -> Settings:
        return self.control.settings

    def _connect(self, name: str, config: Profile, credentials: Any) -> Any:
        from .adapters import user_client

        settings = Settings.load(self.settings.data_dir)
        existing = settings.profiles.get(name)
        config.proxy = existing.proxy if existing else None
        return user_client(name, config, credentials, new=True, settings=settings)

    def routes(self) -> list[Route]:
        return [
            Route("/control/user", self.page),
            Route("/control/user/start", self.start, methods=["POST"]),
            Route("/control/user/state", self.state),
            Route("/control/user/password", self.password, methods=["POST"]),
            Route("/control/user/cancel", self.cancel, methods=["POST"]),
        ]

    # -- validation before anything is stopped or connected -------------------

    def plan_for(self, name: str, replace: bool) -> dict[str, Any]:
        """Validate the name, the existing profile and the prerequisites; no login yet."""
        try:
            validate_name(name)
        except ValueError as error:
            raise TeleloomError("invalid_name", str(error)) from None
        if name in self.settings.profiles and not replace:
            raise TeleloomError(
                "profile_exists",
                "A profile with this name already exists. Choose another name, or replace "
                "this one explicitly; a name match never rotates an identity.",
            )
        api_id = self.credentials.get(name, "api_id")
        if not self.credentials.get(name, "api_hash"):
            api_id = None
        if not api_id:
            raise TeleloomError(
                "credentials_missing",
                f"No Telegram API credentials for '{name}' are stored. Run "
                f"teleloom auth user --profile {name} once to store them on this machine.",
            )
        try:
            number = int(api_id)
        except ValueError:
            raise TeleloomError(
                "credentials_invalid", "The stored API ID is not an integer."
            ) from None
        # A new GUI profile is selected with no chats: no read, send, AI or
        # management grant exists until the owner adds one after login.
        config = Profile(kind="user", api_id=number, read_mode="selected")
        return {"profile": name, "replace": replace, "config": config}

    # -- HTTP surface ---------------------------------------------------------

    def _guard(self, request: Request, *, mutation: bool) -> tuple[Any, Response | None]:
        if not self.control._allowed(request, mutation=mutation):
            return None, JSONResponse({"error": "forbidden_origin"}, status_code=403)
        found = self.control._session(request)
        if found is None:
            return None, self.control._locked(request)
        return found, None

    @staticmethod
    def _csrf(form: Any, found: Any) -> Response | None:
        sent = str(form.get("csrf", ""))
        if not hmac.compare_digest(sent.encode(), found[1].csrf.encode()):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403)
        return None

    async def page(self, request: Request) -> Response:
        found, denied = self._guard(request, mutation=False)
        if denied is not None:
            return denied
        return self._render(found[1].csrf)

    async def start(self, request: Request) -> Response:
        found, denied = self._guard(request, mutation=True)
        if denied is not None:
            return denied
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 16384:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        form = await request.form()
        refused = self._csrf(form, found)
        if refused is not None:
            return refused
        if self.task is not None and not self.task.done():
            return self._render(found[1].csrf, 409, "A login is already running in this process.")
        try:
            plan = self.plan_for(str(form.get("name", "")).strip(), form.get("replace") == "1")
        except TeleloomError as error:
            return self._render(found[1].csrf, 422, f"{error.code}: {error.message}")
        self.plan, self.identity, self.flow = plan, None, None
        self.phase, self.message = "starting", "Validated. Login starts after the owner stops."
        self.task = asyncio.create_task(self._login())
        return self._render(found[1].csrf, 202)

    async def state(self, request: Request) -> Response:
        _, denied = self._guard(request, mutation=False)
        if denied is not None:
            return denied
        return JSONResponse(self.snapshot(), headers={"Cache-Control": "no-store"})

    async def password(self, request: Request) -> Response:
        found, denied = self._guard(request, mutation=True)
        if denied is not None:
            return denied
        if not hmac.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), found[1].csrf.encode()
        ):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403)
        if (
            self.flow is None
            or self.flow.state["phase"] != "password"
            or not self.flow.passwords.empty()
        ):
            return JSONResponse({"error": "password_not_requested"}, status_code=409)
        body = await request.body()
        if len(body) > 8192:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            password = json.loads(body).get("password")
        except (ValueError, AttributeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if not isinstance(password, str) or not password or len(password) > 1024:
            return JSONResponse({"error": "invalid_password"}, status_code=400)
        self.flow.passwords.put_nowait(password)
        password = ""
        return JSONResponse({"accepted": True}, status_code=202)

    async def cancel(self, request: Request) -> Response:
        found, denied = self._guard(request, mutation=True)
        if denied is not None:
            return denied
        if not hmac.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), found[1].csrf.encode()
        ):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403)
        if self.flow is None or self.task is None or self.task.done():
            return JSONResponse({"error": "login_not_running"}, status_code=409)
        self.flow.cancelled.set()
        return JSONResponse({"cancelled": True})

    def snapshot(self) -> dict[str, Any]:
        running = self.task is not None and not self.task.done()
        if running and self.flow is not None:
            return {**self.flow.state, "profile": (self.plan or {}).get("profile")}
        return {
            "phase": self.phase,
            "message": self.message,
            "profile": (self.plan or {}).get("profile"),
            "identity": self.identity,
        }

    # -- the login itself -----------------------------------------------------

    async def _login(self) -> None:
        plan = self.plan or {}
        record, error = await transition(
            self.control.lifecycle,
            self.settings,
            {"operation": "user_login", "profile": plan.get("profile")},
            lambda: self._authorize(plan),
        )
        if error is not None:
            self.phase = "cancelled" if error.code == "auth_cancelled" else "failed"
            self.message = f"{error.code}: {error.message}"
        owner = (record or {}).get("owner") or {}
        if owner:
            self._note(f"Owner {owner.get('owner_id')} answers in this workspace.")

    async def _authorize(self, plan: dict[str, Any]) -> None:
        """Runs under owner.lock: the whole QR/2FA login, including its awaits."""
        name, config = plan["profile"], plan["config"]
        client = self.connector(name, config, self.credentials)
        try:
            await client.connect()
            self.flow = LoginFlow(client, timeout=self.timeout)
            self.phase = "login"
            identity = await self.flow.run()
            self._save(plan, identity, client)
        finally:
            await client.disconnect()

    def _save(self, plan: dict[str, Any], identity: dict[str, str], client: Any) -> None:
        """Write the session and profile only after Telegram confirmed the identity.

        The session goes in first and is put back if the configuration cannot be saved, so
        a failed save or a cancel never costs a working profile its credential. A profile
        that appeared while this login ran is refused, never overwritten by a name match.
        """
        from telethon.sessions import StringSession

        settings, name, config = self.settings, plan["profile"], plan["config"]
        fresh = Settings.load(settings.data_dir)
        if name in fresh.profiles and not plan.get("replace"):
            raise TeleloomError(
                "profile_exists",
                f"A profile named '{name}' appeared while this login ran. Nothing was saved.",
            )
        other = next(
            (
                key
                for key, value in fresh.profiles.items()
                if key != name and value.identity.get("id") == identity.get("id")
            ),
            None,
        )
        if other:
            raise TeleloomError(
                "account_in_use",
                f"This Telegram account is already profile '{other}'. Replacing it must be "
                f"explicit: choose '{other}' and the replace option. Nothing was saved.",
            )
        previous = self.credentials.get(name, "session")
        self.credentials.set(name, "session", StringSession.save(client.session))
        try:
            config.identity = identity
            fresh.profiles[name] = config
            fresh.save()
        except Exception as error:
            if previous:
                with suppress(TeleloomError):
                    self.credentials.set(name, "session", previous)
            raise TeleloomError(
                "save_failed",
                f"config.json could not be saved ({type(error).__name__}); the previous "
                "credential was restored and no profile changed.",
            ) from None
        settings.profiles = fresh.profiles
        self.identity = identity
        self.phase = "done"
        self.message = (
            f"Connected as {identity.get('username') or identity.get('name') or identity.get('id')} "
            f"and saved as '{name}'. Read policy is selected with no chats and no grants."
        )

    def _note(self, text: str) -> None:
        self.message = f"{self.message} {text}" if self.message else text

    # -- page -----------------------------------------------------------------

    def _render(self, csrf: str, status: int = 200, message: str | None = None) -> Response:
        nonce = secrets.token_urlsafe(16)
        return HTMLResponse(
            _page(self.settings, csrf, nonce, self.snapshot(), message or self.message),
            status_code=status,
            headers=self.control._headers(nonce),
        )


def _profiles(settings: Settings) -> str:
    if not settings.profiles:
        return '<p class="small">No profiles yet.</p>'
    return (
        "<dl>"
        + "".join(
            f"<dt>{_esc(name)}</dt><dd>{_esc(value.kind)} · id "
            f"<code>{_esc(value.identity.get('id', 'unknown'))}</code> · "
            f"@{_esc(value.identity.get('username') or 'not reported')} · read "
            f"<code>{_esc(value.read_mode)}</code></dd>"
            for name, value in sorted(settings.profiles.items())
        )
        + "</dl>"
    )


def _page(
    settings: Settings, csrf: str, nonce: str, state: dict[str, Any], message: str | None
) -> str:
    poll = "1" if state.get("phase") not in TERMINAL_PHASES | {"idle"} else ""
    note = f'<p role="status" class="state">{_esc(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Add a user profile · teleloom</title><style>
:root{{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:6vh 24px}}main{{max-width:760px;margin:auto}}
h1{{font-size:32px;letter-spacing:-.03em}}h2{{font-size:18px;margin-top:2em}}
p,li{{line-height:1.6}}code{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;border-radius:4px}}
dl{{display:grid;grid-template-columns:minmax(140px,auto) 1fr;gap:8px 20px}}
small,.small{{font-size:13px;color:#68665d}}
form{{display:grid;gap:.7em;max-width:420px;margin:1.4em 0}}label{{font-weight:600;font-size:14px}}
input{{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid #b8b4a8;background:white}}
input[type=checkbox]{{width:auto}}
button{{font:inherit;padding:10px 16px;border-radius:6px;border:1px solid #b8b4a8;background:white;cursor:pointer;width:max-content}}
button:focus-visible,input:focus-visible,a:focus-visible{{outline:3px solid #24231f;outline-offset:2px}}
.state{{border-left:4px solid #68665d;padding:.7em 1em;background:#ece8df}}
#qr{{background:white;border-radius:12px;min-height:240px;display:flex;align-items:center;justify-content:center}}
#qr svg{{width:240px;height:240px}}
</style></head><body><main>
<p class="small">teleloom / owner management · this process is not the owner</p>
<h1>Add a user profile</h1>
<p><a href="/control">Tool exposure</a></p>
{note}
<section aria-labelledby="policy"><h2 id="policy">What the new profile gets</h2>
<p>Read policy <code>selected</code> with no chats, and no send, broadcast, AI, event,
management or other grants. Login itself grants nothing, and this form never changes an
existing profile's read policy or grants.</p>
{_profiles(settings)}</section>
<form id="start" method="post" action="/control/user/start">
<label for="name">New profile name</label>
<input id="name" name="name" required pattern="[a-z][a-z0-9_]{{0,47}}" maxlength="48"
 autocomplete="off" spellcheck="false" aria-describedby="name-hint">
<span id="name-hint" class="small">Lowercase letter first; letters, digits or underscores.</span>
<label><input type="checkbox" name="replace" value="1"> Replace the existing profile with this name (new generation, grants reset)</label>
<input type="hidden" name="csrf" value="{_esc(csrf)}">
<button type="submit">Validate and start QR login</button>
</form>
<section id="login" aria-labelledby="login-h"><h2 id="login-h">Login</h2>
<p class="small" id="profile-line"></p>
<p id="status" role="status" aria-live="polite">Not started. Start a login above.</p>
<div id="qr" aria-label="Telegram login QR" hidden></div>
<form id="password" hidden>
<label for="secret">Telegram cloud password</label>
<input id="secret" type="password" autocomplete="current-password" maxlength="1024" required>
<button type="submit">Send password</button>
<span class="small">Sent once to this local process; never stored or shown again.</span>
</form>
<button id="cancel" type="button" hidden>Cancel login</button>
</section>
<script nonce="{_esc(nonce)}" data-poll="{poll}">
const csrf={json.dumps(csrf)};
const status=document.querySelector('#status'),qr=document.querySelector('#qr'),
form=document.querySelector('#password'),cancel=document.querySelector('#cancel'),
line=document.querySelector('#profile-line');
let done=document.querySelector('script[data-poll]').dataset.poll!=='1';
function show(s){{
  status.textContent=s.message||s.phase; line.textContent=s.profile?('Profile: '+s.profile):'';
  form.hidden=s.phase!=='password'; cancel.hidden=['qr','password','password_wait','starting'].indexOf(s.phase)<0;
  if(s.phase==='qr'){{qr.hidden=false;qr.innerHTML=s.qr_svg;status.textContent+=' Expires in '+
    Math.max(0,Math.ceil((Date.parse(s.expires)-Date.now())/1000))+'s; refreshes automatically.';}}
  else{{qr.hidden=true;qr.replaceChildren();}}
  if(['done','failed','cancelled'].indexOf(s.phase)>=0){{done=true;
    if(!window.__reloaded){{window.__reloaded=true;setTimeout(()=>location.reload(),600);}}}}
}}
async function poll(){{if(done)return;try{{
  const r=await fetch('/control/user/state',{{cache:'no-store'}});if(!r.ok)throw Error();
  show(await r.json());}}catch(e){{status.textContent='Local status unavailable. Check the terminal.';}}
  if(!done)setTimeout(poll,500);}}
form.addEventListener('submit',async e=>{{e.preventDefault();
  const input=document.querySelector('#secret');let password=input.value;input.value='';
  try{{await fetch('/control/user/password',{{method:'POST',
    headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},
    body:JSON.stringify({{password}})}});}}finally{{password='';input.value='';}});
cancel.addEventListener('click',()=>fetch('/control/user/cancel',{{method:'POST',
  headers:{{'X-CSRF-Token':csrf}}}}));
poll();
</script></main></body></html>"""
