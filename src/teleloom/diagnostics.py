"""Safe owner inspection and an explicitly selected, bounded Telegram read."""

import asyncio
import importlib.util
import json
import shutil
import tempfile
from importlib import metadata
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

import httpx
from filelock import FileLock, Timeout
from telethon import errors

from . import __version__
from .build_info import build_info
from .config import Settings
from .daemon import health, session
from .models import TeleloomError
from .secrets import Secrets
from .server import create_server

if TYPE_CHECKING:
    from .config import Profile
    from .runtime import Runtime


def failure(code: str, phase: str, profile: "Profile | None" = None) -> dict[str, str]:
    actions = {
        "timeout": "Wait for connection cleanup, then explicitly retry this selected read. Inspect receipts before any delivery retry; unknown delivery must be reconciled.",
        "auth_required": "Have the owner stop the configured daemon and explicitly renew this profile with teleloom auth user --profile NAME --replace; replacement resets generation and grants.",
        "account_restricted": "Inspect the account and target restrictions in Telegram before retrying the selected read.",
        "owner_busy": "Wait for the existing owner/authentication flow. Use the same workspace for another bridge; do not kill processes or start a second Telegram owner.",
        "session_in_use": "Use the existing Telegram session owner or ask the owner to stop that specific daemon normally; do not kill processes or replace authentication to bypass ownership.",
        "profile_closing": "Wait for connection cleanup before explicitly retrying the selected read.",
        "cleanup_failed": "Have the owner stop and restart the configured daemon normally, then reconnect the MCP client; reconcile unknown delivery before any retry.",
        "connection_error": "Check connectivity, then reconnect the MCP client to the configured owner and explicitly retry the read; inspect job receipts before any delivery retry.",
        "not_running": "Start the configured owner with teleloom serve, then rerun teleloom doctor --mode mcp.",
        "daemon_mismatch": "Check the configured workspace, port and OS credentials; connect to its existing owner. Reconnect the MCP client after correcting configuration.",
        "port_occupied": "Have the owner choose the configured teleloom port or identify its existing service; do not kill unrelated processes.",
        "credentials_unavailable": "Restore access to the OS credential backend locally, then rerun doctor; keep credentials out of MCP and conversation.",
        "credentials_missing": "Have the owner restore this profile's OS credentials locally before retrying; keep credentials out of MCP and conversation.",
        "credentials_invalid": "Have the owner repair this profile's local credential configuration before retrying.",
        "invalid_proxy": "Have the owner repair the local proxy configuration before retrying.",
        "webhook_conflict": "Use another bot or have the owner disable polling for this profile; the existing webhook is preserved.",
        "tool_not_exposed": "Have the owner inspect effective tool exposure; diagnostics require messages_get to be exposed. Restart the owner and reconnect clients after an approved exposure change.",
        "read_not_allowed": "Choose a chat allowed by this profile's current read policy, or ask the owner to review the exact chat grant locally.",
        "profile_not_found": "Choose an existing profile from profiles_list; have the owner configure a missing identity locally.",
        "invalid_probe": "Supply both an explicit profile_id and canonical numeric chat_id for a probe, with a timeout from 0.05 to 10 seconds.",
        "invalid_id": "Use an exact canonical numeric chat ID from an authorized reader.",
        "unsupported_capability": "Choose a user profile for live history timing; bot readers expose saved updates and cannot establish live Telegram history health.",
        "peer_unavailable": "Refresh this exact allowed peer through an exposed reader first; this probe uses saved peer metadata and never scans other dialogs.",
        "account_changed": "Have the owner inspect the configured identity and explicitly replace authentication if intended; replacement resets generation and grants.",
        "rate_limited": "Wait for Telegram's rate limit before explicitly retrying the selected read; diagnostics do not sleep and replay RPCs.",
        "telegram_rejected": "Inspect this identity's Telegram permissions and the selected chat before retrying the read.",
        "status_unavailable": "Check effective exposure of server_status and reconnect the MCP client to the configured owner.",
        "configuration_invalid": "Repair or restore config.json from a trusted backup before starting the owner; preserve existing profiles and permissions.",
        "configuration_missing": "Run teleloom init locally to create configuration before starting the owner.",
        "unavailable": "Inspect local configuration and reconnect the MCP client to the configured owner; reconcile unknown delivery before any retry.",
    }
    code = code if code in actions else "unavailable"
    action = actions[code]
    if code == "timeout" and phase in {"owner", "mcp"}:
        action = "Inspect the configured local owner, then reconnect the MCP client or rerun teleloom doctor --mode mcp. Reconcile unknown delivery before any retry; no request was replayed."
    if code == "auth_required" and profile is not None and profile.kind == "bot":
        renewal = "bot --backend mtproto" if profile.bot_backend == "mtproto" else "bot"
        action = action.replace("auth user", f"auth {renewal}")
    return {"code": code, "phase": phase, "next_action": action}


def error_code(error: Exception) -> str:
    if isinstance(error, BaseExceptionGroup):
        return next(
            (error_code(child) for child in error.exceptions if isinstance(child, Exception)),
            "unavailable",
        )
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, errors.UnauthorizedError):
        return "auth_required"
    if isinstance(error, TeleloomError):
        return (
            "timeout"
            if error.code in {"read_timeout", "connection_timeout", "daemon_timeout"}
            else error.code
        )
    from .adapters import telegram_error

    return telegram_error(error).code


def _distribution() -> dict[str, Any]:
    try:
        package = metadata.distribution("teleloom")
        direct = json.loads(package.read_text("direct_url.json") or "{}")
        # A direct URL can contain private paths, credentials or host information.
        return {
            "version": package.version,
            "origin": "editable" if direct.get("dir_info", {}).get("editable") else "installed",
            "version_matches": package.version == __version__,
        }
    except (metadata.PackageNotFoundError, ValueError):
        return {"version": "unknown", "origin": "unknown", "version_matches": False}


def _optional_engines() -> dict[str, Any]:
    def available(name: str) -> bool:
        return importlib.util.find_spec(name) is not None

    return {
        "jev": {"available": available("typesafe_sdk"), "action": "Install teleloom[jev]."},
        "pdf": {"available": available("pypdf"), "action": "Install teleloom[pdf]."},
        "ocr": {
            "available": available("PIL")
            and available("pytesseract")
            and bool(shutil.which("tesseract")),
            "action": "Install teleloom[ocr] and Tesseract with its language data.",
        },
        "transcription": {
            "available": available("faster_whisper"),
            "model": "not_checked",
            "action": "Install teleloom[transcription] and supply an existing local model when reading an attachment.",
        },
    }


def _tools(tools: Any, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "count": len(tools),
        "schemas": {
            tool.name: {
                "parameters": tool.inputSchema.get("properties", {}),
                "required": tool.inputSchema.get("required", []),
            }
            for tool in tools
        },
    }


def _projection_available(tools: dict[str, Any]) -> bool | None:
    schemas = tools["schemas"]
    if "messages_get" not in schemas:
        return None  # Owner exposure can intentionally omit the reader.
    return {"fields", "preset"} <= set(
        schemas.get("messages_get", {}).get("parameters", {})
    ) and "fields" not in schemas.get("delivery_execute", {}).get("parameters", {})


def owner_scope(settings: Settings, runtime: "Runtime | None" = None) -> dict[str, Any]:
    return {
        "source": "running_owner" if runtime else "configured_local",
        "tool_exposure": {
            "mode": settings.exposure_mode,
            "selected_tools": sorted(settings.exposed_tools)
            if settings.exposure_mode == "selected"
            else [],
        },
        "profiles": [
            {
                "id": name,
                "kind": profile.kind,
                "backend": "mtproto" if profile.kind == "user" else profile.bot_backend,
                "identity": {
                    k: v for k, v in profile.identity.items() if k in {"id", "username", "name"}
                },
                "connected": name in runtime.adapters if runtime else None,
                "grants": profile.grants(),
                "failure": failure(runtime.profile_errors[name], "connect", profile)
                if runtime and name in runtime.profile_errors
                else None,
            }
            for name, profile in settings.profiles.items()
        ],
    }


async def selected_probe(
    runtime: "Runtime",
    profile_id: str | None,
    chat_id: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    from .runtime import number

    result: dict[str, Any] = {
        "profile_id": None,
        "chat_id": None,
        "status": "failed",
        "phase": "access",
        "timeout_seconds": min(timeout_seconds, runtime.settings.read_timeout_seconds),
        "logical_requests": {"connect": 0, "read": 0},
        "observed_rpc_requests": None,
        "max_history_requests": 1,
        "max_messages": 1,
        "timings_seconds": {"connect": None, "read": None, "local_processing": None},
    }
    profile = None
    started = None
    try:
        if not profile_id or not chat_id or not 0.05 <= timeout_seconds <= 10:
            raise TeleloomError("invalid_probe", "An exact profile and chat are required.")
        profile = runtime.settings.profile(profile_id)
        result["profile_id"] = profile_id
        number(chat_id)
        result["chat_id"] = chat_id
        profile.require_read(chat_id)
        if (
            runtime.settings.exposure_mode == "selected"
            and "messages_get" not in runtime.settings.exposed_tools
        ):
            raise TeleloomError("tool_not_exposed", "The history reader is hidden.")
        if profile.kind != "user":
            raise TeleloomError("unsupported_capability", "Live history requires a user profile.")
        async with asyncio.timeout(result["timeout_seconds"]):
            result["phase"] = "connect"
            result["connection"] = "reused" if profile_id in runtime.adapters else "opened"
            result["logical_requests"]["connect"] = int(profile_id not in runtime.adapters)
            started = perf_counter()
            adapter = await runtime.adapter(profile_id)
            result["timings_seconds"]["connect"] = perf_counter() - started
            result["phase"] = "access"
            started = None
            runtime.settings.profile(profile_id).require_read(chat_id)
            if (
                runtime.settings.exposure_mode == "selected"
                and "messages_get" not in runtime.settings.exposed_tools
            ):
                raise TeleloomError("tool_not_exposed", "The history reader is hidden.")
            result["phase"] = "read"
            result["logical_requests"]["read"] = 1
            started = perf_counter()
            returned = await adapter.diagnostic_read(chat_id)
            result["timings_seconds"]["read"] = perf_counter() - started
            result["phase"] = "local_processing"
            started = perf_counter()
            result.update(status="ok", returned=returned, source="telegram_history")
            result["timings_seconds"]["local_processing"] = perf_counter() - started
            result["phase"] = "complete"
    except Exception as error:
        if started is not None:
            result["timings_seconds"][result["phase"]] = perf_counter() - started
        result["error"] = failure(error_code(error), result["phase"], profile)
    return result


async def diagnose(
    settings: Settings,
    mode: str,
    *,
    configuration_valid: bool = True,
    include_legacy_profiles: bool = False,
    profile_id: str | None = None,
    chat_id: str | None = None,
    timeout_seconds: float = 5,
) -> dict[str, Any]:
    token = None
    credential_error = None
    try:
        token = Secrets().get("", "mcp_token")
    except TeleloomError as error:
        credential_error = error.code
    config_exists = (settings.data_dir / "config.json").is_file()
    artifact = build_info()
    distribution = _distribution()
    info: dict[str, Any] = {
        "mode": mode,
        "build": artifact,
        "package": distribution,
        "config_exists": config_exists,
        "configuration_status": "valid"
        if configuration_valid and config_exists
        else "missing"
        if configuration_valid
        else "invalid",
        "mcp_token_available": bool(token),
        "optional_engines": _optional_engines(),
        "daemon": {"status": "not_checked"},
        "health": {"local": "checked", "mcp": "not_checked", "telegram": "not_checked"},
        "scope": owner_scope(settings) if configuration_valid else None,
        "failure": failure("configuration_invalid", "configuration")
        if not configuration_valid
        else failure("configuration_missing", "configuration")
        if not config_exists
        else failure("credentials_unavailable", "credentials")
        if not token
        else None,
        "checks": [],
        "recovery_actions": [],
    }

    def check(name: str, ok: bool | None, action: str) -> None:
        info["checks"].append({"name": name, "ok": ok})
        if ok is False:
            info["recovery_actions"].append(action)

    check("config", config_exists, "Run teleloom init to create local configuration.")
    check(
        "configuration_valid",
        configuration_valid,
        "Repair or restore config.json from a trusted backup before starting the owner; preserve existing profiles and permissions.",
    )
    check("credentials", bool(token), "Restore the OS credential backend, then run teleloom init.")
    check(
        "package_version",
        distribution["version_matches"],
        "Reinstall one consistent teleloom wheel in this interpreter.",
    )
    check(
        "build_metadata",
        artifact["build_id"] != "unknown",
        "Install a wheel built with provenance metadata; legacy artifacts report unknown.",
    )
    if credential_error:
        info["credential_error"] = failure(credential_error, "credentials")["code"]
    if mode == "local":
        # Discovery uses isolated temporary state and never starts adapters/jobs.
        from .runtime import Runtime

        with tempfile.TemporaryDirectory(prefix="teleloom-doctor-") as directory:
            isolated = settings.model_copy(update={"data_dir": Path(directory)})
            runtime = Runtime(isolated)
            try:
                info["tools"] = _tools(
                    await create_server(isolated, runtime=runtime).list_tools(), "installed_package"
                )
            finally:
                runtime.store.close()
        check(
            "projection_tools",
            _projection_available(info["tools"]),
            "Install an up-to-date wheel with response field selection.",
        )
        return info
    info["tools"] = {"source": "running_mcp", "count": 0, "schemas": {}}
    if not configuration_valid:
        return info
    if not token:
        info["daemon"] = {"status": "credentials_unavailable"}
        info["failure"] = failure("credentials_unavailable", "credentials")
        return info
    if (profile_id is None) != (chat_id is None):
        info["failure"] = failure("invalid_probe", "access")
        return info
    phase = "owner"
    try:
        async with asyncio.timeout(20):
            if not await health(settings, token, report_timeout=True):
                lock_file = settings.data_dir / "owner.lock"
                if lock_file.exists():
                    try:
                        with FileLock(lock_file, timeout=0):
                            pass
                    except Timeout:
                        raise TeleloomError(
                            "owner_busy", "Another flow owns this workspace."
                        ) from None
                raise TeleloomError("not_running", "The configured owner is stopped.")
            phase = "mcp"
            async with session(settings, auto_start=False) as client:
                info["tools"] = _tools((await client.list_tools()).tools, "running_mcp")
                arguments = (
                    {
                        "profile_id": profile_id,
                        "chat_id": chat_id,
                        "timeout_seconds": timeout_seconds,
                    }
                    if profile_id is not None
                    else {}
                )
                status = await client.call_tool("server_status", arguments)
                if status.isError or not status.structuredContent:
                    raise TeleloomError(
                        "status_unavailable", "Running owner did not return status."
                    )
                info["daemon"] = {"status": "running", **status.structuredContent["data"]}
                info["health"] = info["daemon"].get(
                    "health", {"local": "checked", "mcp": "ok", "telegram": "not_checked"}
                )
                info["scope"] = info["daemon"].get("scope", info["scope"])
                if "probe" in info["daemon"]:
                    info["probe"] = info["daemon"]["probe"]
                    info["failure"] = info["probe"].get("error")
                    if info["failure"]:
                        info["recovery_actions"].append(info["failure"]["next_action"])
                if include_legacy_profiles:
                    # profiles_list returns only the owner's local identity/state
                    # snapshot. Preserve the original --live JSON without Telegram reads.
                    info["live"] = (await client.call_tool("profiles_list", {})).structuredContent
                check(
                    "projection_tools",
                    _projection_available(info["tools"]),
                    "Restart the intended daemon installation and reconnect MCP clients to refresh tool discovery.",
                )
                check(
                    "daemon_build",
                    info["daemon"].get("build") == artifact,
                    "Stop the old owner with teleloom stop and restart it from the intended installation; reconnect MCP clients.",
                )
    except Exception as error:
        info["failure"] = failure(error_code(error), phase)
        info["daemon"] = {"status": info["failure"]["code"]}
        info["health"]["mcp"] = "failed"
        check("daemon", False, info["failure"]["next_action"])
    return info
