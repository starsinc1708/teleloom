"""Stamp provenance while building; installed runtime never runs Git."""

import ast
import json
import os
import re
import subprocess
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root).resolve()
        package = root / "src" / "teleloom"
        package_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
        tree = ast.parse((package / "__init__.py").read_text(encoding="utf-8"))
        module_version = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
        )
        if package_version != module_version:
            raise ValueError("Package metadata and module version disagree.")
        embedded = package / "_build_info.json"
        if embedded.exists() and not (root / ".git").exists() and version != "editable":
            value = json.loads(embedded.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or set(value) != {"package_version", "build_id", "source_commit", "build_type"}
                or value["package_version"] != package_version
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value["build_id"])
                or not re.fullmatch(r"[a-f0-9]{40}|unknown", value["source_commit"])
                or value["build_type"] not in {"release", "local", "dev", "unknown"}
            ):
                raise ValueError("Source archive has invalid build provenance.")
        else:
            commit, clean = "unknown", False
            # .git may be a worktree pointer. Require it at this exact source root;
            # an archive inside some unrelated repository must stay unknown.
            if (root / ".git").exists():
                try:
                    kwargs = {
                        "cwd": root,
                        "capture_output": True,
                        "text": True,
                        "encoding": "utf-8",
                        "check": True,
                        "timeout": 5,
                    }
                    if (
                        Path(
                            subprocess.run(
                                ["git", "rev-parse", "--show-toplevel"], **kwargs
                            ).stdout.strip()
                        ).resolve()
                        == root
                    ):
                        candidate = subprocess.run(
                            ["git", "rev-parse", "HEAD"], **kwargs
                        ).stdout.strip()
                        if re.fullmatch(r"[a-f0-9]{40}", candidate):
                            commit = candidate
                            clean = not subprocess.run(
                                ["git", "status", "--porcelain"], **kwargs
                            ).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    pass
            kind = os.getenv("TELELOOM_BUILD_TYPE", "dev" if version == "editable" else "local")
            if kind not in {"release", "local", "dev"}:
                raise ValueError("TELELOOM_BUILD_TYPE must be release, local or dev.")
            if kind == "release" and (commit == "unknown" or not clean):
                raise ValueError("Release provenance requires a clean known Git source commit.")
            build_id = os.getenv("TELELOOM_BUILD_ID", uuid.uuid4().hex)
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", build_id):
                raise ValueError(
                    "TELELOOM_BUILD_ID must be a safe identifier of 1 to 64 characters."
                )
            value = {
                "package_version": package_version,
                "build_id": build_id,
                "source_commit": commit,
                "build_type": kind,
            }
        self.generated = tempfile.TemporaryDirectory(prefix="teleloom-build-")
        generated = Path(self.generated.name) / "_build_info.json"
        generated.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        target = (
            "src/teleloom/_build_info.json"
            if self.target_name == "sdist"
            else "teleloom/_build_info.json"
        )
        build_data["force_include"][str(generated)] = target
        if version == "editable":
            # Editable imports resolve to source: record the editable installation's
            # stamp there. Subsequent source edits intentionally do not restamp it.
            embedded.write_text(generated.read_text(encoding="utf-8"), encoding="utf-8")

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        self.generated.cleanup()
