"""Safe provenance loaded only from the installed package's build artifact."""

import json
import re
from importlib.resources import files

from . import __version__


def _read_build_info() -> dict[str, str]:
    unknown = {
        "package_version": __version__,
        "build_id": "unknown",
        "source_commit": "unknown",
        "build_type": "unknown",
    }
    try:
        value = json.loads((files("teleloom") / "_build_info.json").read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != set(unknown)
            or not all(isinstance(item, str) for item in value.values())
            or value["package_version"] != __version__
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value["build_id"])
            or not re.fullmatch(r"[a-f0-9]{40}|unknown", value["source_commit"])
            or value["build_type"] not in {"release", "local", "dev", "unknown"}
        ):
            return unknown
        return value
    except (OSError, ValueError, TypeError):
        return unknown


# An in-place installation can replace resource files while the old daemon's
# modules remain loaded. The running process must keep its own startup identity.
_BUILD_INFO = _read_build_info()


def build_info() -> dict[str, str]:
    return _BUILD_INFO.copy()
