"""Owner changes against real isolated installations, files, SQLite and processes."""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_build_metadata import wheel_metadata
from scripts.release_common import timed_stage
from tests.release_fixtures import RELEASE_TAG, invoke

pytestmark = [pytest.mark.xdist_group("release"), pytest.mark.release_e2e, pytest.mark.process_e2e]


@pytest.fixture(scope="session")
def previous_wheel(real_release):
    source, output, commit = real_release
    timings = {"source_commit": commit}
    with timed_stage(
        timings,
        "previous_wheel",
        workers=int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "0")),
        path=output / "previous-wheel-timings.json",
    ):
        previous = output / "previous-wheel"
        env = {
            **os.environ,
            "TELELOOM_BUILD_TYPE": "local",
            "TELELOOM_BUILD_ID": "previous-build",
            "PYTHONUTF8": "0",
        }
        subprocess.run(
            [shutil.which("uv"), "build", "--wheel", "--out-dir", str(previous), str(source)],
            env=env,
            check=True,
            capture_output=True,
        )
        wheel = next(previous.glob("*.whl"))
        timings["build"] = wheel_metadata(wheel)
        assert timings["build"]["source_commit"] == commit
    return wheel


@pytest.fixture
def owner_target(real_release, previous_wheel, tmp_path, monkeypatch):
    source, output, commit = real_release
    timings = {"source_commit": commit, "build": wheel_metadata(previous_wheel)}
    with timed_stage(
        timings,
        "owner_target",
        workers=int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "0")),
        path=tmp_path / "owner-target-timings.json",
    ):
        target = tmp_path / "владелец с пробелами"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("TELELOOM_")
            and key not in {"PYTHONPATH", "PYTHONHOME", "TYPESAFE_API_KEY"}
        }
        uv = shutil.which("uv")
        subprocess.run(
            [uv, "venv", "--python", sys.executable, str(target)],
            env=env,
            check=True,
            capture_output=True,
        )
        python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        wheel = previous_wheel
        subprocess.run(
            [uv, "pip", "install", "--python", str(python), str(wheel)],
            env=env,
            check=True,
            capture_output=True,
        )
        data = tmp_path / "состояние"
        data.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        (data / "config.json").write_text(
            json.dumps(
                {
                    "port": port,
                    "jev_model": "модель",
                    "profiles": {
                        "fixture": {
                            "kind": "user",
                            "generation": "generation-one",
                            "send_chats": ["123"],
                            "jev_chats": ["123"],
                        },
                        "bot": {
                            "kind": "bot",
                            "generation": "generation-two",
                            "broadcast_chats": ["456"],
                        },
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        from teleloom.store import Store

        original_store = Store(data)
        original_store.bind_profile("fixture", "generation-one")
        original_store.bind_profile("bot", "generation-two")
        original_store.db.commit()
        original_store.close()
        skills = tmp_path / "skills"
        shutil.copytree(source / "skills", skills)
        client = tmp_path / "client.json"
        launcher = python.with_name("pythonw.exe") if os.name == "nt" else python
        client.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "teleloom": {
                            "command": str(launcher),
                            "args": ["-I", "-X", "utf8", "-m", "teleloom", "mcp"],
                            "env": {
                                "TELELOOM_DATA_DIR": str(data),
                                "TELELOOM_MCP_TOKEN": "private-fixture-token",
                                "TESSDATA_PREFIX": "кириллица",
                                "WHISPER_MODEL": "local-model",
                            },
                        },
                        "unrelated": {"command": "unrelated-command"},
                    }
                }
            ),
            encoding="utf-8",
        )
        descriptor = tmp_path / "target.json"
        descriptor.write_text(
            json.dumps(
                {
                    "data_dir": str(data),
                    "client_config": str(client),
                    "server_name": "teleloom",
                    "skills_dir": str(skills),
                    "skills": ["teleloom-read", "teleloom-inbox"],
                }
            ),
            encoding="utf-8",
        )
        verified = tmp_path / "verified.json"
        prepared = json.loads((output / "prepare-report.json").read_text(encoding="utf-8"))
        verified.write_text(
            json.dumps(
                {
                    **prepared,
                    "phase": "verify-assets",
                    "download_checks": "passed",
                    "prepare_report": str(output / "prepare-report.json"),
                }
            ),
            encoding="utf-8",
        )
        target_info = {
            "python": python,
            "launcher": launcher,
            "wheel": wheel,
            "data": data,
            "skills": skills,
            "client": client,
            "descriptor": descriptor,
            "verified": verified,
            "state": tmp_path / "private-upgrade",
            "port": port,
            "output": output,
            "commit": commit,
        }
    yield target_info
    with timed_stage(
        timings,
        "owner_cleanup",
        workers=int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "0")),
        path=tmp_path / "owner-target-timings.json",
    ):
        # Every test owns its successful replacement too. Await shutdown, assert the
        # loopback listener closes, then prove temporary files can be removed.
        report = target_info["state"] / "upgrade-report.json"
        if report.exists():
            info = json.loads(report.read_text(encoding="utf-8"))
            if info.get("owner_state") == "running_verified":
                child_env = {
                    **env,
                    "TELELOOM_DATA_DIR": str(data),
                    "TELELOOM_MCP_TOKEN": "private-fixture-token",
                }
                subprocess.run(
                    [str(python), "-I", "-X", "utf8", "-m", "teleloom", "stop"],
                    env=child_env,
                    cwd=data,
                    check=True,
                    capture_output=True,
                )
                from release_owner import wait_exit

                wait_exit(info["owner_pid"])
        with socket.socket() as sock:
            sock.settimeout(0.5)
            assert sock.connect_ex(("127.0.0.1", port)) != 0
        shutil.rmtree(target)
        assert not target.exists()


def upgrade_args(target):
    return [
        "upgrade",
        "--verified-release",
        target["verified"],
        "--python",
        target["launcher"],
        "--client",
        target["descriptor"],
        "--extras",
        "",
        "--previous-wheel",
        target["wheel"],
        "--previous-extras",
        "",
        "--state-dir",
        target["state"],
    ]


def test_active_jobs_block_owner_replacement(owner_target):
    from teleloom.store import Store

    target = owner_target
    store = Store(target["data"])
    store.put(
        "jobs",
        {"id": "active", "kind": "delivery", "status": "running", "private": "secret-content"},
    )
    store.db.commit()
    store.close()
    original = target["client"].read_bytes()
    assert invoke(upgrade_args(target)) == 1
    report = json.loads((target["state"] / "upgrade-report.json").read_text(encoding="utf-8"))
    assert report["error"] == "active_jobs"
    assert report["owner_state"] == "unchanged"
    assert target["client"].read_bytes() == original
    assert not (target["state"] / "baseline.json").exists()
    assert "secret-content" not in json.dumps(report)


def seed_history(target):
    from teleloom.store import Store

    store = Store(target["data"])
    store.put(
        "jobs",
        {"id": "historical", "kind": "delivery", "status": "needs_review", "profile_id": "fixture"},
    )
    store.put_delivery(
        "historical",
        0,
        {"status": "unknown", "text": "Секретный текст Telegram", "receipt": "stable-unknown"},
    )
    store.set_state("profile_generation:fixture", "generation-one")
    store.db.commit()
    store.close()


def test_upgrade_preserves_history_client_and_selected_skills_and_requires_native_reconnection(
    owner_target,
):
    target = owner_target
    seed_history(target)
    client = target["client"].read_bytes()
    unrelated = (target["skills"] / "teleloom-connect/SKILL.md").read_bytes()
    (target["skills"] / "teleloom-read/stale.txt").write_text("old extra file", encoding="utf-8")
    assert invoke(upgrade_args(target)) == 0
    report_file = target["state"] / "upgrade-report.json"
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["owner_state"] == "running_verified"
    assert report["native_client"]["status"] == "pending_reconnection"
    assert report["preservation_differences"] == []
    assert not (target["skills"] / "teleloom-read/stale.txt").exists()
    assert (target["state"] / "recovery/skills/teleloom-read/stale.txt").exists()
    assert target["client"].read_bytes() == client
    assert (target["skills"] / "teleloom-connect/SKILL.md").read_bytes() == unrelated
    baseline = target["state"] / "baseline.json"
    snapshot = json.loads(baseline.read_text(encoding="utf-8"))
    assert snapshot["snapshot"]["receipts"]["historical:0"]["status"] == "unknown"
    assert "Секретный" not in report_file.read_text(encoding="utf-8") + baseline.read_text(
        encoding="utf-8"
    )
    saved = report_file.read_bytes()
    assert invoke(upgrade_args(target)) == 1
    assert report_file.read_bytes() == saved
    assert (
        invoke(
            [
                "verify-owner",
                "--verified-release",
                target["verified"],
                "--python",
                target["launcher"],
                "--baseline",
                baseline,
            ]
        )
        == 0
    )


def test_running_owner_exits_and_native_evidence_cannot_be_replaced_by_http(owner_target, tmp_path):
    from release_common import fingerprint

    target = owner_target
    entry = json.loads(target["client"].read_text(encoding="utf-8"))["mcpServers"]["teleloom"]
    env = {**os.environ, **entry["env"]}
    old = subprocess.Popen(
        [str(target["python"]), "-I", "-X", "utf8", "-m", "teleloom", "serve"],
        env=env,
        cwd=target["data"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import time

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", target["port"])) == 0:
                    break
            assert old.poll() is None
            time.sleep(0.05)
        descriptor = json.loads(target["descriptor"].read_text(encoding="utf-8"))
        descriptor["owner_pid"] = old.pid
        target["descriptor"].write_text(json.dumps(descriptor), encoding="utf-8")
        assert invoke(upgrade_args(target)) == 0
        assert old.wait(timeout=5) == 0
    finally:
        if old.poll() is None:
            old.terminate()
        old.wait(timeout=10)
    report = json.loads((target["state"] / "upgrade-report.json").read_text(encoding="utf-8"))
    assert report["build"]["build_id"] == RELEASE_TAG
    baseline_path = target["state"] / "baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["previous_build"]["build_id"] == "previous-build"
    release = json.loads(target["verified"].read_text(encoding="utf-8"))
    observation = {
        "client": "teleloom",
        "reconnected": False,
        "baseline_sha256": fingerprint(baseline),
        "server_status": {"build": release["build"]},
        "schemas": release["inventory"]["schemas"],
        "reference": "fake-native-observation-for-test-only",
    }
    native_file = tmp_path / "native.json"
    native_file.write_text(json.dumps(observation), encoding="utf-8")
    arguments = [
        "verify-owner",
        "--verified-release",
        target["verified"],
        "--python",
        target["launcher"],
        "--baseline",
        baseline_path,
        "--native-observation",
        native_file,
    ]
    assert invoke(arguments) == 1
    observation["reconnected"] = True
    native_file.write_text(json.dumps(observation), encoding="utf-8")
    assert invoke(arguments) == 0
    report = json.loads((target["state"] / "verify-owner-report.json").read_text(encoding="utf-8"))
    assert report["native_client"]["status"] == "verified"


def test_stale_owner_is_reported_without_starting_or_replacing_it(owner_target):
    target = owner_target
    target["state"].mkdir()
    if os.name == "nt":
        subprocess.run(
            ["icacls", str(target["state"]), "/grant", "*S-1-1-0:(OI)(CI)R"],
            check=True,
            capture_output=True,
        )
    else:
        target["state"].chmod(0o755)
    assert invoke(upgrade_args(target) + ["--dry-run"]) == 0
    if os.name == "nt":
        access = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$taskAcl=[System.IO.Directory]::GetAccessControl($env:TEST_RELEASE_PRIVATE_DIR); "
                "$taskAcl.AreAccessRulesProtected; $taskAcl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]).IdentityReference.Value",
            ],
            env={**os.environ, "TEST_RELEASE_PRIVATE_DIR": str(target["state"])},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert "True" in access.stdout
        assert "S-1-1-0" not in access.stdout
    else:
        assert target["state"].stat().st_mode & 0o077 == 0
    assert not (target["state"] / "baseline.json").exists()
    with socket.socket() as stale:
        stale.bind(("127.0.0.1", target["port"]))
        stale.listen()
        assert invoke(upgrade_args(target)) == 1
    report = json.loads((target["state"] / "upgrade-report.json").read_text(encoding="utf-8"))
    assert report["error"] == "owner_unavailable"
    assert report["owner_state"] == "unchanged"
    assert not (target["state"] / "baseline.json").exists()


def test_inherited_credential_references_survive_without_exporting_values(
    owner_target, monkeypatch
):
    target = owner_target
    monkeypatch.setenv("TELELOOM_FIXTURE_SESSION", "private-inherited-session-value")
    assert invoke(upgrade_args(target)) == 0
    baseline_text = (target["state"] / "baseline.json").read_text(encoding="utf-8")
    baseline = json.loads(baseline_text)
    assert "TELELOOM_FIXTURE_SESSION" in baseline["snapshot"]["credential_references"]
    assert "private-inherited-session-value" not in baseline_text
    assert "private-fixture-token" not in baseline_text


def test_same_tooling_environment_alias_and_invalid_client_module_block_preflight(owner_target):
    target = owner_target
    args = upgrade_args(target)
    alias = Path(sys.executable).with_name("pythonw.exe" if os.name == "nt" else "python3")
    assert alias.exists()
    args[args.index("--python") + 1] = alias
    assert invoke(args) == 1
    report = json.loads((target["state"] / "upgrade-report.json").read_text(encoding="utf-8"))
    assert report["error"] == "tooling_is_target"
    config = json.loads(target["client"].read_text(encoding="utf-8"))
    config["mcpServers"]["teleloom"]["args"] = ["-m", "other_module", "teleloom", "mcp"]
    target["client"].write_text(json.dumps(config), encoding="utf-8")
    assert invoke(upgrade_args(target) + ["--dry-run"]) == 1
    report = json.loads(
        (target["state"] / "upgrade-dry-run-report.json").read_text(encoding="utf-8")
    )
    assert report["error"] == "client_launch_mismatch"
    assert not (target["state"] / "baseline.json").exists()


class FailingInstall:
    def install(self, python, wheel, chosen_extras, env, directory):
        raise OSError("token-and-private-content-must-not-escape")


class BrokenSkillTarget:
    def __init__(self, target):
        self.target = target

    def install(self, python, wheel, chosen_extras, env, directory):
        from release_owner import Packages

        Packages().install(python, wheel, chosen_extras, env, directory)
        selected = self.target["skills"] / "teleloom-read"
        shutil.rmtree(selected)
        selected.write_text("injected filesystem failure", encoding="utf-8")


class MissingEngine:
    def install(self, python, wheel, chosen_extras, env, directory):
        from release_owner import Packages

        Packages().install(python, wheel, [], env, directory)


class ChangedCapabilities:
    def install(self, python, wheel, chosen_extras, env, directory):
        from release_owner import Packages

        Packages().install(python, wheel, chosen_extras, env, directory)
        path = (
            subprocess.check_output(
                [
                    str(python),
                    "-I",
                    "-X",
                    "utf8",
                    "-c",
                    "import pathlib,teleloom; print(pathlib.Path(teleloom.__file__).with_name('runtime.py'))",
                ],
                env=env,
                cwd=directory,
            )
            .decode("utf-8")
            .strip()
        )
        runtime = Path(path)
        source = runtime.read_text(encoding="utf-8")
        assert '"telegram_history": p.kind == "user"' in source
        # Detach uv's hard-linked cache file before injecting a target-only failure.
        runtime.unlink()
        runtime.write_text(
            source.replace('"telegram_history": p.kind == "user"', '"telegram_history": False'),
            encoding="utf-8",
        )


@pytest.mark.parametrize(
    "failure,stage",
    [
        ("install", "install_pending"),
        ("skills", "skills_pending"),
        ("runtime", "runtime_verification_pending"),
        ("capabilities", "runtime_verification_pending"),
    ],
)
def test_failed_upgrade_preserves_recovery_and_does_not_repeat_installation(
    owner_target, failure, stage
):
    target = owner_target
    seed_history(target)
    arguments = upgrade_args(target)
    if failure == "install":
        packages = FailingInstall()
    elif failure == "skills":
        packages = BrokenSkillTarget(target)
    elif failure == "runtime":
        packages = MissingEngine()
        arguments[arguments.index("--extras") + 1] = "pdf"
    else:
        packages = ChangedCapabilities()
    assert invoke(arguments, packages=packages) == 1
    report_file = target["state"] / "upgrade-report.json"
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["completed_stage"] == stage
    assert report["owner_state"].startswith("stopped")
    if failure == "capabilities":
        assert report["preservation_differences"] == ["capabilities"]
    assert (target["state"] / "recovery" / target["wheel"].name).is_file()
    assert (target["state"] / "recovery/skills/teleloom-read/SKILL.md").is_file()
    assert report["recovery"]["database"].startswith("Preserve current database")
    assert "token-and-private" not in json.dumps(report)
    saved = report_file.read_bytes()
    assert invoke(arguments, packages=packages) == 1
    assert report_file.read_bytes() == saved
    client_env = json.loads(target["client"].read_text(encoding="utf-8"))["mcpServers"]["teleloom"][
        "env"
    ]
    recovery_env = {**os.environ, **client_env}
    subprocess.run(
        report["recovery"]["package_command"],
        env=recovery_env,
        cwd=target["data"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        report["recovery"]["restore_skills_command"],
        env=recovery_env,
        cwd=target["data"],
        check=True,
        capture_output=True,
    )
    assert (target["skills"] / "teleloom-read/SKILL.md").is_file()
    from release_owner import probe

    after = probe(target["python"], "snapshot", recovery_env, target["data"])
    before = json.loads((target["state"] / "baseline.json").read_text(encoding="utf-8"))["snapshot"]
    assert after["jobs"] == before["jobs"]
    assert after["receipts"] == before["receipts"]


@pytest.mark.parametrize("failure", [False, True])
def test_installed_smoke_awaits_process_port_and_temporary_state_cleanup(
    owner_target, tmp_path, failure
):
    target = owner_target
    lifetime = tmp_path / "lifetime.json"
    script = Path(__file__).resolve().parents[1] / "scripts/smoke_projection_package.py"
    command = [
        str(target["python"]),
        "-I",
        "-X",
        "utf8",
        str(script),
        "--lifetime-report",
        str(lifetime),
    ]
    if failure:
        command.append("--inject-failure")
    result = subprocess.run(
        command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=180
    )
    assert (result.returncode != 0) == failure, result.stderr
    observed = json.loads(lifetime.read_text(encoding="utf-8"))
    assert not Path(observed["directory"]).exists()
    with socket.socket() as sock:
        sock.settimeout(0.5)
        assert sock.connect_ex(("127.0.0.1", observed["port"])) != 0
    from release_owner import wait_exit

    wait_exit(observed["pid"])
    assert '"installed_artifact": "passed"' in result.stdout if not failure else not result.stdout
