"""Owner-only Bot API onboarding inside `control`: token -> identity -> OS credential -> profile.

The token lives only in this process between Review and Apply, is never rendered,
logged or returned by an error, and only a verified identity may write config.
"""

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .config import Profile, Settings, validate_name
from .control import _esc, read_config, revision, transition, write_config
from .control_user import profile_mismatches
from .models import TeleloomError
from .secrets import Secrets

TOKEN = re.compile(r"\d{1,20}:[A-Za-z0-9_-]{20,}")
BOT_LIMIT = (
    "Bot API has no Telegram history, global search or channel comments: only updates the "
    "owner already saved are readable, and polling covers new updates only from the moment "
    "it starts. There is no automatic fallback to a bot MTProto session."
)
PENDING = "The review expired or was cancelled. Enter the token again; nothing was saved."


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sanitised(raw: Any, token: str) -> dict[str, str]:
    """Bot API metadata is untrusted: keep three exact fields, print-capable and bounded."""
    data = raw if isinstance(raw, dict) else {}

    def text(key: str, limit: int, pattern: str | None = None) -> str:
        value = "".join(
            ch for ch in str(data.get(key) or "").replace(token, "") if ch.isprintable()
        )
        return re.sub(pattern, "", value)[:limit] if pattern else value[:limit]

    return {
        "id": text("id", 20, r"\D"),
        "username": text("username", 32, r"[^A-Za-z0-9_]"),
        "name": text("name", 64),
    }


async def _telegram_identity(token: str, polling: bool, proxy: str | None) -> dict[str, Any]:
    """The only real Bot API call; every failure becomes a fixed message without the token."""
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession

    from .auth import identity

    session = AiohttpSession(proxy=proxy)
    bot = Bot(token, session=session)
    try:
        me = await bot.get_me()
        if polling and (await bot.get_webhook_info()).url:
            raise TeleloomError(
                "webhook_conflict",
                "Telegram still delivers this bot's updates to a webhook. Remove it in the "
                "place that set it, then retry; this tool never changes a webhook.",
            )
        if not me.is_bot or str(me.id) != token.split(":", 1)[0]:
            raise TeleloomError("auth_failed", "Telegram did not confirm a bot identity.")
        return identity(me)
    except TeleloomError:
        raise
    except Exception:
        raise TeleloomError(
            "auth_failed", "Telegram did not confirm this Bot API token. Nothing was saved."
        ) from None
    finally:
        await session.close()


@dataclass
class _Review:
    name: str
    token: str  # process memory only, cleared on apply, cancel or expiry
    identity: dict[str, str]
    polling: bool
    replace: bool  # the name already exists: save needs an explicit confirmation
    existing: Profile | None
    preserves: bool  # the same bot identity: keep its generation and grants
    revision: str
    session: str
    expires_at: float


class BotOnboarding:
    """Routes for `control`; mount with `control.app.routes.extend(bot.routes())`."""

    def __init__(
        self,
        control: Any,
        *,
        secrets: Any = None,
        inspect: Callable[[str, bool, str], Awaitable[dict[str, Any]]] | None = None,
        commit: Callable[[str, str, dict[str, str], bool, bool], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = 600,
    ) -> None:
        self.control = control
        self.settings: Settings = control.settings
        self.secrets = secrets or Secrets()
        self.inspect = inspect or self._inspect
        self.commit = commit or self._write
        self.clock = clock
        self.ttl = ttl
        self.reviews: dict[str, _Review] = {}

    def routes(self) -> list[Route]:
        return [
            Route("/control/bot", self.page),
            Route("/control/bot/review", self.review, methods=["POST"]),
            Route("/control/bot/apply", self.apply, methods=["POST"]),
            Route("/control/bot/cancel", self.cancel, methods=["POST"]),
        ]

    async def _inspect(self, token: str, polling: bool, name: str) -> dict[str, Any]:
        from .adapters import proxy_url

        settings = Settings.load(self.settings.data_dir)
        existing = settings.profiles.get(name)
        route = existing or Profile(kind="bot", bot_backend="bot_api")
        return await _telegram_identity(
            token, polling, proxy_url(name, route, self.secrets, settings)
        )

    def _check(self, request: Request, *, mutation: bool = True) -> tuple[Any, Response | None]:
        if not self.control._allowed(request, mutation=mutation):
            return None, JSONResponse({"error": "forbidden_origin"}, status_code=403)
        found = self.control._session(request)
        if found is None:
            return None, self.control._locked(request)
        return found, None

    async def _post(self, request: Request) -> tuple[Any, dict[str, str], Response | None]:
        found, denied = self._check(request)
        if denied is not None:
            return None, {}, denied
        body = await self.control._form(request)
        if isinstance(body, Response):
            return None, {}, body
        form = {key: values[0] for key, values in body.items()}
        if not hmac.compare_digest(form.get("csrf", "").encode(), found[1].csrf.encode()):
            return None, {}, JSONResponse({"error": "invalid_csrf"}, status_code=403)
        return found, form, None

    def _pending(self, handle: str, session: str) -> tuple[str, _Review] | None:
        now = self.clock()
        self.reviews = {key: value for key, value in self.reviews.items() if value.expires_at > now}
        key = _digest(handle)
        value = self.reviews.get(key)
        return (key, value) if value is not None and value.session == session else None

    async def page(self, request: Request) -> Response:
        found, denied = self._check(request, mutation=False)
        if denied is not None:
            return denied
        return self._page(found[1].csrf)

    async def review(self, request: Request) -> Response:
        found, form, denied = await self._post(request)
        if denied is not None:
            return denied
        name = str(form.get("profile", "")).strip()
        token = str(form.get("token", ""))
        polling = form.get("polling") == "1"
        try:
            validate_name(name)
        except ValueError:
            return self._page(
                found[1].csrf,
                "Profile names start with a lowercase letter and use a-z, 0-9, _.",
                422,
            )
        if len(token) > 200 or not TOKEN.fullmatch(token):
            return self._page(found[1].csrf, "That is not a Telegram Bot API token.", 422)
        raw, _ = read_config(self.settings)
        existing = Settings.load(self.settings.data_dir).profiles.get(name)
        try:
            info = _sanitised(await asyncio.wait_for(self.inspect(token, polling, name), 20), token)
        except TeleloomError as error:
            text = {
                "webhook_conflict": "webhook_conflict: Telegram still delivers this bot's updates to a webhook. Polling was not enabled.",
                "auth_failed": "Telegram did not confirm this Bot API token.",
            }.get(error.code, "Telegram could not verify this token.")
            return self._page(found[1].csrf, text + " Nothing was saved.", 422)
        except Exception:
            return self._page(
                found[1].csrf,
                "Telegram could not verify this token. Nothing was saved.",
                422,
            )
        if not info["id"]:
            return self._page(
                found[1].csrf, "Telegram returned no bot identity. Nothing was saved.", 422
            )
        while len(self.reviews) >= 8:
            del self.reviews[next(iter(self.reviews))]
        key = secrets.token_urlsafe(24)
        value = _Review(
            name=name,
            token=token,
            identity=info,
            polling=polling,
            replace=existing is not None,
            existing=existing,
            revision=revision(raw),
            session=found[0],
            preserves=existing is not None
            and existing.kind == "bot"
            and existing.identity.get("id") == info["id"],
            expires_at=self.clock() + self.ttl,
        )
        digest = _digest(key)
        self.reviews[digest] = value
        asyncio.get_running_loop().call_later(self.ttl, self.reviews.pop, digest, None)
        return self._review_page(found[1].csrf, key, value)

    async def apply(self, request: Request) -> Response:
        found, form, denied = await self._post(request)
        if denied is not None:
            return denied
        handle = form.get("review", "")
        pending = self._pending(handle, found[0])
        if pending is None:
            return self._page(found[1].csrf, PENDING, 410)
        key, value = pending
        if value.replace and form.get("confirm") != "1":
            return self._review_page(
                found[1].csrf,
                handle,
                value,
                f"This replaces profile '{value.name}'. Confirm the replacement explicitly.",
                status=422,
            )
        self.reviews.pop(key, None)
        raw, _ = read_config(self.settings)
        if revision(raw) != value.revision:
            return self._page(
                found[1].csrf, "Configuration changed. Review the profile again.", 409
            )
        note: list[str] = []
        polling_connected = False

        async def body() -> None:
            raw, _ = read_config(self.settings)
            if revision(raw) != value.revision:
                raise TeleloomError(
                    "stale_revision", "Configuration changed. Review the profile again."
                )
            note.append(
                self.commit(value.name, value.token, value.identity, value.polling, value.replace)
                or ""
            )

        def verify(observed: dict[str, Any]) -> list[str]:
            nonlocal polling_connected
            profile = Settings.load(self.settings.data_dir).profiles[value.name]
            errors = profile_mismatches(observed, value.name, profile)
            if value.polling and not errors:
                rows = observed["scope"]["profiles"]
                polling_connected = any(
                    item["id"] == value.name and item.get("state") == "connected" for item in rows
                )
            return errors

        record, error = await transition(
            self.control.lifecycle,
            self.settings,
            {"operation": "bot_login", "profile": value.name},
            body,
            verify=verify,
        )
        value.token = ""
        if error is not None:
            status = {
                "owner_busy": 409,
                "transition_in_progress": 409,
                "transition_blocked": 409,
                "profile_exists": 409,
                "stale_revision": 409,
                "verify_failed": 503,
                "credentials_unavailable": 503,
            }.get(error.code, 400)
            return self._page(found[1].csrf, f"{error.code}: {error.message}", status)
        owner = (record or {}).get("owner") or {}
        return self._page(
            found[1].csrf,
            "".join(note)
            + f" Saved '{value.name}' as {value.identity.get('username') or value.identity['id']}, "
            + (
                "existing read policy and grants preserved. "
                if value.preserves
                else "new generation, read policy selected with no chats and no grants. "
            )
            + (
                f"Owner {owner['owner_id']} answers in this workspace."
                if owner
                else "The owner is not running; run teleloom serve to load this profile."
            )
            + (
                " Polling is configured. Startup is still unverified; open teleloom ui to check profile status."
                if value.polling and not polling_connected
                else ""
            ),
            200,
            done=True,
        )

    async def cancel(self, request: Request) -> Response:
        found, form, denied = await self._post(request)
        if denied is not None:
            return denied
        pending = self._pending(form.get("review", ""), found[0])
        if pending is None:
            return self._page(found[1].csrf, PENDING, 410)
        self.reviews.pop(pending[0])
        return self._page(found[1].csrf, "Cancelled. No profile or credential was changed.", 200)

    def _write(
        self, name: str, token: str, info: dict[str, str], polling: bool, replace: bool
    ) -> None:
        """Runs under control.lock and owner.lock (from `transition`): never a second owner."""
        settings = Settings.load(self.settings.data_dir)
        _, data = read_config(self.settings)
        existing = settings.profiles.get(name)
        if existing is not None and not replace:
            raise TeleloomError(
                "profile_exists",
                f"A profile named '{name}' appeared while this review was open. Nothing was saved.",
            )
        if (
            existing is not None
            and existing.kind == "bot"
            and existing.identity.get("id") == info["id"]
        ):
            # Same bot account: its generation, read policy and grants stay valid.
            profile = existing.model_copy(
                update={
                    "bot_backend": "bot_api",
                    "api_id": None,
                    "identity": info,
                    "polling": polling,
                }
            )
        else:
            # A different bot: a new generation, no inherited read policy or grants.
            profile = Profile(
                kind="bot",
                bot_backend="bot_api",
                identity=info,
                polling=polling,
                read_mode="selected",
                proxy=existing.proxy if existing else None,
            )
        previous = self.secrets.get(name, "bot_token")
        self.secrets.set(name, "bot_token", token)
        try:
            profiles = data.setdefault("profiles", {})
            if existing is not None and profile.generation == existing.generation:
                profiles[name].update(
                    bot_backend="bot_api", api_id=None, identity=info, polling=polling
                )
            else:
                profiles[name] = profile.model_dump(mode="json")
            write_config(settings, data)
            settings.profiles[name] = profile
        except Exception as error:
            try:
                if previous:
                    self.secrets.set(name, "bot_token", previous)
                else:
                    self.secrets.delete(name, "bot_token")
            except TeleloomError:
                raise TeleloomError(
                    "rollback_failed",
                    "The configuration is unchanged, but its OS credential could not be restored.",
                ) from None
            raise TeleloomError(
                "save_failed",
                f"config.json could not be saved ({type(error).__name__}); the profile was not changed.",
            ) from None
        self.settings.profiles = settings.profiles

    def _review_page(
        self,
        csrf: str,
        key: str,
        value: _Review,
        message: str | None = None,
        status: int = 200,
    ) -> Response:
        policy, grants_by_name = self._policy(value)
        grants = [
            f"{grant}: {', '.join(chats)}"
            for grant, chats in grants_by_name.items()
            if grant != "read" and chats
        ]
        confirm = (
            f'<p><label><input type="checkbox" name="confirm" value="1"> '
            f"Replace profile <code>{_esc(value.name)}</code> and its stored token. "
            + (
                "Same bot identity: its generation and existing grants are preserved."
                if value.preserves
                else "Different bot identity: a new generation, and its grants are reset."
            )
            + "</label></p>"
            if value.replace
            else ""
        )
        body = f"""<h1>Review Bot API profile</h1>
{'<p role="alert">' + _esc(message) + "</p>" if message else ""}
<dl class="meta">
<dt>Profile</dt><dd><code>{_esc(value.name)}</code></dd>
<dt>Backend</dt><dd>Bot API (explicit; no MTProto bot)</dd>
<dt>Bot identity</dt><dd><code>{_esc(value.identity["id"])}</code>
 {_esc(value.identity["username"])} · {_esc(value.identity["name"])}</dd>
<dt>Read policy</dt><dd>{_esc(policy)}</dd>
<dt>Active grants</dt><dd>{_esc(", ".join(grants) or "none")}</dd>
<dt>Polling</dt><dd>{"on, live updates only" if value.polling else "off"}</dd>
<dt>Token</dt><dd>held in this control process only until Apply or Cancel; never shown again</dd>
</dl>
<p class="small">{_esc(BOT_LIMIT)}</p>
<form method="post" action="/control/bot/apply">
{confirm}
<input type="hidden" name="csrf" value="{_esc(csrf)}">
<input type="hidden" name="review" value="{_esc(key)}">
<p><button type="submit">Save profile</button></p></form>
<form method="post" action="/control/bot/cancel">
<input type="hidden" name="csrf" value="{_esc(csrf)}">
<input type="hidden" name="review" value="{_esc(key)}">
<p><button type="submit">Cancel</button></p></form>"""
        return HTMLResponse(self._shell(body), status_code=status, headers=self.control._headers())

    def _policy(self, value: _Review) -> tuple[str, dict[str, Any]]:
        """What the saved profile will actually allow: the old grants only survive an unchanged bot."""
        existing = value.existing
        if existing is None or not value.preserves:
            return "selected (no chats)", {}
        read = ", ".join(existing.read_chats) or "no chats"
        policy = "all chats" if existing.read_mode == "all" else f"selected ({read})"
        return policy, existing.grants()

    def _page(
        self, csrf: str, message: str | None = None, status: int = 200, *, done: bool = False
    ) -> Response:
        profiles = sorted(
            name for name, value in Settings.load(self.settings.data_dir).profiles.items()
        )
        body = f"""<h1>Connect a Bot API profile</h1>
{'<p role="status">' + _esc(message) + "</p>" if message else ""}
{
            ""
            if done
            else f'''<form method="post" action="/control/bot/review">
<p><label>Profile name <input name="profile" required pattern="[a-z][a-z0-9_]{{0,47}}"
 autocomplete="off" spellcheck="false"></label></p>
<p><label>Bot token <input type="password" name="token" required maxlength="200"
 autocomplete="off" spellcheck="false"></label></p>
<p><label><input type="checkbox" name="polling" value="1"> Enable polling for live updates
 (leave off to read only updates Telegram already delivered)</label></p>
<input type="hidden" name="csrf" value="{_esc(csrf)}">
<p><button type="submit">Verify with Telegram and review</button></p></form>
<p class="small">{_esc(BOT_LIMIT)}</p>'''
        }
<p class="small">Existing profiles: {_esc(", ".join(profiles) or "none")}. A new profile starts with
read policy selected and an empty chat list; the token alone grants no read, send or AI access.
A token is stored only in the OS credential backend, never in config.json.</p>
<p class="small"><a href="/control">Owner management</a></p>"""
        return HTMLResponse(self._shell(body), status_code=status, headers=self.control._headers())

    @staticmethod
    def _shell(body: str) -> str:
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot API profile · teleloom</title><style>
:root{{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:6vh 24px}}main{{max-width:720px;margin:auto}}
h1{{font-size:30px;letter-spacing:-.03em}}p,li,dt,dd{{line-height:1.6}}
code{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;border-radius:4px}}
.small{{font-size:13px;color:#68665d}}.meta{{display:grid;grid-template-columns:160px 1fr;gap:8px 20px}}
button{{font:inherit;padding:10px 16px;border-radius:6px;border:1px solid #b8b4a8;background:white;cursor:pointer}}
input{{font:inherit;padding:6px 8px}}button:focus-visible,input:focus-visible{{outline:3px solid #24231f;outline-offset:2px}}
</style></head><body><main><p class="small">teleloom / owner management · Bot API</p>
{body}</main></body></html>"""
