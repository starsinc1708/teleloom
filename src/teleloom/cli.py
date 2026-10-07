import asyncio
import functools
import json
import logging
import ntpath
import os
import secrets
import shutil
import subprocess
import sys
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from filelock import Timeout
from telethon.sessions import StringSession

from . import __version__
from .adapters import user_client
from .auth import code_login, identity, qr_login
from .config import Limits, Profile, Settings, validate_name
from .daemon import bridge, ownership, session, stop_daemon
from .daemon import serve as run_daemon
from .digest import digest_validate_command
from .evaluation import benchmark_command, evaluate_command
from .models import ErrorInfo, Result, TeleloomError, iso
from .runtime import number
from .secrets import Secrets
from .store import Store

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Local Telegram MCP, skills and QR login.",
)
auth_app = typer.Typer(help="Owner-only authentication; secrets never enter MCP.")
profile_app = typer.Typer(
    help="Configure explicit profile permissions while the daemon is stopped."
)
skills_app = typer.Typer(
    help="Install portable teleloom skills without overwriting existing files."
)
config_app = typer.Typer(help="Print client configuration or explicitly install supported clients.")
cache_app = typer.Typer(help="Explicit local index maintenance.")
app.add_typer(auth_app, name="auth")
app.add_typer(profile_app, name="profile")
app.add_typer(skills_app, name="skills")
app.add_typer(config_app, name="config")
app.add_typer(cache_app, name="cache")


class UI(StrEnum):
    browser = "browser"
    terminal = "terminal"


class DoctorMode(StrEnum):
    local = "local"
    mcp = "mcp"


class Method(StrEnum):
    qr = "qr"
    code = "code"


class BotBackend(StrEnum):
    bot_api = "bot_api"
    mtproto = "mtproto"


class Permission(StrEnum):
    read = "read"
    send = "send"
    broadcast = "broadcast"
    sync = "sync"
    jev = "jev"
    event = "event"
    transcription = "transcription"
    transcription_external = "transcription_external"


class ManagementScope(StrEnum):
    contacts = "contacts"
    folders = "folders"
    groups = "groups"
    account = "account"


class Client(StrEnum):
    codex = "codex"
    claude = "claude"
    opencode = "opencode"
    opencode_v1 = "opencode-v1"
    hermes = "hermes"
    pi = "pi"


def guarded(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Timeout:
            error = TeleloomError(
                "owner_busy", "Stop the active daemon/auth flow before changing configuration."
            )
        except TeleloomError as exc:
            error = exc
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise typer.Exit(130) from None
        except typer.Exit:
            raise
        except Exception:
            error = TeleloomError(
                "operation_failed",
                "Operation failed. Check configuration, credentials and connection; secrets were omitted.",
            )
        typer.echo(
            Result(
                ok=False,
                error=ErrorInfo(
                    code=error.code,
                    message=error.message,
                    retryable=error.retry_after is not None,
                    retry_after=error.retry_after,
                    details=error.details,
                ),
            ).model_dump_json(),
            err=True,
        )
        raise typer.Exit(1) from None

    return wrapped


app.command("evaluate")(guarded(evaluate_command))
app.command("benchmark")(guarded(benchmark_command))
app.command("digest-validate")(guarded(digest_validate_command))


@app.callback()
def root() -> None:
    logging.basicConfig(level=logging.CRITICAL)


@app.command()
@guarded
def init(port: int | None = typer.Option(None, min=1024, max=65535)) -> None:
    """Create private local configuration and an OS-stored MCP token."""
    settings = Settings.load()
    with ownership(settings):
        if port:
            settings.port = port
        credentials = Secrets()
        if not credentials.get("", "mcp_token"):
            credentials.set("", "mcp_token", secrets.token_urlsafe(48))
        settings.save()
    typer.echo(
        json.dumps(
            {"initialized": True, "data_dir": str(settings.data_dir), "version": __version__}
        )
    )


@auth_app.command("user")
@guarded
def auth_user(
    profile: str = typer.Option(...),
    ui: UI = UI.browser,
    method: Method = Method.qr,
    api_id: int | None = None,
    replace: bool = False,
) -> None:
    """Connect by browser/terminal QR, with a private cloud-password prompt if required."""
    validate_name(profile)
    settings = Settings.load()
    with ownership(settings):
        credentials = Secrets()
        existing = settings.profiles.get(profile)
        if existing and not replace:
            typer.echo(
                json.dumps(
                    {
                        "profile_id": profile,
                        "identity": existing.identity,
                        "already_configured": True,
                        "message": "Use --replace for an explicit new login.",
                    }
                )
            )
            return
        raw_api_id = str(api_id) if api_id else credentials.get(profile, "api_id")
        if not raw_api_id:
            raw_api_id = typer.prompt("Telegram API ID", type=str)
        config = Profile(kind="user", api_id=int(raw_api_id))
        if not credentials.get(profile, "api_hash"):
            value = typer.prompt("Telegram API hash", hide_input=True)
            credentials.set(profile, "api_hash", value)
            value = ""

        async def connect() -> dict[str, str]:
            client = user_client(profile, config, credentials, new=True)
            try:
                await client.connect()
                result = (
                    await qr_login(client, ui=ui.value)
                    if method == Method.qr
                    else await code_login(client)
                )
                if not await client.is_user_authorized():
                    raise TeleloomError("auth_failed", "Telegram did not confirm the session.")
                credentials.set(profile, "session", StringSession.save(client.session))
                return result
            finally:
                await client.disconnect()

        config.identity = asyncio.run(connect())
        settings.profiles[profile] = config
        settings.save()
        typer.echo(
            json.dumps({"profile_id": profile, "identity": config.identity, "connected": True})
        )


@auth_app.command("bot")
@guarded
def auth_bot(
    profile: str = typer.Option(...),
    replace: bool = False,
    backend: BotBackend = BotBackend.bot_api,
    api_id: int | None = typer.Option(None, min=1),
) -> None:
    """Validate a bot token supplied by environment, OS credentials, or a hidden prompt."""
    validate_name(profile)
    settings = Settings.load()
    with ownership(settings):
        existing = settings.profiles.get(profile)
        if existing and not replace:
            typer.echo(
                json.dumps(
                    {
                        "profile_id": profile,
                        "identity": existing.identity,
                        "already_configured": True,
                    }
                )
            )
            return
        credentials = Secrets()
        existing_token = credentials.get(profile, "bot_token")
        provided = bool(existing_token)
        token: str = existing_token or typer.prompt("Bot token", hide_input=True)
        config = Profile(kind="bot", bot_backend=backend.value, api_id=api_id)
        if backend == BotBackend.mtproto:
            raw_api_id = str(api_id) if api_id else credentials.get(profile, "api_id")
            config.api_id = int(raw_api_id or typer.prompt("Telegram API ID", type=str))
            if config.api_id <= 0:
                raise TeleloomError("credentials_invalid", "Telegram API ID must be positive.")
            if not credentials.get(profile, "api_hash"):
                credentials.set(
                    profile, "api_hash", typer.prompt("Telegram API hash", hide_input=True)
                )

        async def connect() -> dict[str, str]:
            if backend == BotBackend.mtproto:
                client = user_client(profile, config, credentials, new=True)
                try:
                    await client.connect()
                    await client.sign_in(bot_token=token)
                    if not await client.is_user_authorized():
                        raise TeleloomError(
                            "auth_failed", "Telegram did not confirm the bot session."
                        )
                    user = await client.get_me()
                    if not user.bot or str(user.id) != token.split(":", 1)[0]:
                        raise TeleloomError(
                            "account_changed",
                            "Telegram session differs from the selected bot token identity.",
                        )
                    credentials.set(profile, "session", StringSession.save(client.session))
                    return identity(user)
                finally:
                    await client.disconnect()
            bot = Bot(token, session=AiohttpSession(proxy=credentials.get(profile, "proxy")))
            try:
                return identity(await bot.get_me())
            finally:
                await bot.session.close()

        result = asyncio.run(connect())
        if not provided:
            credentials.set(profile, "bot_token", token)
        token = ""
        config.identity = result
        settings.profiles[profile] = config
        settings.save()
        typer.echo(
            json.dumps(
                {
                    "profile_id": profile,
                    "identity": result,
                    "polling_enabled": False,
                    "backend": backend.value,
                }
            )
        )


@profile_app.command("list")
@guarded
def profile_list() -> None:
    settings = Settings.load()
    typer.echo(
        json.dumps(
            {name: profile.model_dump() for name, profile in settings.profiles.items()}, indent=2
        )
    )


@profile_app.command("allow")
@guarded
def allow(
    profile: str, chat_id: str, scope: Permission = typer.Option(...), remove: bool = False
) -> None:
    """Grant/revoke permission for a canonical numeric chat ID obtained from chat_resolve."""
    number(chat_id)
    settings = Settings.load()
    with ownership(settings):
        config = settings.profile(profile)
        field = scope.value + "_chats"
        ids = list(getattr(config, field))
        if remove:
            ids = [id_ for id_ in ids if id_ != chat_id]
        elif chat_id not in ids:
            ids.append(chat_id)
        setattr(config, field, ids)
        settings.save()
    typer.echo(json.dumps({"profile_id": profile, "scope": scope.value, "chat_ids": ids}))


@profile_app.command("read-policy")
@guarded
def read_policy(profile: str, mode: str = typer.Option(...)) -> None:
    """Choose all visible chats or the explicit read allowlist; grants no send/AI permission."""
    if mode not in {"all", "selected"}:
        raise TeleloomError("invalid_policy", "Use all or selected.")
    settings = Settings.load()
    with ownership(settings):
        config = settings.profile(profile)
        config.read_mode = mode  # type: ignore[assignment]
        settings.save()
    typer.echo(
        json.dumps({"profile_id": profile, "read_mode": mode, "read_chats": config.read_chats})
    )


@config_app.command("exposure")
@guarded
def exposure(mode: str = typer.Option(...), tool: list[str] = typer.Option([])) -> None:
    """Choose all, read-only, or selected MCP tools; reconnect clients after restarting the owner."""
    if mode not in {"all", "read-only", "selected"} or (tool and mode != "selected"):
        raise TeleloomError(
            "invalid_exposure", "Use all, read-only, or selected with --tool names."
        )
    settings = Settings.load()
    with ownership(settings):
        settings.exposure_mode = mode  # type: ignore[assignment]
        settings.exposed_tools = list(dict.fromkeys(tool))
        settings.save()
    typer.echo(json.dumps({"exposure_mode": mode, "exposed_tools": settings.exposed_tools}))


@profile_app.command("polling")
@guarded
def polling(profile: str, enable: bool = typer.Option(False, "--enable/--disable")) -> None:
    """Opt into bot polling; never remove an existing Telegram webhook."""
    settings = Settings.load()
    with ownership(settings):
        config = settings.profile(profile)
        if config.kind != "bot":
            raise TeleloomError("capability_unavailable", "Polling is a bot-profile option.")
        config.polling = enable
        settings.save()
    typer.echo(json.dumps({"profile_id": profile, "polling_enabled": enable}))


@profile_app.command("management")
@guarded
def management(
    profile: str, scope: ManagementScope, enable: bool = typer.Option(False, "--enable/--disable")
) -> None:
    """Grant/revoke explicit account management while the owner is stopped; does not grant send or read scope."""
    settings = Settings.load()
    with ownership(settings):
        config = settings.profile(profile)
        if config.kind != "user" and scope in {ManagementScope.contacts, ManagementScope.folders}:
            raise TeleloomError(
                "unsupported_capability", "Telegram contacts and folders are user-only."
            )
        values = list(config.manage_scopes)
        if not enable:
            values = [value for value in values if value != scope.value]
        elif scope.value not in values:
            values.append(scope.value)
        config.manage_scopes = values
        settings.save()
    typer.echo(json.dumps({"profile_id": profile, "manage_scopes": values}))


@profile_app.command("file-root")
@guarded
def file_root(
    profile: str, path: str, enable: bool = typer.Option(True, "--enable/--disable")
) -> None:
    """Grant or revoke one exact local directory while the owner is stopped."""
    from .file_snapshots import plain_path

    requested = Path(path)
    if not requested.is_absolute() or ".." in requested.parts:
        raise TeleloomError("unsafe_file_path", "Use an absolute directory without traversal.")
    selected = plain_path(requested, directory=True) if enable else requested.absolute()
    settings = Settings.load()
    with ownership(settings):
        config = settings.profile(profile)
        roots = [root for root in config.file_roots if Path(root) != selected]
        config.file_roots = [*roots, str(selected)] if enable else roots
        settings.save()
    typer.echo(json.dumps({"profile_id": profile, "file_roots": config.file_roots}))


@profile_app.command("transcription")
@guarded
def transcription_config(
    profile: str,
    local_model_path: Path | None = None,
    openai_endpoint: str | None = None,
    openai_model: str | None = None,
    groq_model: str | None = None,
    external_daily_calls: int | None = None,
    external_daily_bytes: int | None = None,
) -> None:
    """Configure owner transcription models/endpoints/budgets while stopped. Keys use OS keyring/env; allow exact transcription/external chats separately."""
    from .attachments import _local_model
    from .config import TranscriptionConfig

    if local_model_path and not _local_model(str(local_model_path)):
        raise TeleloomError(
            "engine_unavailable", "Supply an absolute complete existing local model directory."
        )
    settings = Settings.load()
    with ownership(settings):
        config = settings.profile(profile)
        values = config.transcription.model_dump()
        for key, value in {
            "local_model_path": str(local_model_path) if local_model_path else None,
            "openai_endpoint": openai_endpoint,
            "openai_model": openai_model,
            "groq_model": groq_model,
            "external_daily_calls": external_daily_calls,
            "external_daily_bytes": external_daily_bytes,
        }.items():
            if value is not None:
                values[key] = value
        config.transcription = TranscriptionConfig.model_validate(values)
        settings.save()
    typer.echo(
        json.dumps({"profile_id": profile, "transcription": config.transcription.model_dump()})
    )


@app.command("limits")
@guarded
def limits(
    recipients: int | None = None,
    interval_seconds: float | None = None,
    daily_messages: int | None = None,
    jev_daily_calls: int | None = None,
    jev_max_characters: int | None = None,
) -> None:
    """Inspect or change owner-controlled limits while the daemon is stopped."""
    settings = Settings.load()
    changes = {
        key: value
        for key, value in {
            "recipients": recipients,
            "interval_seconds": interval_seconds,
            "daily_messages": daily_messages,
            "jev_daily_calls": jev_daily_calls,
            "jev_max_characters": jev_max_characters,
        }.items()
        if value is not None
    }
    if changes:
        with ownership(settings):
            settings.limits = Limits.model_validate({**settings.limits.model_dump(), **changes})
            settings.save()
    typer.echo(settings.limits.model_dump_json(indent=2))


@app.command("serve")
@guarded
def serve_command() -> None:
    """Run the single local Telegram connection owner."""
    run_daemon(Settings.load())


@app.command("mcp")
@guarded
def mcp_command() -> None:
    """Run the stdio MCP bridge, auto-starting the loopback daemon when needed."""
    asyncio.run(bridge(Settings.load()))


@app.command("stop")
@guarded
def stop_command() -> None:
    """Stop the daemon and release its ownership lock before auth or configuration changes."""
    stopped = asyncio.run(stop_daemon(Settings.load()))
    typer.echo(json.dumps({"stopped": stopped}))


@app.command("call")
@guarded
def call_command(
    tool: str,
    args: str = "{}",
    args_file: Path | None = None,
    fields: list[str] | None = typer.Option(None, "--field"),
    preset: str | None = None,
) -> None:
    """Call a named MCP tool through the local owner and print its structured result."""
    arguments = json.loads(args_file.read_text(encoding="utf-8") if args_file else args)
    if not isinstance(arguments, dict):
        raise TeleloomError("invalid_arguments", "Tool arguments must be a JSON object.")
    if fields is not None:
        if "fields" in arguments:
            raise TeleloomError(
                "invalid_arguments", "Choose fields in JSON arguments or --field, not both."
            )
        arguments["fields"] = fields
    if preset is not None:
        if "preset" in arguments:
            raise TeleloomError(
                "invalid_arguments", "Choose a preset in JSON arguments or --preset, not both."
            )
        arguments["preset"] = preset
    if arguments.get("fields") is not None and arguments.get("preset") is not None:
        raise TeleloomError("invalid_arguments", "Choose fields or a preset, not both.")

    async def call() -> Any:
        async with session(Settings.load()) as client:
            return await client.call_tool(tool, arguments)

    result = asyncio.run(call())
    typer.echo(
        json.dumps(result.structuredContent, ensure_ascii=False, indent=2)
        if result.structuredContent
        else json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)
    )
    if result.isError:
        raise typer.Exit(1)


@app.command("fields")
@guarded
def fields_command(
    tool: str,
    request: str = typer.Option(...),
    fields: list[str] | None = typer.Option(None, "--field"),
    preset: str | None = None,
    use_jev: bool = False,
    detail: str = "full",
) -> None:
    """Select response fields from a task and static schema, without reading Telegram."""
    call_command(
        "response_fields_select",
        args=json.dumps(
            {"tool_name": tool, "request": request, "use_jev": use_jev, "detail": detail}
        ),
        fields=fields,
        preset=preset,
    )


@app.command("doctor")
@guarded
def doctor(
    mode: DoctorMode = DoctorMode.local,
    live: bool = False,
    profile_id: str | None = typer.Option(None, "--profile"),
    chat_id: str | None = typer.Option(None, "--chat"),
    timeout_seconds: float = typer.Option(5, min=0.05, max=10),
) -> None:
    """Inspect local/MCP health; --profile NAME --chat ID opts into one bounded Telegram read."""
    from .diagnostics import diagnose

    configuration_valid = True
    try:
        settings = Settings.load()
    except (ValueError, OSError):
        settings = Settings()
        configuration_valid = False
    info: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "profiles": list(settings.profiles),
        "url": settings.url,
    }
    info.update(
        asyncio.run(
            diagnose(
                settings,
                "mcp" if live or profile_id is not None or chat_id is not None else mode.value,
                configuration_valid=configuration_valid,
                include_legacy_profiles=live,
                profile_id=profile_id,
                chat_id=chat_id,
                timeout_seconds=timeout_seconds,
            )
        )
    )
    if live:
        info.setdefault(
            "live",
            {
                "ok": False,
                "data": {},
                "error": {
                    "code": info["daemon"]["status"],
                    "message": "Running owner unavailable; inspect recovery_actions.",
                },
            },
        )
    typer.echo(json.dumps(info, indent=2))


@skills_app.command("install")
@guarded
def install_skills(
    client: Client | None = None, target: Path | None = None, force: bool = False
) -> None:
    """Copy all six skills to a chosen client or directory; --force permits replacement."""
    if target is None and client is None:
        raise TeleloomError("target_required", "Choose --client or --target.")
    homes = {
        Client.codex: ".agents/skills",
        Client.claude: ".claude/skills",
        Client.opencode: ".config/opencode/skills",
        Client.opencode_v1: ".config/opencode/skills",
        Client.pi: ".pi/agent/skills",
    }
    if target is not None:
        destination = target
    elif client == Client.hermes:
        destination = hermes_config_path().parent / "skills"
    else:
        destination = Path.home() / homes[client or Client.codex]
    destination = destination.expanduser().resolve()
    bundled = Path(__file__).parent / "bundled_skills"
    source = bundled if bundled.exists() else Path(__file__).resolve().parents[2] / "skills"
    skills = sorted(
        folder for folder in source.glob("teleloom-*") if (folder / "SKILL.md").is_file()
    )
    if len(skills) != 6:
        raise TeleloomError(
            "package_incomplete", "Six bundled skills were not found; reinstall the package."
        )
    conflicts = [folder.name for folder in skills if (destination / folder.name).exists()]
    if conflicts and not force:
        raise TeleloomError(
            "skills_exist",
            "Existing skills were preserved. Use --force to replace supplied files.",
            details={"skills": conflicts},
        )
    destination.mkdir(parents=True, exist_ok=True)
    for folder in skills:
        output = destination / folder.name
        if output.resolve().parent != destination:
            raise TeleloomError(
                "invalid_target", "Skill directory resolves outside the selected destination."
            )
        shutil.copytree(folder, output, dirs_exist_ok=force)
    typer.echo(
        json.dumps(
            {"installed": [folder.name for folder in skills], "destination": str(destination)}
        )
    )


def client_cli(client: Client, args: list[str]) -> str:
    executable = shutil.which(client.value)
    if executable is None:
        raise TeleloomError(
            "client_cli_unavailable",
            f"{client.value} CLI was not found on PATH; install it or use a manual fragment/--target.",
        )
    try:
        # Hermes cancels tool selection on EOF. An empty answer enables discovered
        # tools, but retains its "no" defaults for overwrite or failed discovery.
        enable_tools = client == Client.hermes and args[:3] == ["mcp", "add", "teleloom"]
        result = subprocess.run(
            [executable, *args],
            shell=False,
            stdin=None if enable_tools else subprocess.DEVNULL,
            input="\n" if enable_tools else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise TeleloomError(
            "client_install_failed",
            f"{client.value} CLI could not complete {args[0]} {args[1]}; check the client before retrying.",
        ) from exc
    # Hermes reports an absent optional section as an error, not JSON null.
    if (
        client == Client.hermes
        and args == ["config", "get", "mcp_servers", "--json"]
        and result.returncode == 1
        and result.stderr.strip() == "Config key not set: mcp_servers"
    ):
        return "{}"
    if result.returncode:
        raise TeleloomError(
            "client_install_failed",
            f"{client.value} {args[0]} {args[1]} failed (exit {result.returncode}); run the native command for diagnostics.",
        )
    return result.stdout


def hermes_config_path() -> Path:
    # The client resolves HERMES_HOME and the active profile; do not guess its home.
    output = client_cli(Client.hermes, ["config", "path"]).strip()
    path = Path(output).expanduser()
    if not output or "\n" in output or not path.is_absolute():
        raise TeleloomError(
            "client_config_unavailable",
            "hermes config path did not return an absolute file path; use --target.",
        )
    return path


def client_entries(client: Client) -> list[Any]:
    args = (
        ["debug", "config"]
        if client == Client.opencode
        else ["config", "get", "mcp_servers", "--json"]
    )
    output = client_cli(client, args)
    try:
        config = json.loads(output)
        if client == Client.opencode:
            if not isinstance(config, list):
                raise ValueError
            servers = [
                item["info"].get("mcp", {}).get("servers", {})
                for item in config
                if item["type"] == "document"
            ]
        else:
            servers = [config]
        if any(not isinstance(items, dict) for items in servers):
            raise ValueError
        return [items["teleloom"] for items in servers if "teleloom" in items]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise TeleloomError(
            "client_config_unavailable",
            f"{client.value} CLI did not return readable configuration; check the client before retrying.",
        ) from exc


def install_client(client: Client, entry: dict[str, Any]) -> None:
    if client not in {Client.opencode, Client.hermes}:
        raise TeleloomError(
            "client_install_unsupported",
            "--install supports only OpenCode 2 (opencode) and Hermes.",
        )
    command = entry["command"]
    args = entry["args"]
    env = entry["env"]
    expected = (
        {"type": "local", "command": [command, *args], "environment": env}
        if client == Client.opencode
        else entry
    )

    def same_path(saved: Any, wanted: str) -> bool:
        return isinstance(saved, str) and (
            saved == wanted
            or (
                sys.platform == "win32"
                and ntpath.isabs(saved)
                and ntpath.isabs(wanted)
                and ntpath.normcase(ntpath.normpath(saved))
                == ntpath.normcase(ntpath.normpath(wanted))
            )
        )

    def matches(saved: Any) -> bool:
        if not isinstance(saved, dict):
            return False
        saved = dict(saved)
        saved_command = saved.get("command")
        if client == Client.opencode:
            if not isinstance(saved_command, list) or not saved_command:
                return False
            if not same_path(saved_command[0], command):
                return False
            saved["command"] = [command, *saved_command[1:]]
        else:
            if not same_path(saved_command, command):
                return False
            saved["command"] = command
        env_key = "environment" if client == Client.opencode else "env"
        saved_env = saved.get(env_key)
        if not isinstance(saved_env, dict) or not same_path(
            saved_env.get("TELELOOM_DATA_DIR"), env["TELELOOM_DATA_DIR"]
        ):
            return False
        saved[env_key] = {**saved_env, "TELELOOM_DATA_DIR": env["TELELOOM_DATA_DIR"]}
        return (
            all(saved.get(key) == value for key, value in expected.items())
            and saved.get("enabled", True) is not False
            and saved.get("disabled", False) is not True
            and "url" not in saved
        )

    existing = client_entries(client)
    if any(not matches(saved) for saved in existing):
        raise TeleloomError(
            "client_config_conflict",
            f"{client.value} already has a different teleloom entry; it was preserved. Review it in the client before installing.",
        )
    if existing:
        status = "already_configured"
    else:
        assignment = "TELELOOM_DATA_DIR=" + env["TELELOOM_DATA_DIR"]
        native_args = ["mcp", "add", "teleloom"]
        if client == Client.opencode:
            native_args += ["--global", "--env", assignment, "--", command, *args]
        else:
            native_args += ["--command", command, "--env", assignment, "--args", *args]
        client_cli(client, native_args)
        installed = client_entries(client)
        if not installed or any(not matches(saved) for saved in installed):
            raise TeleloomError(
                "client_install_failed",
                f"{client.value} did not save the requested active teleloom entry; check its configuration before retrying.",
            )
        status = "installed"
    typer.echo(json.dumps({"client": client.value, "status": status}))


@config_app.command("client")
@guarded
def client_config(client: Client = Client.codex, install: bool = False) -> None:
    """Print a mergeable fragment; --install registers OpenCode 2 or Hermes using their CLI."""
    settings = Settings.load()
    command = sys.executable
    if sys.platform == "win32":
        windowless = Path(command).with_name("pythonw.exe")
        if windowless.is_file():
            command = str(windowless)
    args = ["-m", "teleloom", "mcp"]
    env = {"TELELOOM_DATA_DIR": str(settings.data_dir)}
    entry = {"command": command, "args": args, "env": env}
    if install:
        install_client(client, entry)
        return
    if client == Client.codex:
        typer.echo(
            "[mcp_servers.teleloom]\ncommand = "
            + json.dumps(command)
            + "\nargs = "
            + json.dumps(args)
            + "\n[mcp_servers.teleloom.env]\nTELELOOM_DATA_DIR = "
            + json.dumps(str(settings.data_dir))
        )
    elif client in {Client.opencode, Client.opencode_v1}:
        local: dict[str, Any] = {
            "type": "local",
            "command": [command, *args],
            "environment": env,
        }
        if client == Client.opencode_v1:
            local["enabled"] = True
        servers = {"teleloom": local}
        typer.echo(
            json.dumps(
                {"mcp": {"servers": servers} if client == Client.opencode else servers},
                indent=2,
            )
        )
    else:
        key = "mcpServers" if client in {Client.claude, Client.pi} else "mcp_servers"
        typer.echo(json.dumps({key: {"teleloom": entry}}, indent=2))


@cache_app.command("prune")
@guarded
def prune(before: datetime = typer.Option(...)) -> None:
    """Delete old indexed messages and Jev caches; delivery audit and exports are retained."""
    cutoff = iso(before)
    settings = Settings.load()
    with ownership(settings):
        store = Store(settings.data_dir)
        try:
            with store.db:
                count = store.db.execute("DELETE FROM messages WHERE date<?", (cutoff,)).rowcount
                store.db.execute("DELETE FROM state WHERE key LIKE 'jev:%'")
                for profile in settings.profiles:
                    store.set_state(
                        f"gap:{profile}",
                        "Local index was explicitly pruned; older coverage is incomplete.",
                    )
        finally:
            store.close()
    typer.echo(json.dumps({"deleted_messages": count, "before": cutoff}))
