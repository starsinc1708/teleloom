"""Owner-only settings for the AI and transcription engines teleloom already supports.

Discovery is local only: package presence, a complete existing model directory, an
endpoint and a stored credential. It never calls a provider, downloads a model or
installs a package, and an installed package is never reported as provider health.
Credentials use the existing OS-keyring boundary; no plaintext fallback exists.
"""

import hmac
import html
import json
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .attachments import _installed, _local_model
from .config import Settings, TranscriptionConfig
from .control import Change, identity_mismatches, read_config, revision, write_config
from .models import TeleloomError
from .secrets import Secrets
from .transcription import external_endpoint

PROVIDERS = ("telegram", "local", "openai", "groq")
FIELDS = ("provider", "local_model_path", "openai_endpoint", "openai_model", "groq_model")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULTS = TranscriptionConfig()


def _effective(config: dict[str, Any]) -> dict[str, Any]:
    """Stored value or the model default the owner is effectively running with."""
    return {
        key: (getattr(DEFAULTS, key) if config.get(key) is None else config[key]) for key in FIELDS
    }


def _credential(credentials: Any, profile: str, name: str) -> bool:
    try:
        return bool(credentials.get(profile, name))
    except TeleloomError:
        # An unusable OS keyring is reported where it is used, never as a stored value.
        return False


def jev(data: dict[str, Any], credentials: Any) -> dict[str, Any]:
    installed = _installed("typesafe_sdk")
    model = data.get("jev_model") or Settings.model_fields["jev_model"].default
    credential = bool(os.getenv("TYPESAFE_API_KEY")) or _credential(
        credentials, "", "typesafe_api_key"
    )
    missing = []
    if not installed:
        missing.append("Install teleloom[jev] in the owner environment.")
    if not credential:
        missing.append("Store a Jev key here or set TYPESAFE_API_KEY in the owner environment.")
    return {
        "installed": installed,
        "configured": bool(model) and credential,
        "credential": credential,
        "available": installed and bool(model) and credential,
        "model": model,
        "missing": missing,
        "health": "not_checked",
    }


def availability(data: dict[str, Any], name: str, credentials: Any) -> dict[str, Any]:
    """What this exact stored profile can use, and what is missing for each provider."""
    profile = (data.get("profiles") or {}).get(name) or {}
    values = _effective(profile.get("transcription") or {})
    user = profile.get("kind") == "user"

    def external(provider: str, endpoint: str | None, model: str, field: str) -> dict[str, Any]:
        missing: list[str] = []
        try:
            external_endpoint(endpoint)
            configured = bool(endpoint)
        except TeleloomError as error:
            configured = False
            missing.append(error.message)
        if not model:
            configured = False
            missing.append(f"Choose the {provider} model name.")
        credential = _credential(credentials, name, field)
        if not credential:
            missing.append(
                f"No {provider} credential: store one here or set "
                f"TELELOOM_{name.upper()}_{field.upper()} in the owner environment."
            )
        return {
            "installed": True,
            "configured": configured,
            "credential": credential,
            "available": configured and credential,
            "external": True,
            "model": model,
            "missing": missing,
        }

    installed = _installed("faster_whisper")
    local_model = _local_model(values["local_model_path"])
    local_missing = []
    if not installed:
        local_missing.append("Install teleloom[transcription] in the owner environment.")
    if local_model is None:
        local_missing.append(
            "Choose an existing complete local model directory (model.bin, config.json, "
            "tokenizer.json); nothing is downloaded."
        )
    return {
        "telegram": {
            "installed": True,
            "configured": user,
            "credential": None,
            "available": user,
            "external": False,
            "model": "telegram" if user else None,
            "missing": []
            if user
            else [
                "Telegram audio transcription is a user-account capability; Premium, trial "
                "and group boosts still apply."
            ],
        },
        "local": {
            "installed": installed,
            "configured": local_model is not None,
            "credential": None,
            "available": installed and local_model is not None,
            "external": False,
            "model": "configured local model" if local_model is not None else None,
            "missing": local_missing,
        },
        "openai": external(
            "openai", values["openai_endpoint"], values["openai_model"], "openai_transcription_key"
        ),
        "groq": external("groq", GROQ_ENDPOINT, values["groq_model"], "groq_transcription_key"),
    }


def current(data: dict[str, Any], name: str) -> dict[str, Any]:
    profile = (data.get("profiles") or {}).get(name) or {}
    return {
        "jev_model": data.get("jev_model") or Settings.model_fields["jev_model"].default,
        "transcription": _effective(profile.get("transcription") or {}),
    }


def validated(data: dict[str, Any], name: str, tokens: dict[str, Any]) -> dict[str, Any]:
    """Only existing profiles and the supported model/endpoint inputs; nothing is created."""
    if not isinstance(name, str) or name not in (data.get("profiles") or {}):
        raise TeleloomError(
            "profile_not_found", "Choose one of the configured profiles by its exact name."
        )
    stored = data["profiles"][name].get("transcription") or {}
    provider = str(tokens.get("provider") or _effective(stored)["provider"]).strip()
    if provider not in PROVIDERS:
        raise TeleloomError(
            "invalid_provider", "Choose one of the existing transcription providers."
        )
    jev_model = str(tokens.get("jev_model") or "").strip()
    if not 1 <= len(jev_model) <= 200:
        raise TeleloomError(
            "invalid_model", "The Jev model must be a non-empty name of at most 200 characters."
        )
    local = str(tokens.get("local_model_path") or "").strip()
    if local and _local_model(local) is None:
        raise TeleloomError(
            "engine_unavailable",
            "Supply an absolute complete existing local model directory; nothing is installed "
            "or downloaded.",
        )
    endpoint = str(tokens.get("openai_endpoint") or "").strip()
    if endpoint:
        external_endpoint(endpoint)  # the same HTTPS/loopback rules the engine will apply
    models: dict[str, str] = {}
    for key in ("openai_model", "groq_model"):
        value = str(tokens.get(key) or "").strip()
        if not 1 <= len(value) <= 200:
            raise TeleloomError(
                "invalid_model", f"The {key.replace('_', ' ')} must be 1 to 200 characters."
            )
        models[key] = value
    return {
        "jev_model": jev_model,
        "transcription": {
            "provider": provider,
            "local_model_path": local or None,
            "openai_endpoint": endpoint or None,
            **models,
        },
    }


def diff(before: dict[str, Any], after: dict[str, Any], name: str) -> list[str]:
    lines = []
    if before["jev_model"] != after["jev_model"]:
        lines.append(f"jev_model: {before['jev_model']} -> {after['jev_model']}")
    for key in FIELDS:
        old, new = before["transcription"][key], after["transcription"][key]
        if old != new:
            lines.append(
                f"profiles.{name}.transcription.{key}: {json.dumps(old, ensure_ascii=False)} -> "
                f"{json.dumps(new, ensure_ascii=False)}"
            )
    return lines


def credentials_of(name: str, tokens: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Only provided values reach the store; keys are never part of the reviewed diff."""
    offered = (
        (("", "typesafe_api_key"), tokens.get("jev_key")),
        ((name, "openai_transcription_key"), tokens.get("openai_key")),
        ((name, "groq_transcription_key"), tokens.get("groq_key")),
    )
    found = []
    for (profile, field), value in offered:
        if not value:
            continue
        if len(value) > 512 or any(character.isspace() for character in value):
            raise TeleloomError(
                "invalid_credential",
                "A credential is one token of at most 512 characters; nothing was written.",
            )
        found.append((profile, field, value))
    return found


def write(
    settings: Settings,
    name: str,
    proposed: dict[str, Any],
    keys: list[tuple[str, str, str]],
    credentials: Any,
) -> None:
    """Replace only jev_model and five transcription fields; every grant and key survives.

    Credentials are written first and restored if the atomic config write refuses, so one
    Apply never leaves a rotated key behind an unchanged configuration.
    """
    _, data = read_config(settings)
    profile = (data.get("profiles") or {}).get(name)
    if not isinstance(profile, dict):
        raise TeleloomError("profile_not_found", "The profile disappeared; nothing was written.")
    stored = profile.get("transcription")
    config = stored if isinstance(stored, dict) else profile.setdefault("transcription", {})
    unchanged = current(data, name) == proposed
    previous = {
        (profile_id, field): credentials.get(profile_id, field) for profile_id, field, _ in keys
    }
    applied: list[tuple[str, str]] = []
    try:
        for profile_id, field, value in keys:
            credentials.set(profile_id, field, value)  # OS keyring or an explicit refusal
            applied.append((profile_id, field))
        if unchanged:
            return  # a key-only rotation leaves every durable configuration field untouched
        data["jev_model"] = proposed["jev_model"]
        for key, value in proposed["transcription"].items():
            config[key] = value
        write_config(settings, data)
    except BaseException:
        for key in applied:
            value = previous[key]
            if value is None:
                credentials.delete(*key)
            else:
                credentials.set(*key, value)
        raise


def change(
    settings: Settings,
    name: str,
    proposed: dict[str, Any],
    keys: list[tuple[str, str, str]],
    credentials: Any,
) -> Change:
    def verify(observed: dict[str, Any]) -> list[str]:
        found = identity_mismatches(observed, settings)
        _, data = read_config(settings)
        if current(data, name) != proposed:
            found.append("the stored engine configuration differs from the review")
        profiles = (observed.get("scope") or {}).get("profiles") or []
        profile: dict[str, Any] = next((item for item in profiles if item.get("id") == name), {})
        engines = (profile.get("capabilities") or {}).get("transcription_engines") or {}
        if engines.get("default_provider") != proposed["transcription"]["provider"]:
            found.append("the owner did not report the reviewed transcription provider")
        return found

    return Change(
        proposed=proposed,
        current=lambda data: current(data, name),
        write=lambda: write(settings, name, proposed, keys, credentials),
        rotate=bool(keys),
        verify=verify,
        describe=lambda observed: (
            f"Applied and verified: engine settings for profile {name} are durable and owner "
            f"{observed.get('owner_id')} answers in this workspace with build "
            f"{str((observed.get('build') or {}).get('build_id'))[:12]}. "
            + (
                "The credential is stored in the OS keyring and is not shown again. "
                if keys
                else ""
            )
            + "No provider call, model download or install was made, and provider health and "
            "credential validity remain unchecked; transcription_capabilities in the running "
            "owner reports the same local availability."
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
    _, data = read_config(instance.settings)
    name = (form.get("profile") or [""])[0]
    try:
        proposed = validated(
            data, name, {key: (form.get(key) or [""])[0] for key in ("jev_model", *FIELDS)}
        )
        keys = credentials_of(
            name, {key: (form.get(key) or [""])[0] for key in ("jev_key", "openai_key", "groq_key")}
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
    return await instance._transition(
        (form.get("revision") or [""])[0],
        change(instance.settings, name, proposed, keys, Secrets()),
        csrf,
    )


def _page(
    instance: Any,
    request: Request,
    csrf: str,
    *,
    name: str | None = None,
    message: str | None = None,
    status: int = 200,
) -> Response:
    raw, data = read_config(instance.settings)
    profiles = sorted((data.get("profiles") or {}).keys())
    name = name or request.query_params.get("profile") or (profiles[0] if profiles else "")
    if name not in profiles:
        name = ""
    reviewed = bool(name) and request.query_params.get("review") == "1"
    before = current(data, name)
    after = before
    changes: list[str] = []
    if reviewed:
        try:
            after = validated(
                data, name, {key: request.query_params.get(key) for key in ("jev_model", *FIELDS)}
            )
            changes = diff(before, after, name)
        except TeleloomError as error:
            reviewed = False
            message = message or f"{error.code}: {error.message}"
            status = 422
    return HTMLResponse(
        _html(
            instance,
            data,
            profiles,
            name=name,
            before=before,
            after=after,
            changes=changes,
            reviewed=reviewed,
            digest=revision(raw),
            csrf=csrf,
            message=message,
            credentials=Secrets(),
        ),
        status_code=status,
        headers=instance._headers(),
    )


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _input(label: str, key: str, value: Any, hint: str = "") -> str:
    return (
        f'<label>{_esc(label)}<br><input name="{key}" value="{_esc(value or "")}" '
        f'spellcheck="false"></label>' + (f'<p class="small">{_esc(hint)}</p>' if hint else "")
    )


def _provider(value: str) -> str:
    options = "".join(
        f'<option value="{_esc(item)}"{" selected" if item == value else ""}>{_esc(item)}</option>'
        for item in PROVIDERS
    )
    return (
        f'<label>Transcription provider<br><select name="provider">{options}</select></label>'
        '<p class="small">Stored default for new jobs; an explicit provider on a call still '
        "wins. local means no external upload and no fallback.</p>"
    )


def _secret(label: str, key: str, configured: bool, env: str) -> str:
    return (
        f'<label>{_esc(label)}<br><input type="password" name="{key}" value="" '
        'autocomplete="new-password" spellcheck="false" placeholder="'
        + ("replace the stored key" if configured else "not configured")
        + f'"></label><p class="small">OS keyring only; paste to replace, or set '
        f"<code>{_esc(env)}</code>. The value is never shown again.</p>"
    )


def _state(label: str, record: dict[str, Any]) -> str:
    def yes(value: Any) -> str:
        return "yes" if value else "no"

    missing = "".join(f"<li>{_esc(item)}</li>" for item in record["missing"])
    model = f" · model <code>{_esc(record['model'])}</code>" if record.get("model") else ""
    return (
        f"<dt>{_esc(label)}</dt><dd>installed {yes(record['installed'])} · configured "
        f"{yes(record['configured'])} · available {yes(record['available'])}{model}"
        f"{' · external upload' if record.get('external') else ''}"
        + (f'<ul class="small">{missing}</ul>' if missing else "")
        + "</dd>"
    )


def _html(
    instance: Any,
    data: dict[str, Any],
    profiles: list[str],
    *,
    name: str,
    before: dict[str, Any],
    after: dict[str, Any],
    changes: list[str],
    reviewed: bool,
    digest: str,
    csrf: str,
    message: str | None,
    credentials: Any,
) -> str:
    options = "".join(
        f'<option value="{_esc(item)}"{" selected" if item == name else ""}>{_esc(item)}</option>'
        for item in profiles
    )
    engine = jev(data, credentials)
    states = availability(data, name, credentials) if name else {}
    if name:
        report = (
            "<dl>"
            + _state("Jev", engine)
            + "".join(
                _state(f"transcription / {provider}", states[provider]) for provider in PROVIDERS
            )
            + "</dl>"
        )
    else:
        report = (
            '<p role="status">No profiles are configured. Run teleloom auth user or auth bot '
            "first.</p><dl>" + _state("Jev", engine) + "</dl>"
        )
    result = (
        f'<section aria-labelledby="result"><h2 id="result">Result</h2>'
        f'<p role="status">{_esc(message)}</p></section>'
        if message
        else ""
    )
    fields = (
        _provider(after["transcription"]["provider"])
        + _input("Jev model", "jev_model", after["jev_model"])
        + "".join(
            _input(
                key.replace("_", " ").capitalize(),
                key,
                after["transcription"][key],
                hint,
            )
            for key, hint in (
                (
                    "local_model_path",
                    "Existing absolute model directory; empty clears it. Never downloaded.",
                ),
                (
                    "openai_endpoint",
                    "HTTPS, or HTTP on loopback only. Empty means OpenAI is unavailable.",
                ),
                ("openai_model", "Model name sent to that endpoint."),
                ("groq_model", "Model name sent to the fixed Groq endpoint."),
            )
        )
    )
    form = (
        '<form method="get" action="/control/engines">'
        '<label>Profile <select name="profile">' + options + "</select></label>"
        ' <button type="submit">Load profile</button></form>'
        if profiles
        else ""
    )
    chooser = (
        form + '<form method="get" action="/control/engines">'
        f'<input type="hidden" name="profile" value="{_esc(name)}">'
        '<fieldset><legend>Existing engines</legend><p class="small">Only the supported inputs '
        "above are edited. AI, transcription and external-upload grants are separate and this "
        "page never changes them.</p>" + fields + '<input type="hidden" name="review" value="1">'
        '<p><button type="submit">Review exact diff</button></p></fieldset></form>'
        if name
        else form
    )
    applied = "".join(
        f'<input type="hidden" name="{key}" value="{_esc(after["transcription"][key] or "")}">'
        for key in FIELDS
    )
    review = (
        '<section aria-labelledby="diff"><h2 id="diff">Exact diff</h2><ul>'
        + (
            "".join(f"<li><code>{_esc(line)}</code></li>" for line in changes)
            or "<li>No difference from the stored engine settings.</li>"
        )
        + "</ul>"
        '<form method="post" action="/control/engines/apply">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        f'<input type="hidden" name="revision" value="{_esc(digest)}">'
        f'<input type="hidden" name="profile" value="{_esc(name)}">'
        f'<input type="hidden" name="jev_model" value="{_esc(after["jev_model"])}">'
        + applied
        + _secret(
            "Jev key",
            "jev_key",
            bool(engine["credential"]),
            "TYPESAFE_API_KEY",
        )
        + _secret(
            "OpenAI-compatible key",
            "openai_key",
            bool(states.get("openai", {}).get("credential")),
            f"TELELOOM_{name.upper()}_OPENAI_TRANSCRIPTION_KEY",
        )
        + _secret(
            "Groq key",
            "groq_key",
            bool(states.get("groq", {}).get("credential")),
            f"TELELOOM_{name.upper()}_GROQ_TRANSCRIPTION_KEY",
        )
        + '<p class="small">Keys are written to the OS keyring and are not part of the diff and '
        "are never displayed. Entering one with no configuration difference rotates only that "
        "key in this same Apply.</p>"
        + '<p><button type="submit">Apply and restart the owner</button> '
        + (
            "No configuration difference: a key you enter is rotated in this same Apply."
            if not changes
            else ""
        )
        + "</p></form></section>"
        if reviewed
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI and transcription engines · teleloom</title><style>
:root{{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:6vh 24px}}main{{max-width:820px;margin:auto}}
h1{{font-size:32px;letter-spacing:-.03em}}h2{{font-size:18px;margin-top:2em}}
p,li,dd{{line-height:1.6}}code{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;border-radius:4px}}
.small{{font-size:13px;color:#68665d}}dt{{font-weight:600;margin-top:.6em}}
input{{font:inherit;padding:8px;min-width:24em;max-width:100%}}
button,select{{font:inherit;padding:10px 16px;border-radius:6px;border:1px solid #b8b4a8;background:white;cursor:pointer}}
button:focus-visible,input:focus-visible,select:focus-visible{{outline:3px solid #24231f;outline-offset:2px}}
fieldset{{border:1px solid #d8d3c7;margin-top:1.5em}}legend{{font-weight:600}}
</style></head><body><main><p class="small">teleloom / owner management · this process is not the owner</p>
<h1>AI and transcription engines</h1>
{result}
<p class="small">Configuration revision <code>{_esc(digest)}</code> · <a href="/control">Tool exposure</a> · <a href="/control/policy">Profile policy</a></p>
<section aria-labelledby="engines"><h2 id="engines">Installed, configured, available</h2>
<p class="small">Local discovery only: package presence, an existing model directory, an endpoint and a stored credential. No provider is called, no model is downloaded and no package is installed, so “installed” is not live provider health. External transcription additionally needs each chat in the separate external-upload grant, and model changes follow the existing job/cache identity rules: work already started is completed with the engine it began with.</p>
{report}</section>
{chooser}
{review}
</main></body></html>"""
