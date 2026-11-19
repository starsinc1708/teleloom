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
import time
from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .auth import LoginFlow
from .config import Profile, Settings, validate_name
from .control import read_config, revision, transition, write_config
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
        self.cancelled = asyncio.Event()
        self.failed_locks: list[Any] = []

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
        raw, data = read_config(self.settings)
        existing = (data.get("profiles") or {}).get(name)
        if existing is not None and not replace:
            raise TeleloomError(
                "profile_exists",
                "A profile with this name already exists. Choose another name, or replace "
                "this one explicitly; a name match never rotates an identity.",
            )
        api_id = self.credentials.get(name, "api_id") or (existing or {}).get("api_id")
        if not self.credentials.get(name, "api_hash"):
            api_id = None
        if not api_id:
            raise TeleloomError(
                "credentials_missing",
                f"No Telegram API credentials for '{name}' are available. Supply them through "
                "the OS credential backend or the profile's API environment variables.",
            )
        try:
            number = int(api_id)
            if number <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise TeleloomError(
                "credentials_invalid", "The stored API ID must be a positive integer."
            ) from None
        # A new GUI profile is selected with no chats: no read, send, AI or
        # management grant exists until the owner adds one after login.
        config = Profile(kind="user", api_id=number, read_mode="selected")
        return {"profile": name, "revision": revision(raw), "config": config}

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
        sent = (form.get("csrf") or [""])[0]
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
        form = await self.control._form(request)
        if isinstance(form, Response):
            return form
        refused = self._csrf(form, found)
        if refused is not None:
            return refused
        if self.task is not None and not self.task.done():
            return self._render(found[1].csrf, 409, "A login is already running in this process.")
        try:
            plan = self.plan_for(
                (form.get("name") or [""])[0].strip(), form.get("replace") == ["1"]
            )
        except TeleloomError as error:
            return self._render(found[1].csrf, 422, f"{error.code}: {error.message}")
        if form.get("revision") != [plan["revision"]]:
            return self._render(
                found[1].csrf, 409, "Configuration changed. Review the profile and retry."
            )
        self.plan, self.identity, self.flow = plan, None, None
        self.cancelled = asyncio.Event()
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
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 8192:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body.extend(chunk)
        if (
            self.flow is None
            or self.flow.state["phase"] != "password"
            or not self.flow.passwords.empty()
        ):
            return JSONResponse({"error": "password_not_requested"}, status_code=409)
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
        if (
            self.task is None
            or self.task.done()
            or self.phase == "saving"
            or (self.flow is not None and self.flow.state["phase"] in TERMINAL_PHASES)
        ):
            return JSONResponse({"error": "login_not_running"}, status_code=409)
        self.cancelled.set()
        return JSONResponse({"cancelled": True})

    def snapshot(self) -> dict[str, Any]:
        running = self.task is not None and not self.task.done()
        if running and self.phase not in TERMINAL_PHASES:
            if self.flow is not None and self.flow.state["phase"] not in TERMINAL_PHASES:
                return {**self.flow.state, "profile": (self.plan or {}).get("profile")}
            if self.flow is not None:
                return {"phase": "saving", "message": "Finishing login and restoring the owner."}
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
            verify=lambda observed: profile_mismatches(observed, plan["profile"], plan["config"]),
        )
        if error is not None:
            self.phase = "cancelled" if error.code == "auth_cancelled" else "failed"
            self.message = f"{error.code}: {error.message}"
        elif self.identity:
            self.phase = "done"
        self.flow = None
        owner = (record or {}).get("owner") or {}
        if owner:
            self._note(f"Owner {owner.get('owner_id')} answers in this workspace.")

    async def _authorize(self, plan: dict[str, Any]) -> None:
        """Runs under owner.lock: the whole QR/2FA login, including its awaits."""
        name, config = plan["profile"], plan["config"]
        from .session_state import acquire_session

        if self.cancelled.is_set():
            raise TeleloomError("auth_cancelled", "Login cancelled.")
        raw, _ = read_config(self.settings)
        if revision(raw) != plan["revision"]:
            raise TeleloomError(
                "stale_revision", "Configuration changed. Review the profile and retry."
            )
        client = self.connector(name, config, self.credentials)
        self.flow = LoginFlow(client, timeout=self.timeout)
        self.flow.cancelled = self.cancelled
        self.flow.deadline = time.monotonic() + self.timeout
        lock = None
        try:
            try:
                await self.flow._await(client.connect())
            except TimeoutError:
                raise TeleloomError(
                    "auth_timeout", "Connection timed out. Start a new login."
                ) from None
            lock = acquire_session(client.session)
            self.phase = "login"
            identity = await self.flow.run()
            self.phase = "saving"
            self._save(plan, identity, client)
        finally:
            try:
                await client.disconnect()
            except BaseException:
                if lock is not None:
                    self.failed_locks.append(lock)
                raise
            else:
                if lock is not None:
                    lock.release()

    def _save(self, plan: dict[str, Any], identity: dict[str, str], client: Any) -> None:
        """Write the session and profile only after Telegram confirmed the identity.

        The session goes in first and is put back if the configuration cannot be saved, so
        a failed save or a cancel never costs a working profile its credential. A profile
        that appeared while this login ran is refused, never overwritten by a name match.
        """
        from telethon.sessions import StringSession

        settings, name, config = self.settings, plan["profile"], plan["config"]
        if getattr(client.session, "auth_key", None) is None:
            raise TeleloomError("auth_failed", "Telegram supplied no valid session.")
        raw, data = read_config(settings)
        if revision(raw) != plan["revision"]:
            raise TeleloomError(
                "stale_revision", "Configuration changed. Review the profile and retry."
            )
        fresh = Settings.load(settings.data_dir)
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
            data.setdefault("profiles", {})[name] = config.model_dump(mode="json")
            write_config(settings, data)
        except Exception as error:
            try:
                if previous:
                    self.credentials.set(name, "session", previous)
                else:
                    self.credentials.delete(name, "session")
            except TeleloomError:
                raise TeleloomError(
                    "rollback_failed",
                    "The configuration is unchanged, but its OS credential could not be restored.",
                ) from None
            raise TeleloomError(
                "save_failed",
                f"config.json could not be saved ({type(error).__name__}); the profile was not changed.",
            ) from None
        settings.profiles = {**fresh.profiles, name: config}
        self.identity = identity
        self.message = (
            f"Connected as {identity.get('username') or identity.get('name') or identity.get('id')} "
            f"and saved as '{name}'. Read policy is selected with no chats and no grants."
        )

    def _note(self, text: str) -> None:
        self.message = f"{self.message} {text}" if self.message else text

    # -- page -----------------------------------------------------------------

    def _render(self, csrf: str, status: int = 200, message: str | None = None) -> Response:
        nonce = secrets.token_urlsafe(16)
        raw, data = read_config(self.settings)
        fresh = Settings.model_validate({**data, "data_dir": self.settings.data_dir})
        return HTMLResponse(
            _page(fresh, csrf, nonce, revision(raw), self.snapshot(), message or self.message),
            status_code=status,
            headers=self.control._headers(nonce),
        )


def profile_mismatches(observed: dict[str, Any], name: str, profile: Profile) -> list[str]:
    found = next(
        (
            item
            for item in ((observed.get("scope") or {}).get("profiles") or [])
            if item.get("id") == name
        ),
        None,
    )
    expected = {
        "identity": profile.identity,
        "kind": profile.kind,
        "backend": "mtproto" if profile.kind == "user" else profile.bot_backend,
        "grants": profile.grants(),
    }
    if found is None or any(found.get(key) != value for key, value in expected.items()):
        return [f"The restarted owner does not report the reviewed profile {name}."]
    if found.get("error"):
        return [f"The restarted owner reports a connection error for profile {name}."]
    if (found.get("capabilities") or {}).get("polling_enabled") != profile.polling:
        return [f"The restarted owner reports a different polling setting for profile {name}."]
    return []


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
    settings: Settings,
    csrf: str,
    nonce: str,
    digest: str,
    state: dict[str, Any],
    message: str | None,
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
#qr[hidden],form[hidden]{{display:none}}
#qr svg{{width:240px;height:240px}}
</style></head><body><main>
<p class="small">teleloom / owner management · this process is not the owner</p>
<h1>Add a user profile</h1>
<p><a href="/control">Tool exposure</a></p>
{note}
<section aria-labelledby="policy"><h2 id="policy">What the new profile gets</h2>
<p>Read policy <code>selected</code> with no chats, and no send, broadcast, AI, event,
management or other grants. Login itself grants nothing, and this form never changes an
existing profile without explicit replacement.</p>
{_profiles(settings)}</section>
<form id="start" method="post" action="/control/user/start">
<label for="name">New profile name</label>
<input id="name" name="name" required pattern="[a-z][a-z0-9_]{{0,47}}" maxlength="48"
 autocomplete="off" spellcheck="false" aria-describedby="name-hint">
<span id="name-hint" class="small">Lowercase letter first; letters, digits or underscores.</span>
<label><input type="checkbox" name="replace" value="1"> Replace the existing profile with this name (new generation, grants reset)</label>
<input type="hidden" name="csrf" value="{_esc(csrf)}">
<input type="hidden" name="revision" value="{_esc(digest)}">
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
    if(!window.__reloaded){{window.__reloaded=true;setTimeout(()=>location.replace("/control/user"),600);}}}}
}}
async function poll(){{if(done)return;try{{
  const r=await fetch('/control/user/state',{{cache:'no-store'}});if(!r.ok)throw Error();
  show(await r.json());}}catch(e){{status.textContent='Local status unavailable. Check the terminal.';}}
  if(!done)setTimeout(poll,500);}}
form.addEventListener('submit',async e=>{{e.preventDefault();
  const input=document.querySelector('#secret');let password=input.value;input.value='';
  try{{const response=await fetch('/control/user/password',{{method:'POST',
    headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},
    body:JSON.stringify({{password}})}});
    if(!response.ok)status.textContent='Password was not accepted. Check the login state.';
  }}catch(e){{status.textContent='Could not submit the password.';}}finally{{password='';input.value='';}});
cancel.addEventListener('click',()=>fetch('/control/user/cancel',{{method:'POST',
  headers:{{'X-CSRF-Token':csrf}}}}));
poll();
</script></main></body></html>"""
