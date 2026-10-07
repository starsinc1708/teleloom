"""Shared release contracts. No runtime credentials or content enter stage reports."""

import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter


class ReleaseError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ReleaseError(code)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def timed_stage(report, name, *, workers=0, path=None):
    """Record completion and safe identities, never commands, environments or exceptions."""
    record = {"stage": name, "exit": None, "seconds": 0, "workers": workers}
    started = perf_counter()
    try:
        yield record
        if record["exit"] is None:
            record["exit"] = 0
    except BaseException:
        if record["exit"] in {None, 0}:
            record["exit"] = 1
        raise
    finally:
        record["seconds"] = round(perf_counter() - started, 6)
        for key in ("source_commit", "source_tree"):
            value = report.get(key, "unknown")
            record[key] = (
                value
                if len(value) == 40 and all(c in "0123456789abcdef" for c in value)
                else "unknown"
            )
        if report.get("build"):
            record["build"] = {
                key: report["build"][key]
                for key in ("package_version", "build_id", "source_commit", "build_type")
            }
        report.setdefault("timings", []).append(record)
        if path is not None:
            write_json(path, report)


def run(command, *, cwd=None, env=None, timeout=600, timing=None):
    # Both boundaries are explicit; shell codepages and locale do not define JSON encoding.
    result = subprocess.run(
        command, cwd=cwd, env=env, timeout=timeout, capture_output=True, text=True, encoding="utf-8"
    )
    if timing is not None:
        timing["exit"] = result.returncode
        if timing["stage"] == "nested_pytest":
            workers = re.search(r"(?m)^(\d+) workers \[", result.stdout)
            timing["workers"] = int(workers[1]) if workers else 0
    require(result.returncode == 0, "operation_failed")
    return result.stdout.strip()


def parse_pytest_durations(stdout):
    """Keep native timings and safe node identities; discard parameter values/groups."""
    return [
        {"seconds": float(match[1]), "phase": match[2], "test": match[3]}
        for line in stdout.splitlines()
        if (
            match := re.match(
                r"([0-9.]+)s\s+(setup|call|teardown)\s+(tests/[A-Za-z_]\w*\.py(?:::[A-Za-z_]\w*)*)(?:\[|@|$)",
                line.strip(),
            )
        )
    ]


def python_command(python, *args):
    return [str(python), "-I", "-X", "utf8", *map(str, args)]


def tooling_env():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TELELOOM_")
        and key not in {"TYPESAFE_API_KEY", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"}
    }
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def private_directory(path):
    # Use the existing OS-specific private-directory contract, in the tooling process.
    from teleloom.config import private_dir, windows_system_directory

    path = Path(path).absolute()
    private_dir(path)
    path.chmod(0o700)
    if os.name == "nt":
        # Replace the entire DACL, including explicit grants on an existing directory.
        # The path is transported as data; it is never interpolated into shell code.
        command = """
$taskSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$taskAcl = [System.Security.AccessControl.DirectorySecurity]::new()
$taskAcl.SetOwner($taskSid)
$taskAcl.SetAccessRuleProtection($true, $false)
foreach ($taskPrincipal in @($taskSid, [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
    $taskRule = [System.Security.AccessControl.FileSystemAccessRule]::new($taskPrincipal, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $taskAcl.AddAccessRule($taskRule)
}
[System.IO.Directory]::SetAccessControl($env:TELELOOM_RELEASE_PRIVATE_DIR, $taskAcl)
"""
        run(
            [
                str(windows_system_directory() / "WindowsPowerShell/v1.0/powershell.exe"),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            env={**os.environ, "TELELOOM_RELEASE_PRIVATE_DIR": str(path)},
        )


def executable_python(path):
    # A venv launcher is commonly a symlink on Linux. Resolving it selects the
    # base interpreter and loses the chosen environment.
    path = Path(path).expanduser().absolute()
    require(path.is_file(), "python_missing")
    # pythonw must remain the client's launcher, but probes need captured console output.
    console = path.with_name("python.exe") if path.name.lower() == "pythonw.exe" else path
    require(console.is_file(), "python_missing")
    return console


def current_python():
    return Path(sys.executable).absolute()
