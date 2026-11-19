"""Owner-only profile read policy and the separate send, broadcast and AI grants.

Only the exact numeric chat IDs an owner types are accepted: no dialog picker, no
folder, username or profile-name inference, and no temporary read widening to fill
a list. Apply reuses control's stop/write/restart/verification transition.
"""

import hmac
import html
import json
import re
from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import Settings
from .control import Change, identity_mismatches, read_config, revision, write_config
from .models import TeleloomError

MODES = ("all", "selected")
GRANTS = {"send": "send_chats", "broadcast": "broadcast_chats", "ai": "jev_chats"}
_ID = re.compile(r"-?[1-9][0-9]{0,18}")


def _ids(tokens: list[str] | str) -> list[str]:
    """Exact decimal chat IDs only; leading zeros, signs other than '-' and text are refused."""
    if isinstance(tokens, str):
        tokens = [tokens]
    found: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if not _ID.fullmatch(token):
            raise TeleloomError(
                "invalid_chat_id",
                f"'{token[:24]}' is not an exact numeric chat ID. Paste the number Telegram "
                "shows (a leading - for groups and channels), one per line; nothing was written.",
            )
        found.append(token)
    return sorted(dict.fromkeys(found), key=int)


def chat_id(value: str) -> str:
    """Exactly one decimal chat ID the owner typed; nothing is inferred from a name."""
    found = _ids([value])
    if len(found) != 1:
        raise TeleloomError("invalid_chat_id", "Type exactly one numeric chat ID.")
    return found[0]


def current(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    profile = (data.get("profiles") or {}).get(name) or {}
    return {
        "read": {
            "mode": profile.get("read_mode", "all"),
            "chat_ids": list(profile.get("read_chats") or []),
        },
        **{grant: list(profile.get(field) or []) for grant, field in GRANTS.items()},
    }


def validated(
    data: Mapping[str, Any], name: str, mode: str, tokens: Mapping[str, Any]
) -> dict[str, Any]:
    """Profile must exist exactly; empty selected read list means no readable chats."""
    profiles = data.get("profiles") or {}
    if not isinstance(name, str) or name not in profiles:
        raise TeleloomError(
            "profile_not_found", "Choose one of the configured profiles by its exact name."
        )
    if mode not in MODES:
        raise TeleloomError("invalid_read_mode", "Choose read mode all or selected.")

    def field(key: str) -> list[str]:
        return _ids(re.split(r"[\s,]+", " ".join(tokens.get(key) or [])))

    read = field("read") if mode == "selected" else []
    grants = {grant: field(grant) for grant in GRANTS}
    if mode == "selected":
        old = current(data, name)
        for grant, chats in grants.items():
            outside = [chat for chat in chats if chat not in read and chat not in old[grant]]
            if outside:
                raise TeleloomError(
                    "grant_not_readable",
                    f"The {grant} grant names {outside[0]}, which this profile cannot read. "
                    "Add it to the selected chats first; nothing was written.",
                )
    return {
        "read": {"mode": mode, "chat_ids": read},
        **grants,
    }


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    return [
        f"profiles.{key}: {json.dumps(before.get(key), ensure_ascii=False)} -> "
        f"{json.dumps(after.get(key), ensure_ascii=False)}"
        for key in ("read", *GRANTS)
        if before.get(key) != after.get(key)
    ]


def write(settings: Settings, name: str, proposed: Mapping[str, Any]) -> None:
    """Replace five keys of one profile; every other grant, root and unknown field survives."""
    _, data = read_config(settings)
    profile = (data.get("profiles") or {}).get(name)
    if not isinstance(profile, dict):
        raise TeleloomError("profile_not_found", "The profile disappeared; nothing was written.")
    profile["read_mode"] = proposed["read"]["mode"]
    profile["read_chats"] = list(proposed["read"]["chat_ids"])
    for grant, field in GRANTS.items():
        profile[field] = list(proposed[grant])
    write_config(settings, data)


def mismatches(
    observed: dict[str, Any], settings: Settings, name: str, proposed: Mapping[str, Any]
) -> list[str]:
    """What the restarted owner reports for this profile versus the reviewed policy."""
    found = identity_mismatches(observed, settings)
    profiles = (observed.get("scope") or {}).get("profiles") or []
    reported = next((item for item in profiles if item.get("id") == name), None)
    if reported is None:
        return found + [f"the owner does not report profile {name}"]
    grants = reported.get("grants") or {}
    expected = dict(proposed)
    if proposed["read"]["mode"] == "selected":
        for grant in GRANTS:
            expected[grant] = [
                chat for chat in proposed[grant] if chat in proposed["read"]["chat_ids"]
            ]
    return found + [
        f"profile {name} reports {key}={json.dumps(grants.get(key), ensure_ascii=False)} "
        f"instead of {json.dumps(value, ensure_ascii=False)}"
        for key, value in expected.items()
        if grants.get(key) != value
    ]


def change(settings: Settings, name: str, proposed: dict[str, Any]) -> Change:
    return Change(
        proposed=proposed,
        current=lambda data: current(data, name),
        write=lambda: write(settings, name, proposed),
        verify=lambda observed: mismatches(observed, settings, name, proposed),
        describe=lambda observed: (
            f"Applied and verified: profile {name} reads "
            + (
                "every chat."
                if proposed["read"]["mode"] == "all"
                else f"only {proposed['read']['chat_ids']}."
            )
            + " send="
            + json.dumps(proposed["send"])
            + " broadcast="
            + json.dumps(proposed["broadcast"])
            + " ai="
            + json.dumps(proposed["ai"])
            + f". Owner {observed.get('owner_id')} reports this policy. "
            "Reconnect every MCP client; revoked chats now refuse readers and job results."
        ),
    )


async def page(instance: Any, request: Request) -> Response:
    if not instance._allowed(request, mutation=False):
        return JSONResponse({"error": "forbidden_origin"}, status_code=403)
    found = instance._session(request)
    if found is None:
        return instance._locked(request)
    return _page(instance, request, found[1].csrf)


async def apply(instance: Any, request: Request) -> Response:
    if not instance._allowed(request, mutation=True):
        return JSONResponse({"error": "forbidden_origin"}, status_code=403)
    found = instance._session(request)
    if found is None:
        return instance._locked(request)
    form = await instance._form(request)
    if isinstance(form, Response):
        return form
    csrf = found[1].csrf
    if not hmac.compare_digest((form.get("csrf") or [""])[0].encode(), csrf.encode()):
        return JSONResponse({"error": "invalid_csrf"}, status_code=403)
    raw, data = read_config(instance.settings)
    digest = (form.get("revision") or [""])[0]
    name = (form.get("profile") or [""])[0]
    if digest != revision(raw):
        return _page(
            instance,
            request,
            csrf,
            name=name,
            message="This editor is stale. Review the current policy again.",
            status=409,
        )
    try:
        proposed = validated(
            data,
            name,
            (form.get("mode") or [""])[0],
            {key: list(form.get(key) or []) for key in ("read", *GRANTS)},
        )
    except TeleloomError as error:
        return _page(
            instance,
            request,
            csrf,
            name=name,
            message=f"{error.code}: {error.message}",
            status=422,
        )
    reviewed = _review_key(found[0], digest, name, proposed)
    if form.get("review") == ["1"]:
        return _page(instance, request, csrf, name=name, proposal=proposed, reviewed=reviewed)
    if not hmac.compare_digest((form.get("reviewed") or [""])[0].encode(), reviewed.encode()):
        return _page(
            instance,
            request,
            csrf,
            name=name,
            message="Review the exact diff before applying.",
            status=409,
        )
    return await instance._transition(digest, change(instance.settings, name, proposed), csrf)


def _review_key(session_key: str, digest: str, name: str, proposed: dict[str, Any]) -> str:
    payload = json.dumps([digest, name, proposed], sort_keys=True).encode()
    return hmac.digest(session_key.encode(), payload, "sha256").hex()


def _page(
    instance: Any,
    request: Request,
    csrf: str,
    *,
    name: str | None = None,
    message: str | None = None,
    status: int = 200,
    proposal: dict[str, Any] | None = None,
    reviewed: str = "",
) -> Response:
    raw, data = read_config(instance.settings)
    profiles = sorted((data.get("profiles") or {}).keys())
    name = name or request.query_params.get("profile") or (profiles[0] if profiles else "")
    if name not in profiles:
        name = ""
    before = current(data, name)
    after = proposal if proposal is not None else before
    changes = diff(before, after)
    return HTMLResponse(
        _html(
            instance,
            profiles,
            name=name,
            before=before,
            after=after,
            changes=changes,
            reviewed=reviewed,
            digest=revision(raw),
            csrf=csrf,
            message=message,
        ),
        status_code=status,
        headers=instance._headers(),
    )


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _box(title: str, key: str, values: list[str], hint: str) -> str:
    return (
        f"<fieldset><legend>{_esc(title)}</legend>"
        f'<p class="small">{_esc(hint)}</p>'
        f'<textarea name="{key}" rows="4" spellcheck="false" '
        f'aria-label="{_esc(title)}">{_esc(chr(10).join(values))}</textarea></fieldset>'
    )


def _html(
    instance: Any,
    profiles: list[str],
    *,
    name: str,
    before: dict[str, Any],
    after: dict[str, Any],
    changes: list[str],
    reviewed: str,
    digest: str,
    csrf: str,
    message: str | None,
) -> str:
    options = "".join(
        f'<option value="{_esc(item)}"{" selected" if item == name else ""}>{_esc(item)}</option>'
        for item in profiles
    )
    modes = "".join(
        f'<label><input type="radio" name="mode" value="{mode}"'
        f"{' checked' if mode == after['read']['mode'] else ''}> {mode}</label>"
        for mode in MODES
    )
    selected = (
        ""
        if not name
        else (
            f'<input type="hidden" name="profile" value="{_esc(name)}">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            f'<input type="hidden" name="revision" value="{_esc(digest)}">'
            f"<p>Editing <strong>{_esc(name)}</strong></p>"
            "<fieldset><legend>Read policy</legend><p>"
            + modes
            + "</p></fieldset>"
            + _box(
                "Selected chats",
                "read",
                after["read"]["chat_ids"] if after["read"]["mode"] == "selected" else [],
                "Exact numeric chat IDs, one per line. selected with an empty list allows no chat.",
            )
            + _box(
                "Send grant",
                "send",
                after["send"],
                "Chats this profile may send to. Separate list.",
            )
            + _box(
                "Broadcast grant",
                "broadcast",
                after["broadcast"],
                "Chats this profile may broadcast to; it never implies send or AI.",
            )
            + _box(
                "AI grant", "ai", after["ai"], "Chats whose content may be sent to the AI provider."
            )
            + '<input type="hidden" name="review" value="1">'
            '<p><button type="submit">Review exact diff</button></p>'
        )
    )
    applied = "".join(
        f'<input type="hidden" name="{key}" value="{_esc(value)}">'
        for key in ("read", "send", "broadcast", "ai")
        for value in (after["read"]["chat_ids"] if key == "read" else after[key])
    )
    review = (
        '<section aria-labelledby="diff"><h2 id="diff">Exact diff</h2><ul>'
        + (
            "".join(f"<li><code>{_esc(line)}</code></li>" for line in changes)
            or ("<li>No difference from the current policy.</li>")
        )
        + "</ul>"
        '<form method="post" action="/control/policy/apply">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        f'<input type="hidden" name="revision" value="{_esc(digest)}">'
        f'<input type="hidden" name="profile" value="{_esc(name)}">'
        f'<input type="hidden" name="mode" value="{_esc(after["read"]["mode"])}">'
        f'<input type="hidden" name="reviewed" value="{reviewed}">'
        + applied
        + '<p><button type="submit"'
        + ("" if changes else " disabled")
        + ">Apply and restart the owner</button> "
        + ("" if changes else "Nothing to apply: this exact policy is active.")
        + "</p></form></section>"
        if reviewed
        else ""
    )
    result = (
        f'<section aria-labelledby="result"><h2 id="result">Result</h2>'
        f'<p role="status">{_esc(message)}</p></section>'
        if message
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Profile policy · teleloom</title><style>
:root{{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:6vh 24px}}main{{max-width:820px;margin:auto}}
h1{{font-size:32px;letter-spacing:-.03em}}h2{{font-size:18px;margin-top:2em}}
p,li{{line-height:1.6}}code{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;border-radius:4px}}
.small{{font-size:13px;color:#68665d}}textarea{{width:100%;font:14px ui-monospace,monospace;padding:8px}}
button,select{{font:inherit;padding:10px 16px;border-radius:6px;border:1px solid #b8b4a8;background:white;cursor:pointer}}
button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{{outline:3px solid #24231f;outline-offset:2px}}
fieldset{{border:1px solid #d8d3c7;margin-top:1.5em}}legend{{font-weight:600}}
</style></head><body><main><p class="small">teleloom / owner management · this process is not the owner</p>
<h1>Profile read policy and grants</h1>
{result}
<p class="small">Configuration revision <code>{_esc(digest)}</code> · <a href="/control">Tool exposure</a></p>
<form method="get" action="/control/policy">{f'<label>Profile <select name="profile">{options}</select></label> <button>Load profile</button>' if profiles else ""}</form>
<form method="post" action="/control/policy/apply">{selected or ('<p role="status">Choose one of the configured profiles.</p>' if profiles else '<p role="status">No profiles are configured. Run teleloom auth user or auth bot first.</p>')}</form>
{review}
<p class="small">Apply stops the owner, writes only read_mode, read_chats, send_chats,
broadcast_chats and jev_chats of the chosen profile, then verifies the restarted owner's own
report. Grants are separate decisions: one never enables another, and a scheduled
owner-authorized send is not a grant. A saved grant outside selected chats remains inactive.
Active jobs and unknown deliveries refuse the change.</p>
</main></body></html>"""
