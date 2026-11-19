"""Owner-only panel views: profiles, tool exposure and one explicit bounded probe."""

import html
from typing import Any

from .config import Settings
from .runtime import Runtime

# An unopened lazy connection is not an error; only these codes mean the owner
# must repair a credential before the profile can connect at all.
CREDENTIAL_ERRORS = frozenset(
    {"credentials_missing", "credentials_unavailable", "credentials_invalid", "auth_required"}
)

# Explicit allowlist: engine status below projects only safe fields.
CAPABILITY_KEYS = (
    "read_policy",
    "telegram_history",
    "explicit_message_lookup",
    "telegram_global_search",
    "exact_sender_search",
    "local_retrieval",
    "public_chat_search",
    "message_context",
    "rich_evidence",
    "telegram_unread",
    "local_inbox",
    "polling_enabled",
    "folder_membership",
    "topics",
    "threads",
    "channel_comments",
    "pinned_posts",
    "selected_attachments",
    "attachment_processing",
    "events",
    "event_delivery",
    "transcription",
    "transcription_providers",
    "media",
    "management",
    "send",
    "broadcast",
)

BOT_LIMIT = (
    "Bot backend exposes saved updates only: live Telegram history, global search and "
    "channel comments are unavailable, and saved coverage is never full history."
)


def profiles_data(settings: Settings, runtime: Runtime) -> dict[str, Any]:
    """Safe profile projection from the same contract as profiles_list."""
    from .diagnostics import failure

    scope = runtime.profiles()
    rows = []
    for item in scope["profiles"]:
        profile = settings.profiles[item["id"]]
        error = item["error"]
        if error in CREDENTIAL_ERRORS:
            state = "credential"
        elif error:
            state = "error"
        else:
            state = "connected" if item["connected"] else "not_opened"
        capabilities = item["capabilities"]
        engines = capabilities["transcription_engines"]
        rows.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "backend": item["backend"],
                "identity": item["identity"],
                "proxy": item["proxy"],
                "state": state,
                "error": error,
                "next_action": failure(error, "connect", profile)["next_action"] if error else None,
                "read_policy": capabilities["read_policy"],
                "grants": item["grants"],
                "capabilities": {
                    **{key: capabilities[key] for key in CAPABILITY_KEYS},
                    "transcription_engines": {
                        "default_provider": engines["default_provider"],
                        "providers": {
                            name: {
                                key: engine[key]
                                for key in ("available", "reason", "external_upload")
                                if key in engine
                            }
                            for name, engine in engines["providers"].items()
                        },
                    },
                },
                "limits": (
                    [BOT_LIMIT]
                    if item["backend"] == "bot_api"
                    else ["Bot history uses saved updates. Exact message lookup uses MTProto."]
                    if item["kind"] == "bot"
                    else []
                ),
            }
        )
    return {"tool_exposure": scope["tool_exposure"], "profiles": rows}


async def tool_catalogue(settings: Settings, runtime: Runtime) -> dict[str, Any]:
    """Owner-wide exposure of every registered tool, from the real MCP registry.

    Listing tools registers handlers only: no adapter, Telegram call or AI call.
    """
    from .server import create_server

    live = {tool.name for tool in await create_server(settings, runtime=runtime).list_tools()}
    # exposure_mode="all" reveals tools the owner hid, so the panel can explain them.
    registered = await create_server(
        settings.model_copy(update={"exposure_mode": "all"}), runtime=runtime
    ).list_tools()
    tools = []
    for tool in sorted(registered, key=lambda item: item.name):
        required = set(tool.inputSchema.get("required", ()))
        annotations = tool.annotations
        tools.append(
            {
                "name": tool.name,
                "summary": (tool.description or "").splitlines()[0] if tool.description else "",
                "exposed": tool.name in live,
                "needs_profile": "profile_id" in required,
                "needs_chat": bool({"chat_id", "chat_ids"} & required),
                "writes": bool(annotations and annotations.readOnlyHint is False),
                "external": bool(annotations and annotations.openWorldHint),
            }
        )
    selected = settings.exposure_mode == "selected"
    return {
        "mode": settings.exposure_mode,
        "selected_tools": sorted(settings.exposed_tools) if selected else [],
        "count": len(tools),
        "exposed": sum(1 for tool in tools if tool["exposed"]),
        "tools": tools,
    }


async def probe(
    runtime: Runtime, profile_id: str | None, chat_id: str | None, timeout_seconds: float
) -> dict[str, Any]:
    """One bounded read with an explicit profile and chat; content never leaves the runtime."""
    from .diagnostics import selected_probe

    result = await selected_probe(runtime, profile_id, chat_id, timeout_seconds)
    view = {
        key: result.get(key)
        for key in (
            "profile_id",
            "chat_id",
            "status",
            "phase",
            "timeout_seconds",
            "max_messages",
            "max_history_requests",
            "logical_requests",
            "timings_seconds",
            "returned",
        )
    }
    view["error"] = result.get("error")
    return view


_TOOL_CAPABILITIES = {
    "messages_get": "message_context",
    "messages_search": "message_context",
    "messages_search_global": "telegram_global_search",
    "chats_search": "public_chat_search",
    "comments_get": "channel_comments",
    "context_get": "message_context",
    "folders_list": "folder_membership",
    "folders_snapshot": "folder_membership",
    "folder_members": "folder_membership",
    "topics_list": "topics",
    "topic_history": "topics",
    "thread_get": "threads",
    "messages_pinned": "pinned_posts",
}


def applicable(profile: dict[str, Any] | None, tool: dict[str, Any]) -> str | None:
    if profile is None or (key := _TOOL_CAPABILITIES.get(tool["name"])) is None:
        return None
    value = profile["capabilities"][key]
    if value is False:
        return f"{_label(key)} is unavailable for this backend."
    if value == "saved_updates_only":
        return "Saved updates only. Coverage is limited to updates recorded by this owner."
    return None


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


STYLE = """<style>
:root{color-scheme:light;font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}
body{margin:0;padding:7vh 24px}main{max-width:820px;margin:auto}
h1{font-size:34px;letter-spacing:-.04em;margin:.3em 0}h2{font-size:19px;margin-top:2.2em}
p,li{line-height:1.6;color:#585650}dt{font-weight:600}dd{margin:0;overflow-wrap:anywhere}
dl{display:grid;grid-template-columns:minmax(160px,auto) 1fr;gap:10px 22px;margin:1.4em 0}
code{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .35em;border-radius:4px}
nav a{color:#24231f;margin-right:14px}section{border-top:1px solid #d8d3c7;padding-top:.4em}
table{border-collapse:collapse;width:100%;font-size:15px}td,th{text-align:left;padding:.45em .5em;
border-bottom:1px solid #e2ddd2;vertical-align:top}th{font-weight:600}
.badge{display:inline-block;border:1px solid #b8b4a8;border-radius:999px;padding:.05em .55em;
margin-right:.35em;font-size:12px;background:white}
.badge[data-on=false]{color:#8a1f1f;border-color:#c9a3a3}
form{display:grid;gap:.7em;max-width:420px;margin:1.2em 0}label{font-weight:600;font-size:14px}
input,select,button{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid #b8b4a8;background:white}
button{cursor:pointer;width:max-content}
.state{border-left:4px solid #68665d;padding:.7em 1em;background:#ece8df;margin:1.2em 0}
.state[data-state=credential],.state[data-state=error]{border-color:#9c2828}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{
outline:3px solid #24231f;outline-offset:3px}
.small{font-size:14px}footer{margin-top:3em;border-top:1px solid #d8d3c7;padding-top:1em}
</style>"""

NAV = (
    '<nav class="small" aria-label="Panel sections"><a href="/ui">Status</a>'
    '<a href="/ui/profiles">Profiles</a><a href="/ui/tools">Tools</a></nav>'
)

SIGN_OUT = (
    '<form method="post" action="/ui/logout"><input type="hidden" name="csrf" value="{csrf}">'
    '<button type="submit">Sign out of this panel</button></form>'
)


def shell(title: str, body: str, csrf: str, *, nonce: str | None = None, script: str = "") -> str:
    tag = f'<script nonce="{_esc(nonce)}">{script}</script>' if nonce and script else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · teleloom</title>{STYLE}</head><body><main>
<p class="small">teleloom / owner panel</p>{NAV}<h1>{_esc(title)}</h1>
{body}
{SIGN_OUT.format(csrf=_esc(csrf))}
<footer class="small">Read-only views. Opening or refreshing a page never reads Telegram,
enumerates dialogs, signs in or calls AI.</footer>{tag}
</main></body></html>"""


def _probe_form(profiles: list[dict[str, Any]], csrf: str) -> str:
    if not profiles:
        return (
            '<p class="small">Add a profile with teleloom auth user or auth bot to run a probe.</p>'
        )
    options = "".join(
        f'<option value="{_esc(item["id"])}">{_esc(item["id"])} ({_esc(item["kind"])})</option>'
        for item in profiles
    )
    timeouts = "".join(
        f'<option value="{value}"{" selected" if value == 3 else ""}>{value} s</option>'
        for value in (1, 3, 5, 10)
    )
    return f"""<section aria-labelledby="probe"><h2 id="probe">One explicit connection probe</h2>
<p>Checks for at most one message in a chat this profile may read. The result shows the
number of messages found and timing. The probe never returns message content or retries.</p>
<form method="post" action="/ui/probe">
<label for="probe-profile">Profile</label>
<select id="probe-profile" name="profile_id" required><option value="">Select a profile</option>{options}</select>
<label for="probe-chat">Chat ID (canonical numeric)</label>
<input id="probe-chat" name="chat_id" required inputmode="numeric" autocomplete="off"
 aria-describedby="probe-chat-hint">
<span id="probe-chat-hint" class="small">The exact chat must already be allowed by this
profile's read policy.</span>
<label for="probe-timeout">Timeout</label>
<select id="probe-timeout" name="timeout_seconds">{timeouts}</select>
<input type="hidden" name="csrf" value="{_esc(csrf)}">
<button type="submit" data-probe>Run bounded probe</button>
<p id="probe-status" class="small" role="status" aria-live="polite"></p>
</form></section>"""


PROBE_SCRIPT = """document.addEventListener('submit',async (event)=>{
const form=event.target;
const button=form.querySelector('[data-probe]');
if(!button)return;
event.preventDefault();
if(button.disabled)return;
const status=form.querySelector('#probe-status');
button.disabled=true;
status.textContent='Running one bounded read…';
try {
    const response=await fetch(form.action,{method:'POST',body:new URLSearchParams(new FormData(form))});
    if(!response.ok)throw new Error();
    const page=new DOMParser().parseFromString(await response.text(),'text/html');
    if(!page.querySelector('#probe-result'))throw new Error();
    document.querySelector('main').replaceChildren(...page.querySelector('main').childNodes);
    document.querySelector('#probe-result').focus();
} catch {
    status.textContent='Unable to display the probe result. Reload to check your panel session.';
} finally {
    button.disabled=false;
}});"""


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _fields(fields: dict[str, Any]) -> str:
    return (
        "<dl>"
        + "".join(
            f"<dt>{_esc(_label(key))}</dt><dd>{_field_value(value)}</dd>"
            for key, value in fields.items()
        )
        + "</dl>"
    )


def _field_value(value: Any) -> str:
    if isinstance(value, dict):
        return _fields(value)
    if isinstance(value, list):
        return ", ".join(_field_value(item) for item in value) or "None"
    if isinstance(value, bool):
        return "Available" if value else "Unavailable"
    return _esc(str(value).replace("_", " ") if value == "saved_updates_only" else value)


def profiles_html(
    data: dict[str, Any],
    csrf: str,
    *,
    nonce: str | None = None,
    result: dict[str, Any] | None = None,
) -> str:
    profiles = data["profiles"]
    exposure = data["tool_exposure"]
    states = {
        "connected": "Connected. This profile has an open adapter in this owner.",
        "not_opened": "Not opened yet. The connection is lazy and starts only when a tool or probe needs it.",
        "credential": "Credential problem. This profile cannot connect until the owner repairs it.",
        "error": "Connection error. Review the next step below.",
    }
    rows = "".join(
        f'<section aria-labelledby="p-{_esc(item["id"])}">'
        f'<h2 id="p-{_esc(item["id"])}">{_esc(item["id"])} '
        f'<span class="badge">{_esc(item["kind"])}</span>'
        f'<span class="badge">{_esc(item["backend"])}</span></h2>'
        f'<p class="state" data-state="{_esc(item["state"])}" role="status">'
        f"{_esc(states[item['state']])}</p>"
        + (
            f'<p><span class="badge" data-on="false">error</span><code>{_esc(item["error"])}</code></p>'
            f"<p>{_esc(item['next_action'])}</p>"
            if item["error"]
            else ""
        )
        + "<dl>"
        f"<dt>Account ID</dt><dd><code>{_esc(item['identity'].get('id', 'unknown'))}</code></dd>"
        f"<dt>Username</dt><dd>{_esc(item['identity'].get('username') or 'not reported')}</dd>"
        f"<dt>Display name</dt><dd>{_esc(item['identity'].get('name') or 'not reported')}</dd>"
        f"<dt>Connection route</dt><dd>{_esc(item['proxy']['name'])}</dd>"
        f"<dt>Read policy</dt><dd>{_esc(item['read_policy']['mode'])}"
        + (
            " · " + ", ".join(_esc(chat) for chat in item["read_policy"]["chat_ids"])
            if item["read_policy"]["chat_ids"]
            else ""
        )
        + "</dd>"
        + "".join(f"<dt>Limit</dt><dd>{_esc(limit)}</dd>" for limit in item["limits"])
        + "</dl><h3>Grants</h3>"
        + _fields({grant: value for grant, value in item["grants"].items() if grant != "read"})
        + "<details><summary>Backend capabilities</summary>"
        + _fields(
            {key: value for key, value in item["capabilities"].items() if key != "read_policy"}
        )
        + '</details><p class="small"><a href="/ui/tools?profile='
        + _esc(item["id"])
        + '">Tool availability for this profile</a></p></section>'
        for item in profiles
    )
    if not profiles:
        rows = (
            '<p class="state" data-state="empty" role="status">No profiles are configured. '
            "Add one with teleloom auth user or auth bot.</p>"
        )
    body = (
        f'<p class="small">Tool exposure: <code>{_esc(exposure["mode"])}</code>'
        + (
            " · selected: " + _esc(", ".join(exposure["selected_tools"]))
            if exposure["selected_tools"]
            else ""
        )
        + "</p>"
        + rows
        + _probe_form(profiles, csrf)
    )
    if result is not None:
        body = _probe_result(result) + body
    return shell(
        "Profiles and capabilities",
        body,
        csrf,
        nonce=nonce,
        script=PROBE_SCRIPT,
    )


def _probe_result(result: dict[str, Any]) -> str:
    returned = result["returned"]
    count = "—" if returned is None else f"{returned} message{'s' if returned != 1 else ''}"
    if result["status"] == "ok":
        detail = (
            f'<p class="state" data-state="ok" role="status">Selected read succeeded in '
            f"phase <code>{_esc(result['phase'])}</code>. Returned {_esc(count)}.</p>"
        )
    elif result["error"]:
        detail = (
            f'<p class="state" data-state="error" role="alert">Probe failed in phase '
            f"<code>{_esc(result['error']['phase'])}</code> with "
            f"<code>{_esc(result['error']['code'])}</code>.</p>"
            f"<p>{_esc(result['error']['next_action'])}</p>"
        )
    else:
        detail = '<p class="state" data-state="error" role="alert">Probe returned no result.</p>'
    timings = ", ".join(
        f"{_esc(phase)}: {_esc('n/a' if value is None else round(value, 4))}"
        for phase, value in result["timings_seconds"].items()
    )
    return (
        '<section aria-labelledby="probe-result"><h2 id="probe-result" tabindex="-1">Last probe result</h2>'
        + detail
        + "<dl>"
        f"<dt>Profile</dt><dd><code>{_esc(result['profile_id'] or 'none')}</code></dd>"
        f"<dt>Chat</dt><dd><code>{_esc(result['chat_id'] or 'none')}</code></dd>"
        f"<dt>Returned</dt><dd>{_esc(count)}</dd>"
        f"<dt>Timeout</dt><dd>{_esc(result['timeout_seconds'])} s</dd>"
        f"<dt>Logical requests</dt><dd>connect {_esc(result['logical_requests']['connect'])}, "
        f"read {_esc(result['logical_requests']['read'])}</dd>"
        f"<dt>Bounds</dt><dd>{_esc(result['max_messages'])} message, "
        f"{_esc(result['max_history_requests'])} history request</dd>"
        f"<dt>Timings</dt><dd>{timings}</dd></dl></section>"
    )


def probe_html(result: dict[str, Any], data: dict[str, Any], csrf: str) -> str:
    return profiles_html(data, csrf, result=result)


def tools_html(
    data: dict[str, Any],
    *,
    query: str,
    profile: dict[str, Any] | None,
    profiles: list[str],
    csrf: str,
) -> str:
    needle = query.casefold().strip()
    rows = []
    for tool in data["tools"]:
        if needle and needle not in (tool["name"] + " " + tool["summary"]).casefold():
            continue
        badges = [
            f'<span class="badge" data-on="{str(tool["exposed"]).lower()}">'
            f"{'exposed' if tool['exposed'] else 'hidden'}</span>"
        ]
        for flag, label in (
            ("needs_profile", "profile"),
            ("needs_chat", "chat permission"),
            ("writes", "writes"),
            ("external", "external"),
        ):
            if tool[flag]:
                badges.append(f'<span class="badge">{label}</span>')
        note = applicable(profile, tool)
        rows.append(
            f"<tr><td><code>{_esc(tool['name'])}</code></td>"
            f"<td>{''.join(badges)}</td>"
            f"<td>{_esc(tool['summary'])}"
            + (f'<br><span class="small">{_esc(note)}</span>' if note else "")
            + "</td></tr>"
        )
    table = (
        '<table><caption class="small">Availability, chat permission and backend notes '
        "are shown separately. Hidden by the owner&#x27;s tool exposure means the tool is absent from MCP."
        "</caption><thead><tr><th>Tool</th><th>Availability</th>"
        "<th>What it needs</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        if rows
        else '<p class="state" data-state="empty" role="status">No tool matches this search. '
        "Clear the search to see the catalogue.</p>"
    )
    options = '<option value="">Any profile</option>' + "".join(
        f'<option value="{_esc(name)}"{" selected" if profile and profile["id"] == name else ""}>'
        f"{_esc(name)}</option>"
        for name in profiles
    )
    body = (
        f'<p class="small">Owner exposure: <code>{_esc(data["mode"])}</code> · '
        f"{_esc(data['exposed'])} of {_esc(data['count'])} tools exposed"
        + (
            " · selected: " + _esc(", ".join(data["selected_tools"]))
            if data["selected_tools"]
            else ""
        )
        + "</p>"
        '<form method="get" action="/ui/tools">'
        '<label for="tool-search">Search tools</label>'
        f'<input id="tool-search" name="q" value="{_esc(query)}" autocomplete="off">'
        '<label for="tool-profile">Explain limits for</label>'
        f'<select id="tool-profile" name="profile">{options}</select>'
        '<button type="submit">Filter</button></form>'
        + (
            f'<p class="small">Profile <code>{_esc(profile["id"])}</code>: '
            f"{_esc(profile['kind'])} {_esc(profile['backend'])}, state "
            f"<code>{_esc(profile['state'])}</code>, read policy "
            f"<code>{_esc(profile['read_policy']['mode'])}</code>.</p>"
            if profile
            else ""
        )
        + '<p class="small">A chat permission badge identifies a required chat argument. '
        "The profile's read policy and operation grants still apply when a tool is exposed.</p>"
        + table
    )
    return shell("Tool catalogue", body, csrf)
