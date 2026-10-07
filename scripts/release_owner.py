"""Explicit owner replacement with private state, guarded installation and recovery."""

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from contextlib import suppress
from pathlib import Path

from check_build_metadata import wheel_metadata
from release_artifacts import checkpoint, prepared, validate_bytes
from release_common import (
    current_python,
    digest,
    executable_python,
    fingerprint,
    private_directory,
    python_command,
    read_json,
    require,
    run,
    tooling_env,
    write_json,
)


def probe(python, operation, env, directory):
    return json.loads(
        run(
            python_command(python, Path(__file__).with_name("release_probe.py"), operation),
            env=env,
            cwd=directory,
            timeout=60,
        )
    )


def verified_release(file):
    release = read_json(file)
    require(
        release["phase"] in {"publish", "verify-assets"}
        and release["status"] == "passed"
        and release["download_checks"] == "passed",
        "download_not_verified",
    )
    original = read_json(release["prepare_report"])
    preparation = prepared(Path(original["artifacts"]), release["tag"], release["source_commit"])
    require(
        release["artifact_hashes"] == preparation["artifact_hashes"]
        and release["build"] == preparation["build"]
        and release["inventory"] == preparation["inventory"],
        "verified_report_conflict",
    )
    require(
        all(release[key] == preparation[key] for key in ("gates", "evidence", "source_tree")),
        "verified_report_conflict",
    )
    require(
        validate_bytes(Path(release["artifacts"]), release["tag"], release["source_commit"])
        == release["artifact_hashes"],
        "hash_mismatch",
    )
    return release


def target_environment(descriptor, python):
    allowed = {"data_dir", "client_config", "server_name", "skills_dir", "skills", "owner_pid"}
    require(set(descriptor) <= allowed, "target_descriptor")
    client = read_json(descriptor["client_config"])
    entry = client["mcpServers"][descriptor["server_name"]]
    require(
        Path(entry["command"]).expanduser().absolute() == Path(python).expanduser().absolute(),
        "client_python_mismatch",
    )
    require(
        entry["args"][-3:] == ["-m", "teleloom", "mcp"]
        and entry["args"][:-3] in [[], ["-I"], ["-X", "utf8"], ["-I", "-X", "utf8"]],
        "client_launch_mismatch",
    )
    # The selected client may inherit credentials/PATH rather than duplicate them
    # in its config. Preserve this environment in memory; serialize only its hash.
    env = dict(os.environ)
    env.update(entry.get("env", {}))
    require(
        all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()),
        "client_environment",
    )
    require(
        Path(env.get("TELELOOM_DATA_DIR", descriptor["data_dir"])).resolve()
        == Path(descriptor["data_dir"]).resolve(),
        "client_workspace_mismatch",
    )
    env["TELELOOM_DATA_DIR"] = str(Path(descriptor["data_dir"]).resolve())
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def permission_hash(path):
    if os.name == "nt":
        from teleloom.config import windows_system_directory

        result = subprocess.run(
            [str(windows_system_directory() / "icacls.exe"), str(path)],
            capture_output=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        import hashlib

        return hashlib.sha256(result.stdout).hexdigest()
    status = path.stat()
    return fingerprint([status.st_mode, status.st_uid, status.st_gid])


def skills_hashes(root):
    require(root.is_dir() and not root.is_symlink(), "skill_target")
    files = list(root.rglob("*"))
    require(not any(path.is_symlink() for path in files), "skill_target")
    return {path.relative_to(root).as_posix(): digest(path) for path in files if path.is_file()}


def snapshot(python, descriptor, env):
    data = Path(descriptor["data_dir"]).resolve()
    require(data.is_dir() and (data / "config.json").is_file(), "target_missing")
    value = probe(python, "snapshot", env, data)
    value["client_sha256"] = digest(Path(descriptor["client_config"]))
    value["environment_sha256"] = fingerprint(env)
    value["permissions"] = {
        key: permission_hash(Path(path))
        for key, path in {
            "data": data,
            "config": data / "config.json",
            "client": descriptor["client_config"],
            "skills": descriptor["skills_dir"],
        }.items()
    }
    return value


def preservation(before, after):
    differences = [
        name
        for name in (
            "config_sha256",
            "profiles",
            "capabilities",
            "credential_references",
            "generations",
            "client_sha256",
            "environment_sha256",
            "permissions",
        )
        if before[name] != after[name]
    ]
    # New evidence can be added by polling; every historical record must remain identical.
    for table in ("jobs", "receipts"):
        if any(after[table].get(key) != value for key, value in before[table].items()):
            differences.append(table)
    return differences


def extras(value):
    values = set(filter(None, value.split(",")))
    require(values <= {"jev", "pdf", "ocr", "transcription"}, "invalid_extras")
    return sorted(values)


class Packages:
    """External package installer boundary; target process/SQLite/files stay real."""

    def install(self, python, wheel, chosen_extras, env, directory):
        uv = shutil.which("uv")
        require(uv is not None, "uv_missing")
        requirement = str(wheel) + (f"[{','.join(chosen_extras)}]" if chosen_extras else "")
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--reinstall-package",
                "teleloom",
                requirement,
            ],
            cwd=directory,
            env=env,
        )


def wait_exit(pid, timeout=15):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            require(ctypes.get_last_error() == 87, "owner_exit_unproven")
            return
        try:
            require(
                kernel.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000)) == 0,
                "owner_exit_pending",
            )
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # A zombie has exited but is still awaiting its external parent's wait().
        status = Path(f"/proc/{pid}/stat")
        if status.exists() and status.read_text(encoding="utf-8").split(") ", 1)[1].startswith(
            "Z "
        ):
            return
        time.sleep(0.05)
    require(False, "owner_exit_pending")


def port_closed(port):
    with socket.socket() as connection:
        connection.settimeout(0.25)
        return connection.connect_ex(("127.0.0.1", port)) != 0


def start_owner(python, env, directory):
    kwargs = {
        "cwd": directory,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(python_command(python, "-m", "teleloom", "serve"), **kwargs)


def verify(python, descriptor, env, release, baseline, report, observation=None):
    info = probe(python, "inspect", env, Path(descriptor["data_dir"]))
    require(info["installed"], "editable_or_checkout")
    require(info["build"] == release["build"] == info["local"]["build"], "installed_build_mismatch")
    require(
        info["mcp"]["daemon"].get("build") == release["build"]
        and info.get("server_status", {}).get("data", {}).get("build") == release["build"],
        "runtime_build_mismatch",
    )
    require(
        info["local"]["tools"]["schemas"]
        == release["inventory"]["schemas"]
        == info["mcp"]["tools"]["schemas"],
        "discovery_mismatch",
    )
    require(info["skills"] == release["inventory"]["skills"], "bundled_skills_mismatch")
    root = Path(descriptor["skills_dir"])
    for skill in descriptor["skills"]:
        expected = {
            name.split("/", 1)[1]: sha
            for name, sha in release["inventory"]["skills"].items()
            if name.startswith(skill + "/")
        }
        require(skills_hashes(root / skill) == expected, "installed_skills_mismatch")
    for name in baseline["extras"]:
        require(info["local"]["optional_engines"][name]["available"], "engine_unavailable")
    differences = preservation(baseline["snapshot"], snapshot(python, descriptor, env))
    report["preservation_differences"] = differences
    require(not differences, "preservation_changed")
    report["owner_state"] = "running_verified"
    report["native_client"] = {"status": "pending_reconnection"}
    if observation:
        native = read_json(observation)
        require(
            native["client"] == descriptor["server_name"]
            and native["reconnected"] is True
            and native["baseline_sha256"] == fingerprint(baseline)
            and native["server_status"]["build"] == release["build"]
            and native["schemas"] == release["inventory"]["schemas"]
            and bool(native["reference"]),
            "native_observation_invalid",
        )
        report["native_client"] = {"status": "verified", "reference": native["reference"]}
    checkpoint(report, "owner_verified")


def owner_phase(args, report, *, packages=None):
    release = verified_release(args.verified_release)
    report.update(
        {key: release[key] for key in ("source_commit", "artifact_hashes", "build", "tag")}
    )
    python = executable_python(args.python)
    require(python != current_python(), "tooling_is_target")
    target_prefix = run(
        python_command(python, "-c", "import sys; print(sys.prefix)"), env=tooling_env()
    )
    require(Path(target_prefix).resolve() != Path(sys.prefix).resolve(), "tooling_is_target")
    report["owner_state"] = "unchanged"
    if args.phase == "verify-owner":
        baseline = read_json(args.baseline)
        require(baseline["python"] == str(args.python.absolute()), "target_mismatch")
        descriptor = baseline["target"]
        env = target_environment(descriptor, args.python)
        verify(python, descriptor, env, release, baseline, report, args.native_observation)
        # Explicit verification closes a pending journal; an upgrade rerun never installs again.
        journal = Path(baseline["journal_path"])
        if journal.is_file() and not args.dry_run:
            pending = read_json(journal)
            require(
                pending["source_commit"] == release["source_commit"]
                and pending["artifact_hashes"] == release["artifact_hashes"],
                "journal_conflict",
            )
            pending.update(
                status="passed",
                completed_stage="owner_verified",
                owner_state="running_verified",
                native_client=report["native_client"],
            )
            write_json(journal, pending)
        return
    # The durable journal is an idempotency guard, including uncertain install/verification.
    journal = args.state_dir / "baseline.json"
    require(not journal.exists(), "upgrade_already_started_use_verify_owner_or_recovery")
    descriptor = read_json(args.client)
    env = target_environment(descriptor, args.python)
    before = snapshot(python, descriptor, env)
    require(before["active_jobs"] == 0, "active_jobs")
    inspect = probe(python, "inspect", env, Path(descriptor["data_dir"]))
    require(inspect["installed"], "editable_or_checkout")
    old_build = wheel_metadata(args.previous_wheel)
    require(old_build == inspect["build"], "previous_artifact_mismatch")
    selected = extras(args.extras)
    previous_extras = extras(args.previous_extras)
    require(set(previous_extras) <= set(selected), "extras_not_preserved")
    require(len(descriptor["skills"]) == len(set(descriptor["skills"])), "skill_target")
    available_skills = {name.split("/", 1)[0] for name in release["inventory"]["skills"]}
    require(
        bool(descriptor["skills"]) and set(descriptor["skills"]) <= available_skills, "skill_target"
    )
    root = Path(descriptor["skills_dir"]).resolve()
    for skill in descriptor["skills"]:
        require((root / skill).resolve().parent == root, "skill_target")
        skills_hashes(root / skill)
    running = inspect["mcp"]["daemon"]["status"] == "running"
    require(running or inspect["mcp"]["daemon"]["status"] == "not_running", "owner_unavailable")
    if running:
        require(
            isinstance(descriptor.get("owner_pid"), int) and descriptor["owner_pid"] > 0,
            "owner_pid_required",
        )
    else:
        configured_port = read_json(Path(descriptor["data_dir"]) / "config.json").get("port", 8765)
        require(port_closed(configured_port), "owner_unavailable")
    report["plan"] = {
        "python": str(args.python.absolute()),
        "artifacts": release["artifact_hashes"],
        "extras": selected,
        "skills": descriptor["skills"],
        "preservation_differences": [],
        "client_config_sha256": before["client_sha256"],
    }
    if args.dry_run:
        checkpoint(report, "preflight")
        return
    private_directory(args.state_dir)
    recovery = args.state_dir / "recovery"
    private_directory(recovery)
    old_wheel = recovery / args.previous_wheel.name
    shutil.copy2(args.previous_wheel, old_wheel)
    shutil.copy2(Path(descriptor["data_dir"]) / "config.json", recovery / "config.json")
    for skill in descriptor["skills"]:
        shutil.copytree(root / skill, recovery / "skills" / skill)
    baseline = {
        "journal_path": report["report_path"],
        "python": str(args.python.absolute()),
        "target": descriptor,
        "snapshot": before,
        "extras": selected,
        "previous_extras": previous_extras,
        "previous_build": old_build,
        "previous_wheel_sha256": digest(old_wheel),
        "previous_skills": {skill: skills_hashes(root / skill) for skill in descriptor["skills"]},
    }
    write_json(journal, baseline)
    uv = shutil.which("uv")
    report["baseline"] = str(journal.resolve())
    report["recovery"] = {
        "stop_command": python_command(python, "-m", "teleloom", "stop"),
        "package_command": [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--reinstall-package",
            "teleloom",
            str(old_wheel.resolve())
            + (f"[{','.join(previous_extras)}]" if previous_extras else ""),
        ],
        "skills": [
            {"backup": str((recovery / "skills" / skill).resolve()), "target": str(root / skill)}
            for skill in descriptor["skills"]
        ],
        "restore_skills_command": [
            str(current_python()),
            "-X",
            "utf8",
            str(Path(__file__).with_name("restore_release_skills.py").resolve()),
            "--baseline",
            str(journal.resolve()),
        ],
        "restart_command": python_command(args.python.absolute(), "-m", "teleloom", "serve"),
        "database": "Preserve current database; never restore a historical database or replay deliveries.",
    }
    checkpoint(report, "baseline_saved")
    process = None
    from filelock import FileLock, Timeout

    startup = FileLock(Path(descriptor["data_dir"]) / "startup.lock", timeout=15)
    try:
        try:
            startup.acquire()
        except Timeout:
            require(False, "owner_startup_busy")
        if running:
            report["owner_state"] = "shutdown_pending"
            checkpoint(report, "owner_shutdown_pending")
            probe(python, "stop", env, Path(descriptor["data_dir"]))
            wait_exit(descriptor["owner_pid"])
        try:
            lock = FileLock(Path(descriptor["data_dir"]) / "owner.lock", timeout=15)
            with lock:
                port = read_json(Path(descriptor["data_dir"]) / "config.json").get("port", 8765)
                require(port_closed(port), "owner_exit_pending")
                report["owner_state"] = "stopped"
                require(snapshot(python, descriptor, env)["active_jobs"] == 0, "active_jobs")
                require(
                    not preservation(before, snapshot(python, descriptor, env)),
                    "preservation_changed",
                )
                checkpoint(report, "owner_stopped")
                checkpoint(report, "install_pending")
                report["owner_state"] = "stopped_installation_uncertain"
                (packages or Packages()).install(
                    python,
                    next(Path(release["artifacts"]).glob("*.whl")),
                    selected,
                    env,
                    Path(descriptor["data_dir"]),
                )
                report["owner_state"] = "stopped_package_installed"
                installed = probe(python, "artifact", env, Path(descriptor["data_dir"]))
                require(
                    installed["installed"] and installed["build"] == release["build"],
                    "installed_build_mismatch",
                )
                checkpoint(report, "package_installed")
                checkpoint(report, "skills_pending")
                with zipfile.ZipFile(next(Path(release["artifacts"]).glob("*.whl"))) as archive:
                    for skill in descriptor["skills"]:
                        # Replace each selected tree exactly, eliminating stale supplied files.
                        shutil.rmtree(root / skill)
                        (root / skill).mkdir()
                        prefix = f"teleloom/bundled_skills/{skill}/"
                        for name in archive.namelist():
                            if name.startswith(prefix) and not name.endswith("/"):
                                relative = name.removeprefix(prefix)
                                destination = root / skill / relative
                                require(
                                    destination.resolve().is_relative_to(root / skill),
                                    "skill_target",
                                )
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                destination.write_bytes(archive.read(name))
                checkpoint(report, "skills_refreshed")
        except Timeout:
            require(False, "owner_lock_busy")
        checkpoint(report, "restart_pending")
        process = start_owner(args.python.absolute(), env, Path(descriptor["data_dir"]))
        report["owner_state"] = "restart_pending"
        deadline = time.monotonic() + 20
        while port_closed(port) and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        require(process.poll() is None and not port_closed(port), "owner_restart_failed")
        report["owner_state"] = "running_verification_pending"
        report["owner_pid"] = process.pid
        checkpoint(report, "runtime_verification_pending")
        verify(python, descriptor, env, release, baseline, report, args.native_observation)
    except BaseException:
        # A failed invocation owns its restarted child and must await its exit.
        # A successful upgrade intentionally transfers that process to the owner.
        if process is not None:
            if process.poll() is None:
                # On Windows a venv/pythonw launcher can own a child interpreter.
                # Graceful authenticated shutdown lets that child release the lock
                # and listener before awaiting the launcher; terminating a launcher
                # alone is not proof that its runtime exited.
                with suppress(Exception):
                    probe(python, "stop", env, Path(descriptor["data_dir"]))
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            report["owner_state"] = (
                "stopped_after_verification_failure"
                if port_closed(port)
                else "unexpected_owner_running"
            )
        raise
    finally:
        startup.release()
