"""Read-only owner views over durable jobs and delivery receipts.

The panel never mutates job state: it only reads the owner's SQLite snapshot,
applies the current exposure/read/account policy before disclosing anything and
paginates with keyset cursors so stable positions survive refreshes and restarts.
"""

import html
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse

from .browser_cache import protected_page
from .models import TeleloomError
from .runtime import cursor_decode, cursor_encode

JOB_PAGE = 25
RECEIPT_PAGE = 10
JOB_KINDS = (
    "activity",
    "attachments",
    "delivery",
    "events",
    "evidence",
    "export",
    "sync",
    "transcription",
    "unread_export",
)

_JOB_STATES = {
    "queued": ("Queued", "No work has started for this job."),
    "running": ("Running", "The owner is working on this job now."),
    "paused": ("Paused", "Progress is retained and nothing is being sent."),
    "completed": ("Completed", "The owner recorded this job as finished."),
    "failed": ("Failed", "Some or all work did not complete; read the recorded error."),
    "cancelled": ("Cancelled", "This job was cancelled; remaining work was not attempted."),
    "needs_review": (
        "Needs review — outcome unknown",
        "The external service may have accepted part of this job. Check its evidence before retrying.",
    ),
}

_DELIVERY_STATES = {
    "pending": ("Not attempted", "This target was not sent."),
    "sending": (
        "Send in progress",
        "Waiting for Telegram to acknowledge this target. If it stays here, reconcile in Telegram.",
    ),
    "sent": ("Sent", "Telegram acknowledged this target and returned its message identity."),
    "failed": ("Failed", "Telegram rejected this target; nothing was resent."),
    "partial": (
        "Partial",
        "Telegram acknowledged only part of this target's work. Inspect the receipts.",
    ),
    "cancelled": ("Cancelled", "This target was not attempted."),
    "unknown": (
        "Unknown — reconcile",
        "Telegram may have received this send. Reconcile it in Telegram; do not resend it.",
    ),
}

_ERRORS = {
    "telegram_rejected": "Telegram rejected this work; nothing was resent.",
    "peer_unavailable": "The target peer is unavailable.",
    "peer_identity_changed": "A target identity changed; create and confirm a new preview.",
    "read_not_allowed": "The owner's read policy no longer includes a target of this job.",
    "account_changed": "This job belongs to a previous account for this profile.",
    "mutation_partial": "Telegram acknowledged only part of the operation. Inspect the receipts.",
    "mutation_budget_exhausted": (
        "Telegram acknowledged bounded batches with work remaining. Inspect the receipts."
    ),
    "delivery_unknown": "The send outcome is uncertain. Reconcile it in Telegram.",
    "connection_error": "The owner lost its Telegram connection during this job.",
    "export_cancelled": "This export was cancelled.",
    "rate_limited": "Telegram requested a wait before the next attempt.",
    "timeout": "The attempt timed out. Inspect the receipts before taking action.",
}

_HTTP = {
    "job_not_found": (404, "No job with this identity is visible to this owner profile."),
    "profile_not_found": (404, "That profile is not configured for this owner."),
    "read_not_allowed": (403, "The owner's read policy does not allow this job's data."),
    "read_policy_changed": (403, "This record belongs to another read policy."),
    "account_changed": (403, "This job belongs to a previous account for this profile."),
    "tool_not_exposed": (403, "Job inspection is disabled by the owner's exposure policy."),
    "invalid_cursor": (400, "This page cursor is invalid or belongs to another view."),
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _stamp(value: Any) -> str:
    """Render a stored UTC epoch or ISO timestamp without inventing one."""
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is not None:
                return stamp.astimezone(UTC).isoformat()
        except (OverflowError, ValueError):
            pass
    return "—"


def _error(error: Any) -> tuple[str, str]:
    code = str(error.get("code")) if isinstance(error, dict) else "error"
    if code in _ERRORS:
        return code, _ERRORS[code]
    return "error", "This job recorded an error. Inspect its status and any available receipts."


_LOADING_SCRIPT = """const loading=document.querySelector('#jobs-loading');
const view=document.querySelector('#job-view');
function navigate(action){
    loading.hidden=false;
    view.setAttribute('aria-busy','true');
    requestAnimationFrame(()=>requestAnimationFrame(action));
}
view.querySelector('.filters')?.addEventListener('submit',(event)=>{
    event.preventDefault();
    navigate(()=>event.target.submit());
});
view.addEventListener('click',(event)=>{
    if(event.button!==0||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey)return;
    const link=event.target.closest('a[href^="/ui/jobs"]');
    if(!link)return;
    event.preventDefault();
    navigate(()=>location.assign(link.href));
});"""


def _layout(title: str, body: str, *, notice: str = "", nonce: str | None = None) -> str:
    loading = (
        '<p class="state" id="jobs-loading" role="status" hidden>Loading durable job state…</p>'
        if nonce
        else ""
    )
    script = f'<script nonce="{_esc(nonce)}">{_LOADING_SCRIPT}</script>' if nonce else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · teleloom</title><style>
:root{{color-scheme:light;font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:6vh 24px}}main{{max-width:960px;margin:auto}}
p{{line-height:1.6;color:#68665d}}h1{{font-size:34px;letter-spacing:-.04em;margin:.2em 0}}
h2{{font-size:19px;margin-top:1.8em}}code{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;border-radius:4px}}
table{{width:100%;border-collapse:collapse;margin:1.5em 0;font-size:15px}}
th,td{{text-align:left;padding:.55em .6em;border-bottom:1px solid #d8d3c7;vertical-align:top}}
th{{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#68665d}}
dl{{display:grid;grid-template-columns:minmax(140px,auto) 1fr;gap:10px 24px;margin:1.5em 0}}
dt{{font-weight:600}}dd{{margin:0;overflow-wrap:anywhere}}
nav{{display:flex;gap:1.2em;flex-wrap:wrap;margin-bottom:1em}}a{{color:#24231f}}
form.filters{{display:flex;gap:1em;flex-wrap:wrap;align-items:end;margin:1.5em 0}}
label{{display:flex;flex-direction:column;gap:.3em;font-size:13px;color:#68665d}}
select,input,button{{font:inherit;padding:7px 10px;border-radius:6px;border:1px solid #b8b4a8;background:white}}
button{{cursor:pointer}}.state{{border-left:4px solid #68665d;padding:.8em 1em;background:#ece8df}}
.state[data-state=unknown]{{border-color:#9c6b11}}.state[data-state=error]{{border-color:#9c2828}}
.state[data-state=empty]{{border-color:#3f6b3f}}.small{{font-size:14px}}
a:focus-visible,button:focus-visible,select:focus-visible{{outline:3px solid #24231f;outline-offset:3px}}
footer{{margin-top:3em;border-top:1px solid #d8d3c7;padding-top:1em}}
</style></head><body><main><p class="small">teleloom / owner panel</p>
<nav><a href="/ui">Status</a><a href="/ui/jobs">Jobs</a><a href="/ui/read">Reading</a></nav>
{notice}<h1>{_esc(title)}</h1>{loading}<div id="job-view">{body}</div>
<footer class="small">Read-only inspector. Refreshing this page re-reads durable owner state only;
it never sends, confirms, resumes or replays an action.</footer>{script}</main></body></html>"""


def render(panel: Any, request: Request, job_id: str | None) -> HTMLResponse:
    """Render the jobs list or one job's detail for an authenticated owner session."""
    if panel.runtime.worker is None:
        body = (
            '<p class="state" data-state="error" role="alert">Owner unavailable. The local owner '
            "is not running, so this view cannot confirm current policy. Start it with "
            "<code>teleloom ui</code> and try again.</p>"
        )
        return HTMLResponse(_layout("Jobs", body), status_code=503, headers=panel._headers())
    try:
        body, notice = _detail(panel, request, job_id) if job_id else _list(panel, request)
    except TeleloomError as exc:
        status, text = _HTTP.get(exc.code, (500, "The panel could not read durable job state."))
        body = (
            f'<p class="state" data-state="error" role="alert">{_esc(text)}</p>'
            f"<p><code>{_esc(exc.code if exc.code in _HTTP else 'error')}</code></p>"
            '<p><a href="/ui/jobs">Back to jobs</a></p>'
        )
        return HTMLResponse(_layout("Jobs", body), status_code=status, headers=panel._headers())
    nonce = secrets.token_urlsafe(16)
    return protected_page(
        _layout("Jobs", body, notice=notice, nonce=nonce), panel._headers, nonce=nonce
    )


def _notice(panel: Any, profiles: set[str]) -> str:
    stale = sorted(name for name in profiles if name in panel.runtime.profile_errors)
    if not stale:
        return ""
    return (
        '<p class="state" data-state="unknown" role="status">Connection unavailable for '
        f"{_esc(', '.join(stale))}. Showing the last durable snapshot; no Telegram read is "
        "attempted here.</p>"
    )


def _options(values: list[str] | tuple[str, ...], selected: str | None) -> str:
    return "".join(
        f'<option value="{_esc(value)}"{" selected" if value == selected else ""}>'
        f"{_esc(value)}</option>"
        for value in values
    )


def _filters(panel: Any, profile: str | None, kind: str | None, status: str | None) -> str:
    profiles = sorted(panel.settings.profiles)
    all_profiles = '<option value="">All profiles</option>' + _options(profiles, profile)
    all_kinds = '<option value="">All types</option>' + _options(JOB_KINDS, kind)
    all_states = '<option value="">All states</option>' + _options(tuple(_JOB_STATES), status)
    return f"""<form class="filters" method="get" action="/ui/jobs">
<label>Profile<select name="profile">{all_profiles}</select></label>
<label>Type<select name="kind">{all_kinds}</select></label>
<label>State<select name="status">{all_states}</select></label>
<button type="submit">Apply filters</button></form>"""


def _list(panel: Any, request: Request) -> tuple[str, str]:
    query = request.query_params
    profile = query.get("profile") or None
    kind = query.get("kind") or None
    status = query.get("status") or None
    if profile is not None:
        panel.settings.profile(profile)
    panel.runtime.jobs.check_panel_exposure()
    scope = {"view": "jobs", "profile": profile, "kind": kind, "status": status}
    rows = panel.runtime.store.job_page(
        cursor_decode(query.get("cursor"), scope),
        JOB_PAGE + 1,
        profile=profile,
        kind=kind,
        status=status,
    )
    more = len(rows) > JOB_PAGE
    rows = rows[:JOB_PAGE]
    next_cursor = cursor_encode(scope, rows[-1]["seq"]) if more and rows else None
    cells, profiles = [], set()
    for row in rows:
        try:
            panel.runtime.jobs.check_panel(row["profile_id"], row)
        except TeleloomError:
            cells.append('<tr><td colspan="6">Restricted under the current policy</td></tr>')
            continue
        profiles.add(row["profile_id"])
        label, note = _JOB_STATES.get(
            row["status"], ("Unrecognised state", "This state is not part of the known contract.")
        )
        link = f"/ui/jobs/{quote(row['id'], safe='')}?{urlencode({'profile': row['profile_id']})}"
        cells.append(
            f"<tr><td>{_esc(_stamp(row.get('created_at')))}</td>"
            f"<td>{_esc(row['profile_id'])}</td><td>{_esc(row['kind'])}</td>"
            f'<td><span title="{_esc(note)}">{_esc(label)}</span></td>'
            f"<td>{_esc(row.get('progress', 0))}</td>"
            f'<td><a href="{link}">Inspect</a></td></tr>'
        )
    if not rows:
        state = (
            '<p class="state" data-state="empty" role="status">No jobs match these filters. '
            "Jobs appear here once the owner records one; this page never creates a job.</p>"
        )
    else:
        state = (
            f'<p role="status">{len(rows)} job(s) shown'
            f"{' — more pages available' if more else ''}.</p>"
        )
    pager = ""
    if next_cursor:
        params = {key: query[key] for key in ("profile", "kind", "status") if query.get(key)}
        params["cursor"] = next_cursor
        pager = f'<p><a href="/ui/jobs?{_esc(urlencode(params))}">Next page →</a></p>'
    body = (
        _filters(panel, profile, kind, status)
        + state
        + (
            '<table><caption class="small">Durable jobs, newest first</caption><thead><tr>'
            "<th>Created (UTC)</th><th>Profile</th><th>Type</th><th>State</th><th>Progress</th>"
            f"<th>Job</th></tr></thead><tbody>{''.join(cells)}</tbody></table>"
            if rows
            else ""
        )
        + pager
    )
    return body, _notice(panel, profiles)


def _detail(panel: Any, request: Request, job_id: str) -> tuple[str, str]:
    profile = request.query_params.get("profile") or ""
    if not profile:
        raise TeleloomError("job_not_found", "A profile is required to inspect this job.")
    job = panel.runtime.jobs.panel_job(profile, job_id)
    label, note = _JOB_STATES.get(
        job["status"], ("Unrecognised state", "This state is not part of the known contract.")
    )
    detail = [
        ("Job ID", f"<code>{_esc(job['id'])}</code>"),
        ("Profile", _esc(job["profile_id"])),
        ("Type", _esc(job["kind"])),
        ("State", _esc(label)),
        ("Created (UTC)", _esc(_stamp(job.get("created_at")))),
        ("Progress", _esc(job.get("progress", 0))),
    ]
    if job.get("next_run"):
        detail.append(("Next run (UTC)", _esc(_stamp(job["next_run"]))))
    body = (
        f'<p class="state" data-state="{"unknown" if job["status"] == "needs_review" else _esc(job["status"])}" '
        f'role="status"><strong>{_esc(label)}</strong> — {_esc(note)}</p>'
        "<dl>" + "".join(f"<dt>{key}</dt><dd>{value}</dd>" for key, value in detail) + "</dl>"
    )
    if job.get("error"):
        code, text = _error(job["error"])
        body += (
            "<h2>Recorded error</h2>"
            f'<p class="state" data-state="error"><code>{_esc(code)}</code> {_esc(text)}</p>'
        )
    if job["kind"] == "delivery":
        receipts, pager = _receipts(panel, request, job)
        body += receipts + pager
    body += '<p><a href="/ui/jobs">← All jobs</a></p>'
    return body, _notice(panel, {job["profile_id"]})


def _target(entry: dict[str, Any]) -> str:
    if entry.get("chat_id"):
        return "chat " + str(entry["chat_id"])
    target = entry.get("target")
    if isinstance(target, dict):
        if target.get("chat_id"):
            return "chat " + str(target["chat_id"])
        if target.get("kind") == "account":
            scope = target.get("scope")
            return f"account ({scope})" if scope and scope != "account" else "account"
    return "target"


def _receipt_ids(entry: dict[str, Any]) -> list[str]:
    items = [
        *(entry.get("receipts") or []),
        *([entry["receipt"]] if isinstance(entry.get("receipt"), dict) else []),
    ]
    ids = [str(entry["message_id"])] if entry.get("message_id") else []
    for item in items:
        ids.extend(str(id_) for id_ in (item.get("message_ids") or []) if id_)
    return list(dict.fromkeys(ids))


def _scheduled(entry: dict[str, Any]) -> str | None:
    items = [
        *(entry.get("receipts") or []),
        *([entry["receipt"]] if isinstance(entry.get("receipt"), dict) else []),
    ]
    for item in items:
        if item.get("schedule_accepted"):
            return _stamp(item.get("scheduled_for"))
    return None


def _receipts(panel: Any, request: Request, job: dict[str, Any]) -> tuple[str, str]:
    scope = {"view": "receipts", "profile": job["profile_id"], "job": job["id"]}
    start = cursor_decode(request.query_params.get("r"), scope) or 0
    entries = panel.runtime.store.delivery_page(job["id"], start, RECEIPT_PAGE + 1)
    more = len(entries) > RECEIPT_PAGE
    entries = entries[:RECEIPT_PAGE]
    counts = panel.runtime.store.delivery_counts(job["id"])
    total = sum(counts.values())
    summary = ", ".join(
        f"{count} {'acknowledged' if name == 'sent' else name}"
        for name, count in sorted(counts.items())
    )
    rows = []
    for position, entry in entries:
        state = str(entry.get("status", "unknown"))
        label, note = _DELIVERY_STATES.get(
            state,
            ("Unrecognised state", "This state is not part of the known contract."),
        )
        ids = _receipt_ids(entry)
        scheduled = _scheduled(entry)
        if scheduled and state == "sent":
            label, note = "Schedule accepted", "Telegram acknowledged the schedule request."
        parts = ["; ".join(f"<code>{_esc(id_)}</code>" for id_ in ids)]
        if scheduled:
            parts.append(
                f"Scheduled for {_esc(scheduled)} — accepted schedule, not a delivered message."
            )
        identity = "<br>".join(part for part in parts if part) or "—"
        if entry.get("error"):
            code, text = _error(entry["error"])
            identity += f'<br><span class="small"><code>{_esc(code)}</code> {_esc(text)}</span>'
        rows.append(
            f"<tr><td>{position + 1}</td><td>{_esc(_target(entry))}</td>"
            f'<td><span title="{_esc(note)}">{_esc(label)}</span></td>'
            f"<td>{_esc(_stamp(entry.get('attempted_at')))}</td><td>{identity}</td></tr>"
        )
    reconciliation = ""
    if counts.get("unknown") or counts.get("sending") or job["status"] == "needs_review":
        reconciliation = (
            '<p class="state" data-state="unknown" role="alert"><strong>Outcome unknown.</strong> '
            "Telegram may have received part of this job. Reconcile it with evidence in Telegram "
            "before creating a new plan. The panel never resends an unknown or partial send.</p>"
        )
    body = (
        f"<h2>Targets and receipts</h2>"
        f'<p role="status">{total} target(s): {_esc(summary or "none recorded")}. '
        + (f"Showing positions {entries[0][0] + 1}–{entries[-1][0] + 1}." if entries else "")
        + "</p>"
        + reconciliation
        + (
            "<table><thead><tr><th>#</th><th>Target</th><th>State</th><th>Attempted (UTC)</th>"
            f"<th>Identities</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
            if entries
            else '<p class="state" data-state="empty" role="status">No targets were recorded '
            "for this job.</p>"
        )
    )
    pager = ""
    if more and entries:
        params = urlencode(
            {"profile": job["profile_id"], "r": cursor_encode(scope, entries[-1][0] + 1)}
        )
        pager = f'<p><a href="/ui/jobs/{quote(job["id"], safe="")}?{_esc(params)}">Next receipts →</a></p>'
    return body, pager
