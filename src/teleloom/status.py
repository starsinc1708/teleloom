"""Safe, read-only owner status shared by MCP and the browser panel."""

from typing import Any

from . import __version__
from .build_info import build_info
from .config import Settings
from .models import utcnow
from .runtime import Runtime, fingerprint


def status_snapshot(settings: Settings, runtime: Runtime) -> dict[str, Any]:
    errors = {
        name: runtime.profile_errors[name]
        for name in sorted(runtime.profile_errors)
        if name in settings.profiles
    }
    if runtime.worker is None:
        state = "starting"
    elif errors:
        state = "error"
    elif not settings.profiles:
        state = "empty"
    else:
        state = "ready"
    return {
        "service": "teleloom",
        "owner_id": runtime.owner_id,
        "workspace_id": fingerprint(str(settings.data_dir.resolve())),
        "version": __version__,
        "build": build_info(),
        "profiles": len(settings.profiles),
        "configuration_status": "valid"
        if (settings.data_dir / "config.json").is_file()
        else "missing",
        "health": {"local": "ok", "telegram": "not_checked"},
        "state": state,
        "profile_errors": errors,
        "snapshot_at": utcnow().isoformat(),
    }
