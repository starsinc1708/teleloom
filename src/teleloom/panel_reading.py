"""Bounded reading workflow: explicit preview, owner start, frozen evidence pages.

One existing flow: ``runtime.selection`` -> ``jobs.evidence_start`` ->
``jobs.results``. A GET never starts a job or reads Telegram; paging re-reads the
frozen owner snapshot and re-checks current exposure, read policy, account
generation and snapshot expiry before any text is disclosed.
"""

import asyncio
import hmac
import html
from datetime import datetime
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .models import TeleloomError, iso
from .panel_jobs import _HTTP, _layout, _notice
from .rich_reads import scoped_evidence
from .runtime import number

PAGE = 10
BUDGETS = {"max_messages": 10000, "max_characters": 1000000, "max_requests": 1000}
DEFAULTS = {"max_messages": "1000", "max_characters": "100000", "max_requests": "200"}

_STATES = {
    "queued": "Queued",
    "running": "Running",
    "paused": "Paused",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "needs_review": "Needs review",
}

_ERRORS = {
    "profile_not_found": "That profile is not configured for this owner.",
    "invalid_selection": "Provide 1–200 unique numeric chat IDs.",
    "invalid_range": "Provide a start and a later exclusive end, both with a timezone.",
    "invalid_time": "Dates must include a timezone.",
    "invalid_query": "A query must contain 1–1000 characters.",
    "invalid_id": "Provide canonical numeric chat IDs.",
    "read_timeout": "The owner timed out while resolving the selected chats.",
    "invalid_budget": "Each budget must be a whole number within its stated limit.",
    "read_not_allowed": "A selected chat is outside the current read policy.",
    "tool_not_exposed": "Reading is disabled by the owner's exposure policy.",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _exposure(settings: Any, *tools: str) -> None:
    if settings.exposure_mode == "selected" and any(
        tool not in settings.exposed_tools for tool in tools
    ):
        raise TeleloomError(
            "tool_not_exposed", "Reading is disabled by the owner's tool exposure policy."
        )


def _time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        raise TeleloomError("invalid_time", "Dates must include a timezone.")
    return parsed


def _form(values: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        key: str(values.get(key) or "").strip()
        for key in ("profile", "chats", "since", "until", "query")
    }
    raw.update(
        {key: str(values.get(key) or "").strip() or default for key, default in DEFAULTS.items()}
    )
    raw["submitted"] = bool(raw["profile"] or raw["chats"])
    return raw


def _params(values: dict[str, Any]) -> dict[str, Any]:
    chats = [part for part in values["chats"].replace(",", " ").split() if part]
    if not 1 <= len(chats) <= 200 or len(set(chats)) != len(chats):
        raise TeleloomError("invalid_selection", "Provide 1–200 unique numeric chat IDs.")
    if not values["profile"]:
        raise TeleloomError("profile_not_found", "Choose a configured profile.")
    since, until = _time(values["since"]), _time(values["until"])
    if since is None or until is None or since >= until:
        raise TeleloomError(
            "invalid_range", "Provide a start and a later exclusive end with timezones."
        )
    if len(values["query"]) > 1000:
        raise TeleloomError("invalid_query", "A query must contain 1–1000 characters.")
    budgets: dict[str, int] = {}
    for key, ceiling in BUDGETS.items():
        if (
            not values[key].isascii()
            or not values[key].isdigit()
            or len(values[key]) > len(str(ceiling))
            or not 1 <= int(values[key]) <= ceiling
        ):
            raise TeleloomError("invalid_budget", f"{key} must be 1..{ceiling}.")
        budgets[key] = int(values[key])
    return {
        "profile": values["profile"],
        "chats": chats,
        "query": values["query"],
        "since": since,
        "until": until,
        **budgets,
    }


def _check(panel: Any, params: dict[str, Any]) -> None:
    """Current exposure and read policy, before anything is started or shown."""
    tool = "messages_search_many_start" if params["query"] else "digest_context_many_start"
    _exposure(panel.settings, tool, "jobs_results", "jobs_status")
    profile = panel.settings.profile(params["profile"])
    for chat in params["chats"]:
        if not chat.isascii() or not chat.removeprefix("-").isdigit():
            raise TeleloomError("invalid_id", "Provide canonical numeric chat IDs.")
        number(chat)
        profile.require_read(chat)


def _failure(exc: TeleloomError) -> str:
    text = _ERRORS.get(exc.code, "This selection cannot start under the current policy.")
    return (
        f'<p class="state" data-state="error" role="alert">{_esc(text)}</p>'
        f'<p class="small"><code>{_esc(exc.code)}</code></p>'
    )


def _unavailable(panel: Any) -> Response:
    body = (
        '<p class="state" data-state="error" role="alert">Owner unavailable. The local owner '
        "is not running, so this view cannot confirm current policy. Start it with "
        "<code>teleloom ui</code> and try again.</p>"
    )
    return HTMLResponse(_layout(panel, "Reading", body), status_code=503, headers=panel._headers())


def _form_html(panel: Any, values: dict[str, str]) -> str:
    options = "".join(
        f'<option value="{_esc(name)}"{" selected" if name == values["profile"] else ""}>'
        f"{_esc(name)}</option>"
        for name in sorted(panel.settings.profiles)
    )
    return f"""<form class="filters" method="get" action="/ui/read">
<label>Profile<select name="profile">{options}</select></label>
<label>Chat IDs<input name="chats" value="{_esc(values["chats"])}" placeholder="100, 200"></label>
<label>Since<input name="since" value="{_esc(values["since"])}" placeholder="2026-11-01T00:00:00+04:00"></label>
<label>Until (exclusive)<input name="until" value="{_esc(values["until"])}"></label>
<label>Query (optional)<input name="query" value="{_esc(values["query"])}"></label>
<label>Max messages<input name="max_messages" value="{_esc(values["max_messages"])}"></label>
<label>Max characters<input name="max_characters" value="{_esc(values["max_characters"])}"></label>
<label>Max requests<input name="max_requests" value="{_esc(values["max_requests"])}"></label>
<button type="submit">Preview</button></form>"""


def _preview(params: dict[str, Any]) -> str:
    rows = [
        ("Profile", f"<code>{_esc(params['profile'])}</code>"),
        ("Chats", "<code>" + _esc(", ".join(params["chats"])) + "</code>"),
        (
            "Period",
            f"{_esc(iso(params['since']))} → {_esc(iso(params['until']))} (end exclusive)",
        ),
        (
            "Query",
            f"<code>{_esc(params['query'])}</code>"
            if params["query"]
            else "Every message in the period",
        ),
        ("Budgets", _esc(", ".join(f"{key}={params[key]}" for key in BUDGETS))),
    ]
    return (
        "<h2>Preview</h2>"
        '<p class="state" data-state="partial" role="status">Nothing has been read yet. '
        "Starting is explicit and creates one durable search job; refreshing or going back "
        "never starts another.</p>"
        "<dl>" + "".join(f"<dt>{key}</dt><dd>{value}</dd>" for key, value in rows) + "</dl>"
    )


def _start_form(csrf: str, params: dict[str, Any]) -> str:
    hidden = {
        "csrf": csrf,
        "profile": params["profile"],
        "chats": " ".join(params["chats"]),
        "since": iso(params["since"]),
        "until": iso(params["until"]),
        "query": params["query"],
        **{key: str(params[key]) for key in BUDGETS},
    }
    fields = "".join(
        f'<input type="hidden" name="{_esc(key)}" value="{_esc(value)}">'
        for key, value in hidden.items()
    )
    return (
        f'<form method="post" action="/ui/read">{fields}'
        '<button type="submit">Start bounded reading job</button></form>'
    )


async def render_preview(panel: Any, request: Request) -> Response:
    if panel.runtime.worker is None:
        return _unavailable(panel)
    values = _form(request.query_params)
    body = _form_html(panel, values)
    if values["submitted"]:
        try:
            params = _params(values)
            _check(panel, params)
        except TeleloomError as exc:
            body += _failure(exc)
        else:
            body += _preview(params) + _start_form(panel._session(request)[1].csrf, params)
    return HTMLResponse(_layout(panel, "Reading preview", body), headers=panel._headers())


async def start(panel: Any, request: Request) -> Response:
    found = panel._session(request)
    if found is None:
        return panel._locked(request)
    if panel.runtime.worker is None:
        return _unavailable(panel)
    length = request.headers.get("content-length", "0")
    if not length.isdigit() or int(length) > 8192:
        return JSONResponse({"error": "request_too_large"}, status_code=413)
    if len(await request.body()) > 8192:
        return JSONResponse({"error": "request_too_large"}, status_code=413)
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not hmac.compare_digest(str(form.get("csrf", "")).encode(), found[1].csrf.encode()):
        return JSONResponse({"error": "invalid_csrf"}, status_code=403)
    values = _form(form)
    try:
        params = _params(values)
        _check(panel, params)
        try:
            async with asyncio.timeout(panel.settings.read_timeout_seconds):
                selection = await panel.runtime.invoke(
                    lambda: panel.runtime.selection(params["profile"], None, params["chats"])
                )
        except TimeoutError:
            raise TeleloomError(
                "read_timeout", "Could not resolve chats before the deadline."
            ) from None
        job = await panel.runtime.jobs.evidence_start(
            params["profile"],
            selection,
            since=params["since"],
            until=params["until"],
            query=params["query"] or None,
            max_messages=params["max_messages"],
            max_characters=params["max_characters"],
            max_requests=params["max_requests"],
        )
    except TeleloomError as exc:
        status, _ = _HTTP.get(exc.code, (400, ""))
        body = _form_html(panel, values) + _failure(exc)
        return HTMLResponse(
            _layout(panel, "Reading preview", body), status_code=status, headers=panel._headers()
        )
    location = (
        "/ui/read/"
        + quote(job["job_id"], safe="")
        + "?profile="
        + quote(params["profile"], safe="")
    )
    return RedirectResponse(location, status_code=303, headers={"Cache-Control": "no-store"})


def _source(item: dict[str, Any]) -> str:
    parts = []
    link = item.get("link")
    if isinstance(link, str) and link.startswith("https://t.me/"):
        parts.append(f'<a href="{_esc(link)}" rel="noreferrer noopener">{_esc(link)}</a>')
    else:
        parts.append(f"chat {_esc(item.get('chat_id'))} · message {_esc(item.get('id'))}")
    if item.get("sender_id"):
        parts.append(f"sender {_esc(item['sender_id'])}")
    if item.get("date"):
        parts.append(_esc(item["date"]))
    return "<br>".join(parts)


def _coverage(page: dict[str, Any]) -> str:
    coverage = page.get("coverage") or {}
    limits = coverage.get("limits") or {}

    def used(key: str, ceiling: str) -> str:
        limit = limits.get(ceiling)
        return f"{_esc(coverage.get(key, 0))} / {_esc(limit) if limit is not None else '—'}"

    stopped = coverage.get("stopped_reason")
    rows = [
        ("Selected chats", _esc(coverage.get("selected_chats", "—"))),
        ("Requests", used("requests", "requests")),
        ("Messages", used("messages", "messages")),
        ("Characters", used("characters", "characters")),
        (
            "Duration limit",
            _esc(limits["duration_seconds"]) if limits.get("duration_seconds") else "none",
        ),
        ("Stopped by", _esc(stopped) if stopped else "—"),
    ]
    if coverage.get("read_policy_redactions"):
        rows.append(("Withheld context", _esc(coverage["read_policy_redactions"])))
    chats = coverage.get("chats") or []
    table = ""
    if chats:
        cells = "".join(
            f"<tr><td><code>{_esc(chat.get('chat_id'))}</code></td>"
            f"<td>{_esc(chat.get('status', 'pending'))}</td>"
            f"<td>{'yes' if chat.get('complete') is True else 'no'}</td>"
            f"<td>{_esc(chat.get('returned', 0))}</td>"
            f"<td>{_esc((chat.get('error') or {}).get('code', '—'))}</td></tr>"
            for chat in chats
        )
        table = (
            "<h3>Per-chat coverage</h3><table><thead><tr><th>Chat</th><th>Status</th>"
            f"<th>Complete</th><th>Returned</th><th>Error</th></tr></thead><tbody>{cells}"
            "</tbody></table>"
        )
    extras = []
    for key, label in (("empty", "Empty in scope"), ("unavailable", "Unavailable or failed")):
        entries = page.get(key) or []
        if entries:
            extras.append(
                f"<dt>{label}</dt><dd>"
                + _esc(
                    ", ".join(
                        str(entry.get("chat_id"))
                        + (
                            f" ({(entry.get('error') or {}).get('code')})"
                            if entry.get("error")
                            else ""
                        )
                        for entry in entries
                    )
                )
                + "</dd>"
            )
    warnings = page.get("warnings") or []
    notice = (
        "<h3>Warnings</h3><ul>" + "".join(f"<li>{_esc(text)}</li>" for text in warnings) + "</ul>"
        if warnings
        else ""
    )
    return (
        "<h2>Coverage and budgets</h2><dl>"
        + "".join(f"<dt>{key}</dt><dd>{value}</dd>" for key, value in rows)
        + "".join(extras)
        + "</dl>"
        + notice
        + table
    )


def _evidence(job: dict[str, Any], page: dict[str, Any]) -> str:
    status = str(page.get("status") or job["status"])
    label = _STATES.get(status, "Unrecognised state")
    coverage = page.get("coverage") or {}
    incomplete = bool(
        page.get("incomplete")
        and (
            not page.get("next_cursor")
            or status != "completed"
            or page.get("source") == "bot_updates"
            or coverage.get("stopped_reason")
            or page.get("unavailable")
            or any(not chat.get("complete") for chat in coverage.get("chats", []))
        )
    )
    notes = {
        "running": "Frozen so far. Refresh this URL to follow the same job; it starts nothing.",
        "queued": "Queued. Refresh this URL to follow the same job; it starts nothing.",
        "paused": "Paused. This page contains the evidence frozen so far.",
        "failed": "This job did not complete; the evidence below is what it recorded.",
        "cancelled": "This job was cancelled; the evidence below is what it recorded.",
    }
    note = notes.get(status, "")
    if incomplete:
        note += (
            " Collection is incomplete: coverage gaps and budget stops remain listed below; "
            "observed counts are not Telegram totals. A missing hit does not prove a message "
            "was absent or a delivery failed."
        )
    rows = [
        ("Job", f"<code>{_esc(job['id'])}</code>"),
        ("Profile", _esc(job["profile_id"])),
        ("State", _esc(label)),
        (
            "Query",
            f"<code>{_esc(job['payload'].get('query'))}</code>"
            if job["payload"].get("query")
            else "Every message in the period",
        ),
        (
            "Period",
            f"{_esc(job['payload'].get('since'))} → {_esc(job['payload'].get('until'))} "
            "(end exclusive)",
        ),
        ("Frozen at", _esc(page.get("result_snapshot_at", "—"))),
    ]
    if reference := page.get("evidence_ref"):
        rows.extend(
            [
                ("Evidence reference", f"<code>{_esc(reference)}</code>"),
                ("Reference expires", _esc(page.get("reference_expires_at") or "—")),
            ]
        )
    items = page.get("items") or []
    cells = "".join(
        f"<tr><td>{_esc(item.get('date', '—'))}</td>"
        f"<td><code>{_esc(item.get('chat_id'))}</code><br><code>{_esc(item.get('id'))}</code></td>"
        f"<td>{_esc(item.get('text_source', 'original'))}</td>"
        f"<td>{_source(item)}</td>"
        f'<td style="white-space:pre-wrap">{_esc(item.get("text", ""))}</td></tr>'
        for item in items
    )
    params = quote(job["profile_id"], safe="")
    links = [
        f'<a href="/ui/read/{_esc(job["id"])}?profile={params}">First page</a>',
        f'<a href="/ui/jobs/{_esc(job["id"])}?profile={params}">Job detail</a>',
        '<a href="/ui/read">New preview</a>',
    ]
    if page.get("next_cursor"):
        links.insert(
            0,
            f'<a href="/ui/read/{_esc(job["id"])}?profile={params}'
            f'&amp;p={_esc(page["next_cursor"])}">Next page →</a>',
        )
    return (
        f'<p class="state" data-state="{_esc("unknown" if incomplete else status)}" '
        f'role="status"><strong>{_esc(label)}</strong> — {_esc(note or "Frozen owner evidence.")}</p>'
        + "<dl>"
        + "".join(f"<dt>{key}</dt><dd>{value}</dd>" for key, value in rows)
        + "</dl>"
        + _coverage(page)
        + (
            f'<p role="status">{len(items)} frozen message(s) on this page, '
            f"at most {PAGE} per page.</p>"
        )
        + (
            '<table><caption class="small">Frozen messages, newest first per chat</caption>'
            "<thead><tr><th>Date</th><th>Chat / message</th><th>Text source</th><th>Source</th>"
            f"<th>Text</th></tr></thead><tbody>{cells}</tbody></table>"
            if items
            else '<p class="state" data-state="empty" role="status">No messages are frozen in '
            "this page of the selection. Empty coverage is not proof that Telegram has nothing."
            "</p>"
        )
        + '<p class="small">'
        + " · ".join(links)
        + "</p>"
    )


async def render(panel: Any, request: Request, job_id: str) -> Response:
    if panel.runtime.worker is None:
        return _unavailable(panel)
    profile = request.query_params.get("profile") or ""
    try:
        if not profile:
            raise TeleloomError("job_not_found", "A profile is required to read this job.")
        _exposure(panel.settings, "jobs_results")
        job = panel.runtime.jobs.panel_job(profile, job_id)
        if job["kind"] != "evidence":
            raise TeleloomError("job_not_found", "This job is not a bounded reading collection.")
        page = await panel.runtime.jobs.results(
            profile, job_id, request.query_params.get("p") or None, PAGE
        )
        # Fails closed on a cross-profile row or a chat outside the current read policy.
        page = scoped_evidence(panel.settings.profile(profile), profile, page)
    except TeleloomError as exc:
        status, text = _HTTP.get(
            exc.code, (400, "This reading evidence is unavailable under the current policy.")
        )
        body = (
            f'<p class="state" data-state="error" role="alert">{_esc(text)}</p>'
            f'<p class="small"><code>{_esc(exc.code)}</code></p>'
            '<p><a href="/ui/read">Preview a reading job</a></p>'
        )
        return HTMLResponse(
            _layout(panel, "Frozen evidence", body), status_code=status, headers=panel._headers()
        )
    return HTMLResponse(
        _layout(panel, "Frozen evidence", _evidence(job, page), notice=_notice(panel, {profile})),
        headers=panel._headers(),
    )
