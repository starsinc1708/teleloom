"""One plain-text send from the owner-only control process.

Compose does no Telegram work. Preview asks the owner's own MCP surface for one
``delivery_preview`` of the exact numeric chat and text typed in this form, which
is the direct human instruction ADR 0002 already accepts; Review reads that
immutable plan back from the owner's SQLite (id, hash, deadline, resolved target)
so a lost response or a restarted control reopens the same plan; Confirm is a
separate CSRF-protected step that calls the existing ``delivery_execute`` with the
reviewed id and hash. The owner's own ``jobs_status`` renders the durable receipt.

Control never hands a credential or an authorization to the browser: the MCP
bearer is read from the OS store for each call (as ``Lifecycle.observed`` does)
and no header, cookie, query flag or MCP tool supplies the human step.
"""

import hmac
import html
from datetime import datetime
from typing import Any
from urllib.parse import quote

from mcp.shared.exceptions import McpError
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .browser_cache import protected_page
from .config import Settings
from .control_policy import chat_id as exact_chat
from .models import TeleloomError, utcnow
from .store import Store

MAX_BODY = 64 * 1024
TOOLS = ("delivery_preview", "delivery_execute", "jobs_status")

_HTTP = {
    "profile_not_found": 404,
    "plan_not_found": 404,
    "job_not_found": 404,
    "chat_not_found": 404,
    "plan_changed": 409,
    "plan_expired": 409,
    "recipient_changed": 409,
    "read_not_allowed": 403,
    "account_changed": 403,
    "recipient_not_allowed": 403,
    "invalid_chat_id": 422,
    "invalid_text": 422,
    "owner_unavailable": 503,
    "send_unavailable": 503,
}


class Owner:
    """The owner's MCP surface; the bearer stays inside each call, never in a response."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from .daemon import session

        try:
            async with session(self.settings, auto_start=False) as client:
                result = await client.call_tool(tool, arguments)
        except TeleloomError:
            raise
        except McpError:
            raise TeleloomError(
                "send_unavailable",
                f"This owner does not expose {tool}. Widen the tool exposure and retry; "
                "nothing was sent.",
            ) from None
        except Exception:
            # MCP transport teardown may wrap connection failures in an ExceptionGroup.
            raise TeleloomError(
                "owner_unavailable",
                (
                    "The owner did not answer the confirmation. Reopen this plan to check its "
                    "durable state before creating another preview."
                    if tool == "delivery_execute"
                    else "The owner did not answer. Check that teleloom serve is running and reload."
                ),
            ) from None
        if result.isError or not result.structuredContent:
            # A refusal is the owner's own code and message, never this page's invention.
            info = (result.structuredContent or {}).get("error") or {}
            raise TeleloomError(
                str(info.get("code") or "send_refused"),
                str(info.get("message") or "The owner refused this step; nothing was sent."),
            )
        return dict(result.structuredContent["data"])

    async def preview(self, profile: str, chat: str, text: str) -> dict[str, Any]:
        return await self._call(
            "delivery_preview",
            {
                "profile_id": profile,
                "recipients": [chat],
                "text": text,
                "owner_authorized": True,
            },
        )

    async def execute(self, profile: str, plan_id: str, plan_hash: str) -> dict[str, Any]:
        return await self._call(
            "delivery_execute",
            {
                "profile_id": profile,
                "plan_id": plan_id,
                "plan_hash": plan_hash,
                "confirmed": True,
            },
        )

    async def job(self, profile: str, job_id: str) -> dict[str, Any]:
        return await self._call("jobs_status", {"profile_id": profile, "job_id": job_id})


def _owner(instance: Any) -> Any:
    return instance.owner or Owner(instance.settings)


def plan(settings: Any, profile: str, plan_id: str) -> dict[str, Any]:
    """The immutable preview exactly as the owner stored it, or nothing."""
    if not (settings.data_dir / "workspace.sqlite").is_file():
        raise TeleloomError("plan_not_found", "No preview exists. Start a new one.")
    store = Store(settings.data_dir)
    try:
        found = store.get("plans", plan_id)
    finally:
        store.close()
    if found is None or found.get("profile_id") != profile:
        raise TeleloomError(
            "plan_not_found", "No reviewed preview has this identity. Start a new preview."
        )
    payload = found["payload"]
    recipients = payload.get("recipients") or []
    if (
        not payload.get("owner_authorized")
        or payload.get("format") != "plain_text"
        or payload.get("broadcast")
        or payload.get("reply_to_message_id") is not None
        or "operation" in payload
        or len(recipients) != 1
    ):
        raise TeleloomError(
            "plan_not_found", "This preview is outside the one-message send workflow."
        )
    return found


def _missing(instance: Any) -> list[str]:
    """Tools this owner does not currently register; nothing is inferred from a request."""
    from .control import read_config

    _, data = read_config(instance.settings)
    mode = data.get("exposure_mode", "all")
    if mode == "selected":
        exposed = set(data.get("exposed_tools") or [])
    elif mode == "read-only":
        exposed = {str(item["name"]) for item in instance.catalogue if not item.get("writes")}
    else:
        exposed = set(instance.names)
    return [name for name in TOOLS if name not in exposed]


def _require_tools(instance: Any) -> None:
    if missing := _missing(instance):
        raise TeleloomError(
            "send_unavailable", "This owner does not register " + ", ".join(missing) + "."
        )


def _guard(instance: Any, request: Request, *, mutation: bool) -> Any:
    if not instance._allowed(request, mutation=mutation):
        return JSONResponse({"error": "forbidden_origin"}, status_code=403)
    found = instance._session(request)
    if found is None:
        return instance._locked(request)
    return found


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _layout(instance: Any, title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · teleloom</title><style>
:root{{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:6vh 24px}}main{{max-width:820px;margin:auto}}
h1{{font-size:32px;letter-spacing:-.03em}}h2{{font-size:18px;margin-top:2em}}
p,li{{line-height:1.6}}code,pre{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;border-radius:4px}}
pre{{white-space:pre-wrap;padding:.8em 1em}}dl{{display:grid;grid-template-columns:160px 1fr;gap:10px 20px}}
dt{{font-weight:600}}dd{{margin:0;overflow-wrap:anywhere}}.small{{font-size:13px;color:#68665d}}
label{{display:flex;flex-direction:column;gap:.3em;font-size:13px;color:#68665d;margin:1em 0}}
input,select,textarea,button{{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid #b8b4a8;background:white}}
textarea{{min-height:9em}}button{{cursor:pointer}}button:disabled{{cursor:not-allowed;opacity:.55}}
.state{{border-left:4px solid #68665d;padding:.8em 1em;background:#ece8df}}
.state[data-state=error]{{border-color:#9c2828}}.state[data-state=unknown]{{border-color:#9c6b11}}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{{outline:3px solid #24231f;outline-offset:2px}}
</style></head><body><main><p class="small">teleloom / owner management · this process is not the owner</p>
<p class="small"><a href="/control">Tool exposure</a> · <a href="/control/policy">Profile read policy and grants</a></p>
<h1>{_esc(title)}</h1>{body}</main></body></html>"""


def _page(instance: Any, title: str, body: str, status: int = 200) -> Response:
    return protected_page(_layout(instance, title, body), instance._headers, status=status)


def _refused(instance: Any, exc: TeleloomError) -> Response:
    body = (
        f'<p class="state" data-state="error" role="alert">{_esc(exc.message)}</p>'
        f"<p><code>{_esc(exc.code)}</code></p>"
        '<p><a href="/control/send">Start a new preview</a></p>'
    )
    return _page(instance, "Send refused", body, _HTTP.get(exc.code, 400))


def _compose(
    instance: Any,
    csrf: str,
    *,
    profile: str = "",
    chat_id: str = "",
    text: str = "",
    message: str | None = None,
    status: int = 200,
) -> Response:
    missing = _missing(instance)
    names = sorted(instance.settings.profiles)
    options = "".join(
        f'<option value="{_esc(name)}"{" selected" if name == profile else ""}>{_esc(name)}</option>'
        for name in names
    )
    notice = (
        f'<p class="state" data-state="error" role="alert">{_esc(message)}</p>' if message else ""
    )
    blocked = (
        '<p class="state" data-state="unknown" role="alert" id="send-unavailable">This owner does '
        f"not register {_esc(', '.join(missing))}. Widen the tool exposure on "
        '<a href="/control">Tool exposure</a> and retry; nothing is sent from this page.</p>'
        if missing
        else '<p class="state" role="status">Nothing is sent by this page. Preview reads the '
        "recipient from Telegram, freezes the exact text, plan ID, hash and deadline, and "
        "creates no job.</p>"
    )
    form = (
        f'<form method="post" action="/control/send/preview">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        f'<label>Profile<select name="profile" required>{options}</select></label>'
        f'<label>Recipient chat ID<input name="chat_id" required inputmode="numeric" '
        f'value="{_esc(chat_id)}" placeholder="-1001234567890"></label>'
        f'<label>Message text<textarea name="text" required maxlength="4096">{_esc(text)}</textarea></label>'
        f'<p><button type="submit"{" disabled" if missing else ""}>Preview this exact send</button></p>'
        "</form>"
        if names
        else '<p class="state" data-state="empty" role="status">No profiles are configured. Run '
        "teleloom auth user or auth bot first.</p>"
    )
    return _page(instance, "Send one message", notice + blocked + form, status)


def _sender(payload: dict[str, Any], profile: str) -> str:
    account = payload.get("account") or {}
    identity = account.get("identity") or {}
    parts = [
        _esc(account.get("kind") or "unknown"),
        *(_esc(identity[key]) for key in ("id", "username", "name") if identity.get(key)),
    ]
    return f"{_esc(profile)} · " + " · ".join(parts)


def _review(instance: Any, csrf: str, profile: str, plan: dict[str, Any]) -> Response:
    payload = plan["payload"]
    resolved = plan["resolved"][0] if plan.get("resolved") else {}
    target = f"<code>{_esc(resolved.get('id') or payload['recipients'][0])}</code>"
    if resolved.get("title"):
        target += " · " + _esc(resolved["title"])
    rows = [
        ("Profile", _esc(profile)),
        ("Sender", _sender(payload, profile)),
        ("Recipient", target),
        ("Format", "plain text"),
        ("Message", f"<pre>{_esc(payload['text'])}</pre>"),
        ("Plan ID", f"<code>{_esc(plan['id'])}</code>"),
        ("Plan hash", f"<code>{_esc(plan['hash'])}</code>"),
        (
            "Confirm before",
            f'<time datetime="{_esc(plan["expires_at"])}">{_esc(plan["expires_at"])}</time>',
        ),
    ]
    body = "<dl>" + "".join(f"<dt>{key}</dt><dd>{value}</dd>" for key, value in rows) + "</dl>"
    body += (
        "<h2>Side effects</h2><p>Adds one new message to this single chat. It does not edit, "
        "delete, forward, reply, schedule, broadcast or acknowledge anything, and changes no "
        "grant.</p>"
    )
    if plan.get("job_id"):
        body += (
            '<p class="state" role="status"><strong>Already confirmed.</strong> This plan created '
            "one durable job; confirming again reopens that job and never sends twice.</p>"
            f'<p><a href="/control/send/job/{_esc(plan["job_id"])}?profile={quote(profile)}">'
            "Inspect job and receipts</a></p>"
        )
        return _page(instance, "Review this send", body)
    if utcnow() >= datetime.fromisoformat(plan["expires_at"]):
        body += (
            '<p class="state" data-state="error" role="alert">This plan expired. Nothing was sent; '
            "create a new preview to review the current target and text.</p>"
        )
        return _page(instance, "Review this send", body, 409)
    body += f"""<h2>Confirm</h2>
<form method="post" action="/control/send/confirm">
<input type="hidden" name="csrf" value="{_esc(csrf)}">
<input type="hidden" name="profile" value="{_esc(profile)}">
<input type="hidden" name="plan_id" value="{_esc(plan["id"])}">
<input type="hidden" name="plan_hash" value="{_esc(plan["hash"])}">
<p><button type="submit">Confirm and send this exact message</button></p></form>"""
    return _page(instance, "Review this send", body)


def page(instance: Any, request: Request) -> Response:
    found = _guard(instance, request, mutation=False)
    if isinstance(found, Response):
        return found
    return _compose(instance, found[1].csrf)


async def preview(instance: Any, request: Request) -> Response:
    found = _guard(instance, request, mutation=True)
    if isinstance(found, Response):
        return found
    form = await instance._form(request, limit=MAX_BODY)
    if isinstance(form, Response):
        return form
    csrf = found[1].csrf
    if not hmac.compare_digest((form.get("csrf") or [""])[0].encode(), csrf.encode()):
        return JSONResponse({"error": "invalid_csrf"}, status_code=403)
    profile = (form.get("profile") or [""])[0]
    typed = (form.get("chat_id") or [""])[0]
    text = (form.get("text") or [""])[0]
    try:
        _require_tools(instance)
        instance.settings.profile(profile)
        chat = exact_chat(typed)
        plan_id = (await _owner(instance).preview(profile, chat, text))["plan_id"]
    except TeleloomError as exc:
        # The typed text stays only in this response so the owner can fix one field.
        return _compose(
            instance,
            csrf,
            profile=profile,
            chat_id=typed,
            text=text,
            message=f"{exc.message} ({exc.code})",
            status=_HTTP.get(exc.code, 400),
        )
    return RedirectResponse(
        f"/control/send/plan/{quote(str(plan_id))}?profile={quote(profile)}",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


def plan_page(instance: Any, request: Request) -> Response:
    found = _guard(instance, request, mutation=False)
    if isinstance(found, Response):
        return found
    profile = request.query_params.get("profile") or ""
    try:
        _require_tools(instance)
        reviewed = plan(instance.settings, profile, request.path_params["plan_id"])
        config = Settings.load(instance.settings.data_dir).profile(profile)
        config.require_read(reviewed["payload"]["recipients"][0])
        account = reviewed["payload"]["account"]
        if account.get("generation") != config.generation:
            raise TeleloomError(
                "account_changed",
                "This preview belongs to a previous account. Create a fresh preview.",
            )
    except TeleloomError as exc:
        return _refused(instance, exc)
    return _review(instance, found[1].csrf, profile, reviewed)


async def confirm(instance: Any, request: Request) -> Response:
    found = _guard(instance, request, mutation=True)
    if isinstance(found, Response):
        return found
    form = await instance._form(request, limit=MAX_BODY)
    if isinstance(form, Response):
        return form
    if not hmac.compare_digest((form.get("csrf") or [""])[0].encode(), found[1].csrf.encode()):
        return JSONResponse({"error": "invalid_csrf"}, status_code=403)
    profile = (form.get("profile") or [""])[0]
    plan_id = (form.get("plan_id") or [""])[0]
    submitted = (form.get("plan_hash") or [""])[0]
    async with instance.send_confirming:
        try:
            reviewed = plan(instance.settings, profile, plan_id)
            if not hmac.compare_digest(submitted.encode(), str(reviewed["hash"]).encode()):
                raise TeleloomError(
                    "plan_changed",
                    "This confirmation does not match the reviewed preview. Nothing was sent by this action.",
                )
            job_id = reviewed.get("job_id")
            if not job_id:
                _require_tools(instance)
        except TeleloomError as exc:
            return _refused(instance, exc)
        if not job_id:
            try:
                result = await _owner(instance).execute(profile, plan_id, reviewed["hash"])
                job_id = result["job_id"]
            except TeleloomError as exc:
                # The answer may have been lost after the owner committed the job.
                job_id = plan(instance.settings, profile, plan_id).get("job_id")
                if not job_id:
                    return _refused(instance, exc)
    return RedirectResponse(
        f"/control/send/job/{quote(str(job_id))}?profile={quote(profile)}",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


def _receipt(instance: Any, profile: str, job: dict[str, Any]) -> Response:
    rows = [
        ("Job ID", f"<code>{_esc(job['id'])}</code>"),
        ("Profile", _esc(profile)),
        ("State", f"<strong>{_esc(job.get('status'))}</strong>"),
        ("Created", _esc(job.get("created_at") or "—")),
    ]
    body = (
        '<p class="state" role="status">This is the owner\'s durable record of the one job this '
        "plan created. Reloading it reads the same job; nothing is resent.</p>"
        + "<dl>"
        + "".join(f"<dt>{key}</dt><dd>{value}</dd>" for key, value in rows)
        + "</dl>"
    )
    error = job.get("error") or {}
    if error.get("code"):
        body += (
            '<p class="state" data-state="error" role="alert">'
            f"<code>{_esc(error['code'])}</code> {_esc(error.get('message') or '')}</p>"
        )
    statuses = [str(item.get("status")) for item in job.get("deliveries") or []]
    if job.get("status") == "needs_review" or any(
        state in {"unknown", "sending"} for state in statuses
    ):
        body += (
            '<p class="state" data-state="unknown" role="alert"><strong>Outcome unknown.</strong> '
            "Telegram may have received this message. Reconcile it in Telegram before creating "
            "another preview; this page never resends or replays it.</p>"
        )
    if job.get("deliveries"):
        cells = "".join(
            f"<tr><td>{position + 1}</td><td>{_esc(item.get('chat_id') or 'target')}</td>"
            f"<td>{_esc(item.get('status'))}</td><td>{_esc(item.get('message_id') or '—')}</td>"
            f"<td>{_esc(item.get('attempted_at') or '—')}</td></tr>"
            for position, item in enumerate(job["deliveries"])
        )
        body += (
            "<h2>Receipts</h2><table><thead><tr><th>#</th><th>Target</th><th>State</th>"
            f"<th>Message</th><th>Attempted</th></tr></thead><tbody>{cells}</tbody></table>"
        )
    panel = (
        f"http://127.0.0.1:{instance.settings.port}/ui/jobs/{quote(str(job['id']))}"
        f"?profile={quote(profile)}"
    )
    body += (
        f'<p class="small">Full evidence, including every receipt field, is in the read-only '
        f'panel: <a href="{_esc(panel)}">{_esc(panel)}</a> (run <code>teleloom ui</code> for its '
        "own session).</p>"
        '<p><a href="/control/send">Send another message</a></p>'
    )
    return _page(instance, "Job and receipt", body)


async def job_page(instance: Any, request: Request) -> Response:
    found = _guard(instance, request, mutation=False)
    if isinstance(found, Response):
        return found
    profile = request.query_params.get("profile") or ""
    try:
        instance.settings.profile(profile)
        job = await _owner(instance).job(profile, request.path_params["job_id"])
    except TeleloomError as exc:
        return _refused(instance, exc)
    return _receipt(instance, profile, job)
