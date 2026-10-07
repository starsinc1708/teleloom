"""Private probes executed directly by the selected installed interpreter, with -I -X utf8."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sysconfig
import tempfile
from importlib.metadata import distribution
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def skill_hashes(directory):
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def artifact():
    import teleloom
    from teleloom.build_info import build_info

    package = distribution("teleloom")
    origin = json.loads(package.read_text("direct_url.json") or "{}")
    imported = Path(teleloom.__file__).resolve()
    installed = Path(package.locate_file("teleloom/__init__.py")).resolve()
    return {
        "build": build_info(),
        "installed": not origin.get("dir_info", {}).get("editable", False)
        and imported == installed
        and imported.is_relative_to(Path(sysconfig.get_path("purelib"))),
        "skills": skill_hashes(imported.parent / "bundled_skills"),
    }


def snapshot(directory):
    config = directory / "config.json"
    value = json.loads(config.read_text(encoding="utf-8"))
    # State and evidence are hashed semantically. Never export content, credential values,
    # live database bytes or polling/checkpoint state that normally evolves.
    result = {
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "profiles": fingerprint(value.get("profiles", {})),
        "jobs": {},
        "receipts": {},
        "generations": {},
        "active_jobs": 0,
        "credential_references": sorted(
            key for key in os.environ if key.startswith("TELELOOM_") or key == "TYPESAFE_API_KEY"
        ),
    }
    from teleloom.config import Settings
    from teleloom.runtime import Runtime

    settings = Settings.load(directory)
    with tempfile.TemporaryDirectory(prefix="teleloom-release-capabilities-") as isolated:
        settings.data_dir = Path(isolated)
        runtime = Runtime(settings)
        try:
            capabilities = {
                profile["id"]: {"kind": profile["kind"], "capabilities": profile["capabilities"]}
                for profile in runtime.profiles()["profiles"]
            }
            result["capabilities"] = fingerprint(capabilities)
        finally:
            runtime.store.close()
    db_path = directory / "workspace.sqlite"
    if db_path.exists():
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("BEGIN")
            for id_, data in connection.execute("SELECT id,data FROM jobs ORDER BY id"):
                job = json.loads(data)
                result["active_jobs"] += int(job["status"] in {"queued", "running", "paused"})
                result["jobs"][id_] = {"status": job["status"], "sha256": fingerprint(job)}
            for job, position, data in connection.execute(
                "SELECT job,position,data FROM deliveries ORDER BY job,position"
            ):
                receipt = json.loads(data)
                result["receipts"][f"{job}:{position}"] = {
                    "status": receipt["status"],
                    "sha256": fingerprint(receipt),
                }
                result["active_jobs"] += int(receipt["status"] in {"pending", "sending"})
            result["generations"] = {
                key: fingerprint(json.loads(data))
                for key, data in connection.execute(
                    "SELECT key,data FROM state WHERE key LIKE 'profile_generation:%' ORDER BY key"
                )
            }
    return result


async def inspect():
    from teleloom.config import Settings
    from teleloom.daemon import session
    from teleloom.diagnostics import diagnose

    settings = Settings.load()
    local = await diagnose(settings, "local")
    mcp = await diagnose(settings, "mcp")
    result = {**artifact(), "local": local, "mcp": mcp}
    if mcp["daemon"]["status"] == "running":
        async with session(settings, auto_start=False) as client:
            response = await client.call_tool("server_status", {})
            result["server_status"] = response.structuredContent
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=["snapshot", "inspect", "inventory", "artifact", "stop"]
    )
    args = parser.parse_args()
    from teleloom.config import Settings

    if args.operation == "snapshot":
        value = snapshot(Settings.load().data_dir)
    elif args.operation == "artifact":
        value = artifact()
    elif args.operation == "inventory":
        from teleloom.diagnostics import diagnose

        local = asyncio.run(diagnose(Settings(), "local"))
        value = {**artifact(), "schemas": local["tools"]["schemas"]}
    elif args.operation == "stop":
        from teleloom.daemon import stop_daemon

        value = {"stopped": asyncio.run(stop_daemon(Settings.load()))}
    else:
        value = asyncio.run(inspect())
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
