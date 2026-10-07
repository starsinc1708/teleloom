"""Release workflows at the public command boundary, with real temporary source."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release_common import parse_pytest_durations
from tests.release_fixtures import RELEASE_TAG, FakeGitHub, invoke

pytestmark = [pytest.mark.xdist_group("release"), pytest.mark.release_e2e, pytest.mark.process_e2e]

ROOT = Path(__file__).resolve().parents[1]


def test_prepare_refuses_dirty_source_and_writes_a_pinned_failure(tmp_path):
    source = tmp_path / "исходники с пробелом"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    (source / "tracked").write_text("original", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=source,
        check=True,
        capture_output=True,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source).decode().strip()
    (source / "tracked").write_text("изменено", encoding="utf-8")
    output = tmp_path / "release"
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts/release.py"),
            "prepare",
            "--source",
            str(source),
            "--tag",
            "v0.2.1",
            "--commit",
            commit,
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads((output / "prepare-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["source_commit"] == commit
    assert report["completed_stage"] == "initialized"
    assert report["error"] == "dirty_source"
    assert report["artifact_hashes"] == {}
    timing = report["timings"][0]
    assert timing["stage"] == "source_validation" and timing["exit"] == 1
    assert timing["seconds"] > 0 and timing["source_commit"] == commit
    assert "изменено" not in json.dumps(timing, ensure_ascii=False)
    node = "tests/test_release_owner.py::test_active_jobs_block_owner_replacement"
    assert parse_pytest_durations(f"1.25s call     {node}[private-fixture-value]@release") == [
        {"seconds": 1.25, "phase": "call", "test": node}
    ]
    assert parse_pytest_durations(f"1.25s setup    {node}@release")[0]["test"] == node


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "path", "digest", "modified"])
def test_verify_assets_refuses_untrusted_manifest_or_bytes(artifact_set, change):
    artifacts, commit = artifact_set
    github = FakeGitHub(artifacts, commit)
    manifest = github.assets["SHA256SUMS"]
    wheel = next(name for name in github.assets if name.endswith(".whl"))
    if change == "missing":
        del github.assets[wheel]
    elif change == "extra":
        github.assets["surprise.txt"] = b"extra"
    elif change == "duplicate":
        github.assets["SHA256SUMS"] = manifest + manifest.splitlines(keepends=True)[0]
    elif change == "path":
        github.assets["SHA256SUMS"] = manifest.replace(wheel.encode(), b"../escape.whl")
    elif change == "digest":
        github.assets["SHA256SUMS"] = manifest.replace(manifest[:64], b"z" * 64)
    else:
        github.assets[wheel] += b"modified"
    assert (
        invoke(
            [
                "verify-assets",
                "--tag",
                "v0.2.1",
                "--commit",
                commit,
                "--artifacts",
                artifacts,
                "--repo",
                "owner/repo",
            ],
            github=github,
        )
        == 1
    )
    report = json.loads(
        (artifacts.parent / "verify-assets-report.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "failed"
    assert report["error"] in {"asset_set", "manifest_invalid", "hash_mismatch"}
    assert report["source_commit"] == commit
    assert report["artifact_hashes"]
    assert not github.mutations


def test_release_cli_produces_utf8_even_with_legacy_python_environment(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts/release.py"),
            "prepare",
            "--tag",
            "v0.2.1",
            "--commit",
            "unknown",
            "--output",
            str(tmp_path / "русский путь"),
        ],
        env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1251"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "unknown_source"
    assert "русский путь" in result.stdout


def test_prepare_and_interrupted_publication_are_reused_without_rebuilding(real_release, tmp_path):
    source, output, commit = real_release
    artifacts = output / "artifacts"
    prepared = json.loads((output / "prepare-report.json").read_text(encoding="utf-8"))
    assert set(prepared["artifact_hashes"]) == {
        f"teleloom-{RELEASE_TAG.removeprefix('v')}-py3-none-any.whl",
        f"teleloom-{RELEASE_TAG.removeprefix('v')}.tar.gz",
        "SHA256SUMS",
    }
    status_schema = prepared["inventory"]["schemas"]["server_status"]
    assert set(status_schema["parameters"]) == {"profile_id", "chat_id", "timeout_seconds"}
    assert status_schema["required"] == []
    stages = {item["stage"]: item for item in prepared["timings"]}
    assert set(stages) == {
        "source_validation",
        "clone",
        "build",
        "metadata",
        "inventory",
    }
    assert all(item["exit"] == 0 and item["seconds"] > 0 for item in stages.values())
    assert all(item["source_commit"] == commit for item in stages.values())
    assert prepared["evidence"]["tests"]["result"] == "waived"
    assert prepared["inventory"]["build"] == prepared["build"]
    assert prepared["inventory"]["installed"] is True
    assert prepared["installed_timings"]
    assert {item["stage"] for item in prepared["installed_timings"]} == {
        "installed_venv",
        "installed_install",
        "installed_inventory",
        "installed_cleanup",
    }
    original_timings = (output / "prepare-timings.json").read_bytes()
    assert json.loads(original_timings)["timings"] == prepared["timings"]
    original = {path.name: path.read_bytes() for path in artifacts.iterdir()}
    args = [
        "prepare",
        "--source",
        source,
        "--tag",
        RELEASE_TAG,
        "--commit",
        commit,
        "--output",
        output,
        "--evidence",
        output / "evidence.json",
    ]
    assert invoke(args) == 0
    assert {path.name: path.read_bytes() for path in artifacts.iterdir()} == original
    assert (output / "prepare-timings.json").read_bytes() == original_timings
    github = FakeGitHub(artifacts, commit)
    github.exists = False
    github.tags = {}
    github.interrupt_upload = True
    report_path = tmp_path / "publication.json"
    args = [
        "publish",
        "--source",
        source,
        "--tag",
        RELEASE_TAG,
        "--commit",
        commit,
        "--artifacts",
        artifacts,
        "--repo",
        "owner/repo",
        "--report",
        report_path,
    ]
    assert invoke(args, github=github) == 1
    interrupted = json.loads(report_path.read_text(encoding="utf-8"))
    assert interrupted["completed_stage"] == "publication_pending"
    assert "credential-and-content" not in report_path.read_text(encoding="utf-8")
    assert invoke(args, github=github) == 0
    assert github.mutations == ["tag", "release"]
    verified = json.loads(report_path.read_text(encoding="utf-8"))
    assert verified["completed_stage"] == "verified_download"
    assert verified["artifact_hashes"] == prepared["artifact_hashes"]
    assert Path(verified["artifacts"]) != artifacts


@pytest.mark.parametrize("change", ["gate", "evidence", "stamp", "commit"])
def test_publication_blocks_invalid_preparation_evidence(artifact_set, change):
    artifacts, commit = artifact_set
    file = artifacts.parent / "prepare-report.json"
    prepared = json.loads(file.read_text(encoding="utf-8"))
    if change == "gate":
        prepared["gates"]["pytest"] = "failed"
    elif change == "evidence":
        prepared["evidence"]["ci"]["commit"] = "c" * 40
    elif change == "stamp":
        prepared["build"]["build_type"] = "local"
    else:
        prepared["source_commit"] = "unknown"
    file.write_text(json.dumps(prepared), encoding="utf-8")
    github = FakeGitHub(artifacts, commit)
    assert (
        invoke(
            [
                "publish",
                "--tag",
                "v0.2.1",
                "--commit",
                commit,
                "--artifacts",
                artifacts,
                "--repo",
                "owner/repo",
            ],
            github=github,
        )
        == 1
    )
    assert not github.mutations


@pytest.mark.parametrize("changed", ["module", "lock", "tag", "evidence", "unknown", "notes"])
def test_prepare_gates_versions_tags_and_evidence_before_building(tmp_path, changed):
    source = tmp_path / "source"
    (source / "src/teleloom").mkdir(parents=True)
    (source / "docs/releases").mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nversion="0.2.1"\n', encoding="utf-8")
    (source / "uv.lock").write_text(
        '[[package]]\nname="teleloom"\nversion="0.2.1"\n', encoding="utf-8"
    )
    module = source / "src/teleloom/__init__.py"
    module.write_text('__version__ = "0.2.1"\n', encoding="utf-8")
    (source / "docs/releases/v0.2.1.md").write_text("Заметки", encoding="utf-8")
    if changed == "notes":
        (source / ".gitignore").write_text("docs/releases/*.md\n", encoding="utf-8")
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)

    def commit_source():
        subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "fixture",
            ],
            cwd=source,
            check=True,
            capture_output=True,
        )
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source).decode().strip()

    reviewed = commit_source()
    if changed == "module":
        module.write_text('__version__ = "0.2.2"\n', encoding="utf-8")
    if changed == "lock":
        (source / "uv.lock").write_text(
            '[[package]]\nname="teleloom"\nversion="0.2.2"\n', encoding="utf-8"
        )
    if changed == "tag":
        subprocess.run(["git", "tag", "v0.2.1"], cwd=source, check=True)
    commit = commit_source()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=source).decode().strip()
    evidence = {
        kind: {"commit": commit, "tree": tree, "result": "passed", "reference": "fixture"}
        for kind in ("standards", "spec", "ci")
    }
    evidence["ci"]["platforms"] = ["windows", "linux"]
    if changed == "evidence":
        evidence["standards"]["tree"] = "f" * 40
    file = tmp_path / "evidence.json"
    file.write_text(json.dumps(evidence), encoding="utf-8")
    output = tmp_path / "output"
    expected = {
        "module": "version_mismatch",
        "lock": "version_mismatch",
        "tag": "tag_conflict",
        "evidence": "evidence_source",
        "unknown": "unknown_source",
        "notes": "notes_untracked",
    }[changed]
    assert (
        invoke(
            [
                "prepare",
                "--source",
                source,
                "--tag",
                "v0.2.1",
                "--commit",
                commit if changed != "unknown" else "unknown",
                "--output",
                output,
                "--evidence",
                file,
            ]
        )
        == 1
    )
    report = json.loads((output / "prepare-report.json").read_text(encoding="utf-8"))
    assert report["error"] == expected
    assert not (output / "artifacts").exists()
    if changed in {"evidence", "tag"}:
        subprocess.run(["git", "tag", "-d", "v0.2.1"], cwd=source, capture_output=True)
        evidence["standards"].update(commit=reviewed, tree=tree)
        evidence["spec"].update(commit=reviewed, tree=tree)
        file.write_text(json.dumps(evidence), encoding="utf-8")
        assert (
            invoke(
                [
                    "prepare",
                    "--source",
                    source,
                    "--tag",
                    "v0.2.1",
                    "--commit",
                    commit,
                    "--output",
                    output,
                    "--evidence",
                    file,
                    "--dry-run",
                ]
            )
            == 0
        )
        report = json.loads((output / "prepare-dry-run-report.json").read_text(encoding="utf-8"))
        assert report["source_commit"] == commit != report["evidence"]["standards"]["commit"]


def test_download_failure_is_reported_without_republishing(artifact_set):
    artifacts, commit = artifact_set
    github = FakeGitHub(artifacts, commit)
    github.fail_download = True
    assert (
        invoke(
            [
                "verify-assets",
                "--tag",
                "v0.2.1",
                "--commit",
                commit,
                "--artifacts",
                artifacts,
                "--repo",
                "owner/repo",
            ],
            github=github,
        )
        == 1
    )
    report = json.loads(
        (artifacts.parent / "verify-assets-report.json").read_text(encoding="utf-8")
    )
    assert report["completed_stage"] == "remote_inspected"
    assert "private-download-error" not in json.dumps(report)
    assert not github.mutations


@pytest.mark.parametrize(
    "decision", ["passed", "waived", "not_required", "failed", "not_run", "stale", "no_reference"]
)
def test_verify_assets_requires_bound_test_result_or_explicit_waiver(artifact_set, decision):
    artifacts, commit = artifact_set
    file = artifacts.parent / "prepare-report.json"
    prepared = json.loads(file.read_text(encoding="utf-8"))
    prepared["gates"] = {name: "passed" for name in ("build", "metadata", "inventory")}
    item = {key: prepared["evidence"]["ci"][key] for key in ("commit", "tree", "reference")}
    item["result"] = decision if decision not in {"stale", "no_reference"} else "waived"
    if decision == "stale":
        item["commit"] = "c" * 40
    if decision == "no_reference":
        item["reference"] = ""
    prepared["evidence"] = {"tests": item}
    prepared["inventory"].update(build=prepared["build"], installed=True)
    file.write_text(json.dumps(prepared), encoding="utf-8")
    github = FakeGitHub(artifacts, commit)
    result = invoke(
        [
            "verify-assets",
            "--tag",
            "v0.2.1",
            "--commit",
            commit,
            "--artifacts",
            artifacts,
            "--repo",
            "owner/repo",
        ],
        github=github,
    )
    accepted = decision in {"passed", "waived", "not_required"}
    assert result == (0 if accepted else 1)
    report = json.loads(
        (artifacts.parent / "verify-assets-report.json").read_text(encoding="utf-8")
    )
    if accepted:
        assert report["evidence"]["tests"]["result"] == item["result"]
    else:
        assert report["error"] in {"evidence_missing", "evidence_source"}
    assert not github.mutations


def test_conflicting_remote_tag_is_rejected_before_mutation(artifact_set):
    artifacts, commit = artifact_set
    github = FakeGitHub(artifacts, commit)
    github.tags["v0.2.1"] = "f" * 40
    assert (
        invoke(
            [
                "publish",
                "--tag",
                "v0.2.1",
                "--commit",
                commit,
                "--artifacts",
                artifacts,
                "--repo",
                "owner/repo",
            ],
            github=github,
        )
        == 1
    )
    report = json.loads((artifacts.parent / "publish-report.json").read_text(encoding="utf-8"))
    assert report["error"] == "tag_conflict"
    assert not github.mutations
