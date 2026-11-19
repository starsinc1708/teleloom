"""Client connection review/install and bundled runtime skills for owner management (#27).

Only OpenCode 2 (`opencode`) and Hermes are written, through their own native CLI and
inside an owner-only `control` browser session. Every other client gets an explicit
export fragment. Skill writes touch only the selected bundled `teleloom-*` folders.
"""

import hashlib
import html
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .models import TeleloomError

SKILL_HOMES = {
    "codex": ".agents/skills",
    "claude": ".claude/skills",
    "opencode": ".config/opencode/skills",
    "opencode-v1": ".config/opencode/skills",
    "hermes": None,
    "pi": ".pi/agent/skills",
}
AUTO_CLIENTS = frozenset({"opencode", "hermes"})
CLIENTS = tuple(SKILL_HOMES)


def connection_entry(settings: Any) -> dict[str, Any]:
    """The installed interpreter, data directory and windowless Windows host."""
    command = sys.executable
    if sys.platform == "win32":
        windowless = Path(command).with_name("pythonw.exe")
        if windowless.is_file():
            command = str(windowless)
    return {
        "command": command,
        "args": ["-m", "teleloom", "mcp"],
        "env": {"TELELOOM_DATA_DIR": str(settings.data_dir)},
    }


def native_add_args(client: str, entry: dict[str, Any]) -> list[str]:
    """Exact CLI arguments after the executable; always executed without a shell."""
    assignment = "TELELOOM_DATA_DIR=" + entry["env"]["TELELOOM_DATA_DIR"]
    args = ["mcp", "add", "teleloom"]
    if client == "opencode":
        return [*args, "--global", "--env", assignment, "--", entry["command"], *entry["args"]]
    return [*args, "--command", entry["command"], "--env", assignment, "--args", *entry["args"]]


def bundled_skills() -> list[Path]:
    bundled = Path(__file__).parent / "bundled_skills"
    source = bundled if bundled.exists() else Path(__file__).resolve().parents[2] / "skills"
    skills = sorted(
        folder for folder in source.glob("teleloom-*") if (folder / "SKILL.md").is_file()
    )
    if len(skills) != 6:
        raise TeleloomError(
            "package_incomplete", "Six bundled skills were not found; reinstall the package."
        )
    return skills


def skill_destination(
    client: str | None, target: Path | None, hermes_path: Path | None = None
) -> Path:
    if target is not None:
        return target.expanduser().resolve()
    if client == "hermes":
        if hermes_path is None:
            # The client resolves HERMES_HOME and the active profile; never guess its home.
            from .cli import hermes_config_path

            hermes_path = hermes_config_path()
        return (hermes_path.parent / "skills").resolve()
    home = SKILL_HOMES[client or "codex"]
    assert home is not None  # Hermes was handled above; every other client has a home
    return (Path.home() / home).expanduser().resolve()


def _selected(names: list[str] | None) -> list[Path]:
    skills = bundled_skills()
    if names is None:
        return skills
    wanted = set(names)
    known = {folder.name for folder in skills}
    if unknown := sorted(wanted - known):
        raise TeleloomError(
            "unknown_skills",
            "Unknown skills: " + ", ".join(unknown) + ".",
            details={"skills": unknown},
        )
    return [folder for folder in skills if folder.name in wanted]


def _skill_output(destination: Path, folder: Path) -> Path:
    output = destination / folder.name
    if output.resolve() != output or (output.exists() and not output.is_dir()):
        raise TeleloomError("invalid_target", "Choose a regular skill directory without redirects.")
    for item in folder.rglob("*"):
        if item.is_file():
            target = output / item.relative_to(folder)
            if target.resolve() != target or (target.exists() and not target.is_file()):
                raise TeleloomError(
                    "invalid_target", "A supplied skill file has a conflicting target."
                )
    return output


def skill_plan(destination: Path, names: list[str] | None = None) -> list[dict[str, Any]]:
    """What a write would do per selected skill, without touching the destination."""
    plans = []
    for folder in _selected(names):
        output = _skill_output(destination, folder)
        files = sorted(
            item.relative_to(folder).as_posix() for item in folder.rglob("*") if item.is_file()
        )
        if not output.exists():
            action, changed = "install", files
        else:
            changed = [
                name
                for name in files
                if not (output / name).is_file()
                or (output / name).read_bytes() != (folder / name).read_bytes()
            ]
            action = "identical" if not changed else "conflict"
        plans.append({"name": folder.name, "action": action, "files": changed})
    return plans


def install_bundled(
    destination: Path, names: list[str] | None = None, force: bool = False
) -> dict[str, Any]:
    skills = _selected(names)
    if not skills:
        return {"installed": [], "destination": str(destination)}
    conflicts = [folder.name for folder in skills if (destination / folder.name).exists()]
    if conflicts and not force:
        raise TeleloomError(
            "skills_exist",
            "Existing skills were preserved. Use --force to replace supplied files.",
            details={"skills": conflicts},
        )
    destination.mkdir(parents=True, exist_ok=True)
    for folder in skills:
        output = _skill_output(destination, folder)
        try:
            shutil.copytree(folder, output, dirs_exist_ok=force)
        except OSError:
            raise TeleloomError(
                "skills_write_failed", "Could not copy all selected skill files. Check permissions."
            ) from None
    return {"installed": [folder.name for folder in skills], "destination": str(destination)}


def export_fragment(client: str, entry: dict[str, Any]) -> str:
    """Mergeable fragment for clients this ticket does not write automatically."""
    if client == "codex":
        return (
            "[mcp_servers.teleloom]\ncommand = "
            + json.dumps(entry["command"])
            + "\nargs = "
            + json.dumps(entry["args"])
            + "\n[mcp_servers.teleloom.env]\nTELELOOM_DATA_DIR = "
            + json.dumps(entry["env"]["TELELOOM_DATA_DIR"])
        )
    if client in {"opencode", "opencode-v1"}:
        local = {
            "type": "local",
            "command": [entry["command"], *entry["args"]],
            "environment": entry["env"],
        }
        if client == "opencode-v1":
            local["enabled"] = True
        servers = {"teleloom": local}
        shape = {"servers": servers} if client == "opencode" else servers
        return json.dumps({"mcp": shape}, indent=2)
    key = "mcpServers" if client in {"claude", "pi"} else "mcp_servers"
    return json.dumps({key: {"teleloom": entry}}, indent=2)


def _client_paths(client: str) -> list[Path]:
    """The files the native CLI resolved for this client, never a guessed home."""
    from . import cli

    try:
        if client == "hermes":
            return [cli.hermes_config_path()]
        documents = json.loads(cli.client_cli(cli.Client(client), ["debug", "config"]))
        return [
            Path(item["path"])
            for item in documents
            if isinstance(item, dict)
            and item.get("type") == "document"
            and isinstance(item.get("path"), str)
        ]
    except (TeleloomError, ValueError, KeyError, TypeError, AttributeError):
        return []


def _entry_diff(client: str, entry: dict[str, Any], saved: Any) -> dict[str, Any]:
    """Compare the existing entry by field; foreign values stay behind their key names."""
    if not isinstance(saved, dict):
        return {
            "state": "different",
            "command": "unreadable",
            "args": "unreadable",
            "environment": {},
            "preserved": [],
            "blocking": ["entry is not a mapping"],
        }
    env_key = "environment" if client == "opencode" else "env"
    command = saved.get("command")
    if client == "opencode":
        current = command[0] if isinstance(command, list) and command else None
        current_args = command[1:] if isinstance(command, list) else None
    else:
        current, current_args = command, saved.get("args")
    blocking: list[str] = []
    if client == "opencode" and saved.get("type") != "local":
        blocking.append("connection type is not local")
    if current != entry["command"]:
        blocking.append("command differs")
    if current_args != entry["args"]:
        blocking.append("args differ")
    env = saved.get(env_key)
    if not isinstance(env, dict):
        blocking.append(f"{env_key} is not a mapping")
        env = {}
    environment = {}
    for key in sorted(set(env) | set(entry["env"])):
        if key not in entry["env"]:
            environment[key] = "extra"
        elif env.get(key) != entry["env"][key]:
            environment[key] = "different" if key in env else "missing"
        else:
            environment[key] = "identical"
    blocking.extend(
        f"{env_key}.{key} {state}"
        for key, state in environment.items()
        if state in {"different", "missing", "extra"}
    )
    if "url" in saved:
        blocking.append("remote url entry")
    if saved.get("enabled") is False or saved.get("disabled") is True:
        blocking.append("disabled entry")
    return {
        "state": "different" if blocking else "identical",
        "command": "identical" if current == entry["command"] else "different",
        "args": "identical" if current_args == entry["args"] else "different",
        "environment": environment,
        "preserved": sorted(set(saved) - {"command", "args", env_key, "type"}),
        "blocking": blocking,
    }


def _existing(client: str, entry: dict[str, Any]) -> dict[str, Any]:
    from . import cli

    try:
        entries = cli.client_entries(cli.Client(client))
    except TeleloomError as error:
        return {
            "state": "unavailable",
            "code": error.code,
            "detail": error.message,
            "paths": [],
            "diff": None,
            "fingerprint": "unavailable:" + error.code,
        }
    paths = _client_paths(client)
    fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True, default=str).encode())
    for path in paths:
        fingerprint.update(str(path).encode())
        try:
            fingerprint.update(path.read_bytes())
        except OSError:
            fingerprint.update(b"<unreadable>")
    diffs = [_entry_diff(client, entry, saved) for saved in entries]
    existing_diff = next((item for item in diffs if item["blocking"]), diffs[0] if diffs else None)
    public: dict[str, Any] = {
        "state": "present" if entries else "absent",
        "paths": [str(path) for path in paths],
        "diff": existing_diff,
        "fingerprint": fingerprint.hexdigest(),
    }
    if entries:
        public["detail"] = (
            "An identical entry is kept as is; a different one is refused and preserved."
        )
    return public


def _skill_fingerprint(destination: Path, names: list[str] | None) -> str:
    digest = hashlib.sha256()
    for folder in _selected(names):
        output = _skill_output(destination, folder)
        for item in sorted(folder.rglob("*")):
            if item.is_file():
                relative = item.relative_to(folder)
                digest.update(folder.name.encode() + b"/" + relative.as_posix().encode())
                digest.update(item.read_bytes())
                installed = output / relative
                if installed.is_file():
                    digest.update(b"installed/" + installed.read_bytes())
    return digest.hexdigest()


def _review_hash(
    client: str, destination: Path, names: list[str] | None, connection: dict[str, Any]
) -> str:
    """Bind a review to the client files and selected skill files it described."""
    scope = json.dumps(
        [client, str(destination), sorted(names) if names is not None else "all"],
        sort_keys=True,
    )
    digest = hashlib.sha256(scope.encode() + _skill_fingerprint(destination, names).encode())
    digest.update(str(connection["fingerprint"]).encode())
    return digest.hexdigest()


def _prepare(
    settings: Any, client: str, target: Path | None, names: list[str] | None
) -> dict[str, Any]:
    if client not in CLIENTS:
        raise TeleloomError("unknown_client", "Choose a supported client.")
    entry = connection_entry(settings)
    destination = skill_destination(client, target)
    review: dict[str, Any] = {
        "client": client,
        "install_supported": client in AUTO_CLIENTS,
        "entry": entry,
        "destination": str(destination),
        "skills": skill_plan(destination, names),
    }
    review["conflicts"] = [
        item["name"] for item in review["skills"] if item["action"] == "conflict"
    ]
    if client in AUTO_CLIENTS:
        review["native_args"] = [client, *native_add_args(client, entry)]
        review["connection"] = _existing(client, entry)
        review["config_paths"] = review["connection"]["paths"]
        review["review_hash"] = _review_hash(client, destination, names, review["connection"])
    else:
        review["export"] = export_fragment(client, entry)
    return review


def review(
    settings: Any,
    client: str,
    target: Path | None = None,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """Read-only: exact client, resolved paths, supplied-file changes and conflicts."""
    return _prepare(settings, client, target, names)


def install(
    settings: Any,
    client: str,
    target: Path | None = None,
    names: list[str] | None = None,
    overwrite: bool = False,
    reviewed: str | None = None,
) -> dict[str, Any]:
    """Save the native connection, then copy only the selected bundled skills."""
    prepared = _prepare(settings, client, target, names)
    if not prepared["install_supported"]:
        raise TeleloomError(
            "client_install_unsupported",
            "Automatic install supports only OpenCode 2 (opencode) and Hermes; use the export.",
        )
    if reviewed is None:
        raise TeleloomError("review_required", "Review the exact changes before installing.")
    if reviewed != prepared["review_hash"]:
        raise TeleloomError(
            "review_stale",
            "The client configuration or selected skill files changed after that review;"
            " review the exact changes again.",
        )
    if prepared["conflicts"] and not overwrite:
        raise TeleloomError(
            "skills_exist",
            "Existing skills were preserved. Review the differences and choose overwrite.",
            details={"skills": prepared["conflicts"]},
        )
    from . import cli

    status: dict[str, str] = cli.install_client(cli.Client(client), prepared["entry"])
    pending = [item["name"] for item in prepared["skills"] if item["action"] != "identical"]
    destination = Path(prepared["destination"])
    try:
        installed = install_bundled(destination, pending, force=overwrite)
    except TeleloomError as error:
        raise TeleloomError(
            error.code,
            error.message
            + " The client connection is already saved; reconnect it and retry the skills.",
            details=error.details,
        ) from None
    return {
        "client": client,
        "connection": status["status"],
        "connection_saved": status["status"] == "installed",
        "destination": installed["destination"],
        "installed": installed["installed"],
        "already_present": [
            item["name"] for item in prepared["skills"] if item["action"] == "identical"
        ],
        "reconnect": "Reconnect the client so it reloads MCP discovery.",
        "discovery": "unverified: this page did not observe a model workflow.",
    }


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def page_html(
    *,
    csrf: str,
    client: str,
    target: str,
    names: list[str] | None,
    skills: list[str],
    review: dict[str, Any] | None = None,
    message: str | None = None,
    reviewed: bool = False,
) -> str:
    options = "".join(
        f'<option value="{_esc(name)}"{" selected" if name == client else ""}>{_esc(name)}</option>'
        for name in CLIENTS
    )
    checked = set(names) if names is not None else set(skills)
    boxes = "".join(
        f'<li><label><input type="checkbox" name="skills" value="{_esc(name)}"'
        f"{' checked' if name in checked else ''}> <code>{_esc(name)}</code></label></li>"
        for name in skills
    )
    result = (
        f'<section aria-labelledby="result"><h2 id="result">Result</h2>'
        f'<p role="status">{_esc(message)}</p></section>'
        if message
        else ""
    )
    detail = ""
    if review:
        rows = [
            f"<dt>Client</dt><dd><code>{_esc(review['client'])}</code>"
            f" ({'native install' if review['install_supported'] else 'export only'})</dd>",
            f"<dt>Command</dt><dd><code>{_esc(review['entry']['command'])}</code></dd>",
            f"<dt>Arguments</dt><dd><code>{_esc(' '.join(review['entry']['args']))}</code></dd>",
            f"<dt>Data directory</dt>"
            f"<dd><code>{_esc(review['entry']['env']['TELELOOM_DATA_DIR'])}</code></dd>",
            f"<dt>Skills destination</dt><dd><code>{_esc(review['destination'])}</code></dd>",
        ]
        if "config_paths" in review:
            paths = (
                "".join(f"<code>{_esc(path)}</code><br>" for path in review["config_paths"])
                or "<em>not resolved by the client CLI</em>"
            )
            rows.append(f"<dt>Client config</dt><dd>{paths}</dd>")
        if "native_args" in review:
            rows.append(
                "<dt>Native invocation</dt>"
                f"<dd><code>{_esc(json.dumps(review['native_args']))}</code> (list argv, no shell)</dd>"
            )
            state = review["connection"]
            rows.append(
                f"<dt>Existing connection</dt><dd>{_esc(state['state'])}"
                + (f" · {_esc(state.get('detail', ''))}" if state.get("detail") else "")
                + "</dd>"
            )
        actions = "".join(
            f"<li><code>{_esc(item['name'])}</code>: {_esc(item['action'])}"
            + (f" · {_esc(', '.join(item['files']))}" if item.get("files") else "")
            + "</li>"
            for item in review["skills"]
        )
        detail = (
            '<section aria-labelledby="changes"><h2 id="changes">Exact changes</h2>'
            f'<dl class="meta">{"".join(rows)}</dl>'
            f"<h3>Selected skills</h3><ul>{actions or '<li>No skills selected.</li>'}</ul>"
        )
        connection = review.get("connection")
        diff = connection.get("diff") if connection else None
        if connection is not None:
            detail += "<h3>Entry diff</h3>"
        if connection is not None and diff is None:
            detail += (
                "<p>No teleloom entry exists; the native CLI adds the one above.</p>"
                if connection.get("state") == "absent"
                else '<p role="alert">The existing teleloom entry could not be read; saving fails closed.</p>'
            )
        elif connection is not None:
            assert diff is not None  # guarded above
            diff_rows = [
                f"<dt>command</dt><dd><code>{_esc(review['entry']['command'])}</code>"
                f" · existing {_esc(diff['command'])}</dd>",
                f"<dt>args</dt><dd><code>{_esc(' '.join(review['entry']['args']))}</code>"
                f" · existing {_esc(diff['args'])}</dd>",
            ]
            for key, state in diff["environment"].items():
                proposed = review["entry"]["env"].get(key)
                shown = (
                    f"<code>{_esc(proposed)}</code>" if proposed is not None else "(value hidden)"
                )
                diff_rows.append(
                    f"<dt>environment.{_esc(key)}</dt><dd>{shown} · existing {_esc(state)}</dd>"
                )
            detail += f'<dl class="meta">{"".join(diff_rows)}</dl>'
            if diff["preserved"]:
                detail += (
                    '<p class="small">Preserved existing settings: '
                    + _esc(", ".join(diff["preserved"]))
                    + ".</p>"
                )
            if diff["blocking"]:
                detail += (
                    '<p role="alert">A different teleloom entry is preserved and saving is'
                    " refused until it matches: " + _esc(", ".join(diff["blocking"])) + ".</p>"
                )
        if review.get("conflicts"):
            detail += (
                '<p role="alert">Existing supplied skills differ: '
                + _esc(", ".join(review["conflicts"]))
                + ".</p>"
            )
        if "export" in review:
            detail += (
                "<h3>Export</h3><pre><code>"
                + _esc(review["export"])
                + '</code></pre><p class="small">This client is never written by this page.</p>'
            )
        if reviewed and review["install_supported"]:
            hidden = "".join(
                f'<input type="hidden" name="skills" value="{_esc(name)}">' for name in checked
            )
            detail += (
                '<form method="post" action="/control/clients/apply">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                f'<input type="hidden" name="client" value="{_esc(client)}">'
                f'<input type="hidden" name="target" value="{_esc(target)}">'
                f'<input type="hidden" name="review_hash" value="{_esc(review["review_hash"])}">'
                + hidden
                + "<p>"
                + (
                    '<label><input type="checkbox" name="overwrite" value="1"> '
                    "Replace only the supplied files of the selected existing skills</label>"
                    if review["conflicts"]
                    else ""
                )
                + ' <button type="submit">Save client config and install selected skills</button></p>'
                "</form>"
            )
        elif not review["install_supported"]:
            detail += '<p class="small">Copy this fragment into the client; nothing is written.</p>'
        else:
            detail += (
                f'<form method="get" action="/control/clients">'
                f'<input type="hidden" name="review" value="1">'
                f'<input type="hidden" name="client" value="{_esc(client)}">'
                f'<input type="hidden" name="target" value="{_esc(target)}">'
                + "".join(
                    f'<input type="hidden" name="skills" value="{_esc(name)}">' for name in checked
                )
                + '<p><button type="submit">Review exact changes</button></p></form>'
            )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Client connection · teleloom</title><style>
:root{{font-family:system-ui,sans-serif;background:#f4f1eb;color:#24231f}}
body{{margin:0;padding:6vh 24px}}main{{max-width:820px;margin:auto}}
h1{{font-size:32px;letter-spacing:-.03em}}h2{{font-size:18px;margin-top:2em}}
p,li{{line-height:1.6}}code{{overflow-wrap:anywhere;background:#e8e4da;padding:.1em .4em;
border-radius:4px}}pre{{overflow-x:auto;background:#e8e4da;padding:12px;border-radius:6px}}
.small{{font-size:13px;color:#68665d}}.meta{{display:grid;grid-template-columns:180px 1fr;
gap:8px 20px}}button{{font:inherit;padding:10px 16px;border-radius:6px;border:1px solid #b8b4a8;
background:white;cursor:pointer}}fieldset{{border:1px solid #d8d3c7;margin-top:1.5em}}
</style></head><body><main>
<p class="small">teleloom / owner management · client files only, the owner keeps running</p>
<h1>Connect an MCP client</h1>
{result}
<form method="get" action="/control/clients"><fieldset><legend>Choose client and skills</legend>
<p><label>Client <select name="client">{options}</select></label></p>
<p><label>Skills target (blank uses the client's own resolution)
<input type="text" name="target" size="60" value="{_esc(target)}"
placeholder="not set — resolved from the client"></label></p>
<ul>{boxes}</ul><input type="hidden" name="review" value="1">
<p><button type="submit">Review exact changes</button></p></fieldset></form>
{detail}
<p class="small">Only OpenCode 2 and Hermes are written, through their native CLI with an
argument list and no shell. Existing skills and foreign client entries are preserved;
supplied files are replaced only after an explicit overwrite. A saved config is not a
reconnected client and never proves MCP discovery.</p>
</main></body></html>"""
