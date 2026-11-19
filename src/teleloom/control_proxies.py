"""Owner-only named proxy catalogue and explicit profile assignment.

Config holds protocol/host/port and the assignment name; the username/password
stay in a separate named-endpoint namespace inside Secrets. Apply
reuses control's stop/write/restart/verification transition. A profile with no
assignment keeps its legacy per-profile secret; an assignment that is missing or
invalid refuses a direct connection instead of silently going around the proxy.
"""

import asyncio
import hmac
import html
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .adapters import named_proxy_credentials
from .config import ProxyEndpoint, Settings, proxy_uri_host, validate_proxy_name
from .control import Change, identity_mismatches, read_config, revision, write_config
from .models import TeleloomError
from .secrets import NAMED_PROXY, Secrets

PROTOCOLS = ("socks5", "socks4", "http")
DEFAULT_ROUTE = "profile default"


def _profiles(data: Mapping[str, Any]) -> dict[str, Any]:
    profiles = data.get("profiles") or {}
    return profiles if isinstance(profiles, dict) else {}


def current(data: Mapping[str, Any], secrets: Secrets | None = None) -> dict[str, Any]:
    """Comparable projection: route identity and assignment names, never a credential."""
    proxies = {
        name: {
            "protocol": item.get("protocol"),
            "host": item.get("host"),
            "port": item.get("port"),
            **({"legacy_credentials": False} if item.get("legacy_credentials") is False else {}),
        }
        for name, item in sorted((data.get("proxies") or {}).items())
        if isinstance(item, dict)
    }
    credentials: dict[str, bool | None] = {}
    for name, item in proxies.items():
        try:
            credentials[name] = bool(
                secrets
                and secrets.named_proxy(name, legacy=item.get("legacy_credentials") is not False)
            )
        except TeleloomError:
            credentials[name] = None
    return {
        "proxies": proxies,
        "assignments": {
            name: (profile or {}).get("proxy") for name, profile in _profiles(data).items()
        },
        "credentials": credentials,
    }


def affected(data: Mapping[str, Any], name: str) -> list[str]:
    return sorted(
        profile for profile, item in _profiles(data).items() if (item or {}).get("proxy") == name
    )


def _endpoint(protocol: Any, host: str, port: str) -> dict[str, Any]:
    try:
        value = ProxyEndpoint(protocol=protocol, host=host.strip(), port=int(port))
    except (ValidationError, ValueError):
        raise TeleloomError(
            "invalid_proxy",
            "Proxy needs socks5, socks4 or http with a bare hostname or IP address and a port from 1 to 65535; "
            "nothing was written.",
        ) from None
    return value.model_dump(exclude={"legacy_credentials"})


def validated(
    data: Mapping[str, Any],
    *,
    action: str,
    form: Mapping[str, list[str]],
    secrets: Secrets | None = None,
    credential: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    """Build the complete proposed catalogue plus one explicit assignment."""
    proposed = current(data, secrets)
    profiles = _profiles(data)
    if action == "endpoint":
        raw_name = (form.get("name") or [""])[0]
        try:
            name = validate_proxy_name(raw_name)
        except ValueError as error:
            raise TeleloomError("invalid_proxy_name", str(error)) from None
        previous = proposed["proxies"].get(name)
        endpoint = _endpoint(
            (form.get("protocol") or [""])[0],
            (form.get("host") or [""])[0],
            (form.get("port") or [""])[0],
        )
        if (
            previous is None
            or credential is not None
            or previous.get("legacy_credentials") is False
        ):
            endpoint["legacy_credentials"] = False
        proposed["proxies"][name] = endpoint
        proposed["credentials"].setdefault(name, False)
        if credential is not None:
            proposed["credentials"][name] = True
    elif action == "assign":
        profile = (form.get("profile") or [""])[0]
        choice = (form.get("choice") or [""])[0]
        if profile not in profiles:
            raise TeleloomError("profile_not_found", "Choose one configured profile by name.")
        if choice != "" and choice not in proposed["proxies"] and choice != "direct":
            raise TeleloomError(
                "proxy_unavailable",
                f"Unknown named proxy '{choice[:64]}'; create it first. Nothing was written.",
            )
        proposed["assignments"][profile] = choice if choice in proposed["proxies"] else None
    elif action != "migrate":
        raise TeleloomError("invalid_action", "Choose endpoint, assign or migrate.")
    for profile, choice in proposed["assignments"].items():
        if choice is not None and choice not in proposed["proxies"]:
            raise TeleloomError(
                "proxy_unavailable",
                f"Profile {profile} is assigned to missing proxy '{choice}'; repair it explicitly.",
            )
    return proposed


def migrate(
    data: Mapping[str, Any], secrets: Secrets
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """One endpoint per legacy per-profile secret; the legacy secret is untouched."""
    proposed = current(data, secrets)
    copied: list[tuple[str, str, str]] = []
    for profile in sorted(_profiles(data)):
        if proposed["assignments"].get(profile) is not None:
            continue
        legacy = secrets.get(profile, "proxy")
        if not legacy:
            continue
        try:
            parsed = urlparse(legacy)
            if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
                raise ValueError("Legacy route has unsupported URI components")
            if parsed.password and (not parsed.username or parsed.scheme == "socks4"):
                raise ValueError("Legacy route has unsupported credentials")
            name = f"legacy_{profile}"[:64]
            validate_proxy_name(name)
            endpoint = _endpoint(parsed.scheme, parsed.hostname or "", str(parsed.port or ""))
        except (ValueError, TeleloomError):
            raise TeleloomError(
                "invalid_proxy",
                f"Legacy proxy for profile {profile} is invalid; nothing was migrated or written.",
            ) from None
        if name in proposed["proxies"] or secrets.named_proxy(name, legacy=True) is not None:
            raise TeleloomError(
                "proxy_exists", f"The name {name} is already in use. Rename it before migration."
            )
        endpoint["legacy_credentials"] = False
        proposed["proxies"][name] = endpoint
        proposed["assignments"][profile] = name
        proposed["credentials"][name] = True
        copied.append((name, unquote(parsed.username or ""), unquote(parsed.password or "")))
    return proposed, copied


def _credential_status(value: bool | None) -> str:
    return "needs repair" if value is None else "stored" if value else "absent"


def diff(before: Mapping[str, Any], after: Mapping[str, Any], data: Mapping[str, Any]) -> list[str]:
    lines = []
    for name in sorted(set(before["proxies"]) | set(after["proxies"])):
        old, new = before["proxies"].get(name), after["proxies"].get(name)
        if old != new:
            profiles = affected(data, name)
            lines.append(
                f"proxies.{name}: {json.dumps(old, ensure_ascii=False)} -> "
                f"{json.dumps(new, ensure_ascii=False)}"
                + (f" (affects profiles: {', '.join(profiles)})" if profiles else "")
            )
    for name in sorted(set(before["assignments"]) | set(after["assignments"])):
        old, new = before["assignments"].get(name), after["assignments"].get(name)
        if old != new:
            lines.append(f"profiles.{name}.proxy: {old or DEFAULT_ROUTE} -> {new or DEFAULT_ROUTE}")
    for name in sorted(set(before["credentials"]) | set(after["credentials"])):
        if before["credentials"].get(name) != after["credentials"].get(name):
            lines.append(
                f"proxies.{name}.credentials: {_credential_status(after['credentials'].get(name))}"
            )
    return lines


def write(
    settings: Settings,
    proposed: Mapping[str, Any],
    copied: list[tuple[str, str, str]],
) -> None:
    secrets = Secrets()
    _, data = read_config(settings)
    if proposed["proxies"]:
        data["proxies"] = {name: dict(item) for name, item in sorted(proposed["proxies"].items())}
    else:
        data.pop("proxies", None)
    for name, profile in _profiles(data).items():
        choice = proposed["assignments"].get(name)
        if choice is None:
            profile.pop("proxy", None)
        else:
            profile["proxy"] = choice
    previous = {name: secrets.get_keyring(NAMED_PROXY, name) for name, _, _ in copied}
    applied = []
    try:
        for name, username, password in copied:
            secrets.set(NAMED_PROXY, name, json.dumps({"username": username, "password": password}))
            applied.append(name)
        write_config(settings, data)
    except BaseException:
        for name in applied:
            if (value := previous[name]) is None:
                secrets.delete(NAMED_PROXY, name)
            else:
                secrets.set(NAMED_PROXY, name, value)
        raise


def mismatches(
    observed: dict[str, Any], settings: Settings, proposed: Mapping[str, Any]
) -> list[str]:
    found = identity_mismatches(observed, settings)
    _, data = read_config(settings)
    now = current(data)
    if now["proxies"] != proposed["proxies"] or now["assignments"] != proposed["assignments"]:
        found.append("config.json does not report the reviewed proxy catalogue and assignments")
    rows = (observed.get("scope") or {}).get("profiles") or []
    for name, choice in proposed["assignments"].items():
        reported = next((item for item in rows if item.get("id") == name), None)
        if reported is None or (reported.get("proxy") or {}).get("name") != (
            choice or DEFAULT_ROUTE
        ):
            found.append(f"profile {name} did not report the reviewed route")
    return found


def change(
    settings: Settings,
    proposed: dict[str, Any],
    copied: list[tuple[str, str, str]] | None = None,
) -> Change:
    def describe(observed: dict[str, Any]) -> str:
        routes = ", ".join(
            f"{name}={choice or DEFAULT_ROUTE}"
            for name, choice in sorted(proposed["assignments"].items())
        )
        return (
            f"Applied and verified: {len(proposed['proxies'])} named proxy endpoint(s); "
            f"{routes}. Owner {observed.get('owner_id')} reports these assignments. "
            "No direct fallback is enabled; reconnect every MCP client."
        )

    return Change(
        proposed=proposed,
        current=lambda data: current(data, Secrets()),
        rotate=bool(copied),
        write=lambda: write(settings, proposed, copied or []),
        verify=lambda observed: mismatches(observed, settings, proposed),
        describe=describe,
    )


async def probe(settings: Settings, name: str, timeout_seconds: float = 5.0) -> dict[str, Any]:
    """One explicitly requested TCP reachability check with a hard deadline.

    shortcut: it proves the local proxy listener answers, not that the upstream
    route works; upgrade after ordered fallback/health work.
    """
    timeout_seconds = min(max(timeout_seconds, 0.05), 10.0)
    endpoint = settings.proxies.get(name)
    if endpoint is None:
        return {"proxy": name, "status": "unavailable", "direct_fallback": False}
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(endpoint.host, endpoint.port), timeout_seconds
        )
    except TimeoutError:
        return {"proxy": name, "status": "timeout", "direct_fallback": False}
    except OSError:
        return {"proxy": name, "status": "unavailable", "direct_fallback": False}
    writer.close()
    await writer.wait_closed()
    return {"proxy": name, "status": "reachable", "direct_fallback": False}


async def page(instance: Any, request: Request, *, message: str | None = None) -> Response:
    if not instance._allowed(request, mutation=False):
        return JSONResponse({"error": "forbidden_origin"}, status_code=403)
    found = instance._session(request)
    if found is None:
        return instance._locked(request)
    raw, data = read_config(instance.settings)
    secrets = Secrets()
    before = current(data, secrets)
    action = request.query_params.get("action", "")
    reviewed = request.query_params.get("review") == "1" and action in {
        "endpoint",
        "assign",
        "migrate",
    }
    after, changes = before, []
    subject = "***"
    status = 200
    if reviewed:
        form = {
            key: request.query_params.getlist(key)
            for key in ("name", "protocol", "host", "port", "profile", "choice")
        }
        subject = (form.get("name") or form.get("profile") or [""])[0]
        try:
            if action == "migrate":
                after, _ = migrate(data, secrets)
            else:
                after = validated(data, action=action, form=form, secrets=secrets)
            changes = diff(before, after, data)
        except TeleloomError as error:
            reviewed = False
            status = 422
            message = f"{error.code}: {error.message}"
    return HTMLResponse(
        _html(
            instance,
            data,
            before=before,
            after=after,
            changes=changes,
            reviewed=reviewed,
            action=action,
            subject=subject,
            digest=revision(raw),
            csrf=found[1].csrf,
            message=message,
        ),
        status_code=status,
        headers=instance._headers(),
    )


async def check(instance: Any, request: Request) -> Response:
    if not instance._allowed(request, mutation=True):
        return JSONResponse({"error": "forbidden_origin"}, status_code=403)
    found = instance._session(request)
    if found is None:
        return instance._locked(request)
    form = await instance._form(request)
    if isinstance(form, Response):
        return form
    if not hmac.compare_digest((form.get("csrf") or [""])[0].encode(), found[1].csrf.encode()):
        return JSONResponse({"error": "invalid_csrf"}, status_code=403)
    settings = Settings.load(instance.settings.data_dir)
    result = await probe(settings, (form.get("name") or [""])[0][:64])
    return await page(instance, request, message=f"Route check: {result['status']}")


async def apply(instance: Any, request: Request) -> Response:
    if not instance._allowed(request, mutation=True):
        return JSONResponse({"error": "forbidden_origin"}, status_code=403)
    found = instance._session(request)
    if found is None:
        return instance._locked(request)
    form = await instance._form(request)
    if isinstance(form, Response):
        return form
    if not hmac.compare_digest((form.get("csrf") or [""])[0].encode(), found[1].csrf.encode()):
        return JSONResponse({"error": "invalid_csrf"}, status_code=403)
    raw, data = read_config(instance.settings)
    secrets = Secrets()
    action = (form.get("action") or [""])[0]
    subject = (form.get("name") or form.get("profile") or [""])[0]
    username = (form.get("username") or [""])[0]
    password = (form.get("password") or [""])[0]
    copied: list[tuple[str, str, str]] = []
    try:
        if action == "migrate":
            proposed, copied = migrate(data, secrets)
        else:
            credential = None
            if username or password:
                protocol = (form.get("protocol") or [""])[0]
                if action != "endpoint" or not username or (protocol == "socks4" and password):
                    raise TeleloomError(
                        "invalid_proxy_credentials",
                        "Set credentials on the endpoint. Use a username, and omit the password for SOCKS4.",
                    )
                credential = (
                    (form.get("name") or [""])[0],
                    username,
                    password,
                )
                copied.append(credential)
            proposed = validated(
                data, action=action, form=form, secrets=secrets, credential=credential
            )
            checked = subject if action == "endpoint" else proposed["assignments"].get(subject)
            if checked and credential is None:
                endpoint = proposed["proxies"][checked]
                value = secrets.named_proxy(
                    checked, legacy=endpoint.get("legacy_credentials") is not False
                )
                named_proxy_credentials(value or "", endpoint["protocol"])
    except TeleloomError as error:
        return HTMLResponse(
            _html(
                instance,
                data,
                before=current(data, secrets),
                after=current(data, secrets),
                changes=[],
                reviewed=False,
                action=action,
                subject=subject,
                digest=revision(raw),
                csrf=found[1].csrf,
                message=f"{error.code}: {error.message}",
            ),
            status_code=422,
            headers=instance._headers(),
        )
    return await instance._transition(
        (form.get("revision") or [""])[0],
        change(instance.settings, proposed, copied),
        found[1].csrf,
    )


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _html(
    instance: Any,
    data: Mapping[str, Any],
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    changes: list[str],
    reviewed: bool,
    action: str,
    subject: str,
    digest: str,
    csrf: str,
    message: str | None,
) -> str:
    profiles = sorted(_profiles(data))
    rows = ""
    for name, item in before["proxies"].items():
        assigned = affected(data, name)
        rows += (
            f"<tr><td><strong>{_esc(name)}</strong></td>"
            f"<td>{_esc(item['protocol'])}://{_esc(proxy_uri_host(str(item['host'])))}:{_esc(item['port'])}</td>"
            f"<td>{_credential_status(before['credentials'].get(name))}</td>"
            f"<td>{_esc(', '.join(assigned) or '—')}</td>"
            '<td><form method="post" action="/control/proxies/check">'
            f'<input type="hidden" name="name" value="{_esc(name)}">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            '<button type="submit">Check route</button></form></td></tr>'
        )
    endpoint_form = (
        '<form method="get" action="/control/proxies">'
        '<input type="hidden" name="action" value="endpoint">'
        '<input type="hidden" name="review" value="1">'
        '<label>Name <input name="name" required pattern="[a-z][a-z0-9_-]*"></label> '
        '<label>Protocol <select name="protocol">'
        + "".join(f"<option>{_esc(item)}</option>" for item in PROTOCOLS)
        + "</select></label> "
        '<label>Host <input name="host" required></label> '
        '<label>Port <input name="port" type="number" min="1" max="65535" required></label> '
        '<button type="submit">Review endpoint</button></form>'
    )
    profile_form = (
        '<form method="get" action="/control/proxies">'
        '<input type="hidden" name="action" value="assign">'
        '<input type="hidden" name="review" value="1">'
        '<label>Profile <select name="profile">'
        + "".join(f"<option>{_esc(item)}</option>" for item in profiles)
        + '</select></label> <label>Route <select name="choice">'
        '<option value="">Use profile default</option>'
        + "".join(f"<option>{_esc(item)}</option>" for item in before["proxies"])
        + '</select></label> <button type="submit">Review assignment</button></form>'
    )
    hidden = ""
    if reviewed:
        if action == "endpoint" and subject in after["proxies"]:
            item = after["proxies"][subject]
            hidden = (
                f'<input type="hidden" name="name" value="{_esc(subject)}">'
                f'<input type="hidden" name="protocol" value="{_esc(item["protocol"])}">'
                f'<input type="hidden" name="host" value="{_esc(item["host"])}">'
                f'<input type="hidden" name="port" value="{_esc(item["port"])}">'
            )
        elif action == "assign" and subject in after["assignments"]:
            hidden = (
                f'<input type="hidden" name="profile" value="{_esc(subject)}">'
                f'<input type="hidden" name="choice" '
                f'value="{_esc(after["assignments"][subject] or "")}">'
            )
    apply_form = (
        '<form method="post" action="/control/proxies/apply">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        f'<input type="hidden" name="revision" value="{_esc(digest)}">'
        f'<input type="hidden" name="action" value="{_esc(action)}">'
        + hidden
        + (
            '<label>Username <input name="username" autocomplete="off"></label> '
            '<label>Password <input name="password" type="password" '
            'autocomplete="new-password"></label> '
            if action == "endpoint"
            else ""
        )
        + '<button type="submit"'
        + ("" if changes or action == "endpoint" else " disabled")
        + ">Apply and restart the owner</button></form>"
        if reviewed
        else ""
    )
    review = ""
    if reviewed:
        review = (
            "<section><h2>Exact diff</h2><ul>"
            + (
                "".join(f"<li><code>{_esc(line)}</code></li>" for line in changes)
                or "<li>No difference from the current catalogue.</li>"
            )
            + "</ul>"
            + (
                "<p>Profiles using this endpoint: "
                + _esc(", ".join(affected(data, subject)) or "none")
                + ". Enter credentials below to replace the stored pair.</p>"
                if action == "endpoint"
                else ""
            )
            + apply_form
            + '<p class="small">Editing an endpoint changes every listed profile. Delete is not offered: '
            "an assigned endpoint must be replaced by an explicit assignment first.</p></section>"
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Proxies · teleloom</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f;margin:0;padding:6vh 24px}}
main{{max-width:900px;margin:auto}}table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{text-align:left;padding:8px;border-bottom:1px solid #ddd}}code{{overflow-wrap:anywhere}}
input,select,button{{font:inherit;padding:8px;margin:4px 0}}button{{cursor:pointer}}</style></head>
<body><main><p class="small">teleloom / owner management · this process is not the owner</p>
<h1>Named proxies</h1>
<p role="status">{_esc(message or "")}</p>
<table><thead><tr><th>Name</th><th>Endpoint</th><th>Credentials</th><th>Profiles</th><th>Check</th></tr></thead>
<tbody>{rows or '<tr><td colspan="5">No named proxies.</td></tr>'}</tbody></table>
<h2>Add or edit endpoint</h2>{endpoint_form}
<h2>Assign one profile</h2>{profile_form}
<p><a href="/control/proxies?action=migrate&amp;review=1">Review migration of legacy per-profile proxies</a></p>
{review}
<p class="small">Configuration revision <code>{_esc(digest)}</code> · <a href="/control">Tool exposure</a>
· Passwords and proxy URIs with credentials never appear here. A route check is the only network action.</p>
</main></body></html>"""
