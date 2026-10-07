"""Real temporary release sources and files, fake remote GitHub boundary only."""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.release_common import timed_stage, write_json

ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = (
    "v" + tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
)


def invoke(arguments, **operations):
    from scripts.release import main

    return main(list(map(str, arguments)), **operations)


@pytest.fixture(autouse=True)
def release_import_path(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))


class FakeGitHub:
    def __init__(self, artifacts, commit):
        self.assets = {path.name: path.read_bytes() for path in artifacts.iterdir()}
        self.commit = commit
        self.exists = True
        tag = json.loads((artifacts.parent / "prepare-report.json").read_text(encoding="utf-8"))[
            "tag"
        ]
        self.tags = {tag: commit}
        self.mutations = []
        self.interrupt_upload = False
        self.fail_download = False

    def tag_commit(self, repo, tag):
        return self.tags.get(tag)

    def push_tag(self, source, repo, tag, commit):
        self.mutations.append("tag")
        self.tags[tag] = commit

    def release(self, repo, tag):
        return list(self.assets) if self.exists else None

    def create(self, repo, tag, commit, assets, notes):
        self.mutations.append("release")
        self.exists = True
        if self.interrupt_upload:
            self.interrupt_upload = False
            raise OSError("credential-and-content-must-not-be-logged")
        self.assets = {path.name: path.read_bytes() for path in assets}

    def download(self, repo, tag, destination):
        if self.fail_download:
            raise OSError("private-download-error")
        for name, data in self.assets.items():
            (destination / name).write_bytes(data)


@pytest.fixture
def artifact_set(tmp_path):
    """Valid minimal archives suffice for integrity rejection before external smoke."""
    directory = tmp_path / "готовые байты"
    artifacts = directory / "artifacts"
    artifacts.mkdir(parents=True)
    commit = "a" * 40
    build = {
        "package_version": "0.2.1",
        "build_id": "v0.2.1",
        "source_commit": commit,
        "build_type": "release",
    }
    wheel = artifacts / "teleloom-0.2.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("teleloom/_build_info.json", json.dumps(build))
        archive.writestr("teleloom/__init__.py", '__version__ = "0.2.1"\n')
        archive.writestr("teleloom-0.2.1.dist-info/METADATA", "Version: 0.2.1\n")
    source = tmp_path / "archive-source" / "teleloom-0.2.1"
    (source / "src/teleloom").mkdir(parents=True)
    (source / "src/teleloom/_build_info.json").write_text(json.dumps(build), encoding="utf-8")
    (source / "pyproject.toml").write_text('[project]\nversion = "0.2.1"\n', encoding="utf-8")
    (source / "PKG-INFO").write_text("Version: 0.2.1\n", encoding="utf-8")
    with tarfile.open(artifacts / "teleloom-0.2.1.tar.gz", "w:gz") as archive:
        archive.add(source, arcname=source.name)
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts.iterdir()
    }
    (artifacts / "SHA256SUMS").write_text(
        "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes)), encoding="utf-8"
    )
    hashes["SHA256SUMS"] = hashlib.sha256((artifacts / "SHA256SUMS").read_bytes()).hexdigest()
    evidence = {
        name: {
            "commit": commit,
            "tree": "b" * 40,
            "result": "passed",
            "reference": "https://example.invalid/evidence",
        }
        for name in ("standards", "spec", "ci")
    }
    evidence["ci"]["platforms"] = ["windows", "linux"]
    report = {
        "phase": "prepare",
        "status": "passed",
        "tag": "v0.2.1",
        "source_commit": commit,
        "source_tree": "b" * 40,
        "artifact_hashes": hashes,
        "build": build,
        "evidence": evidence,
        "gates": {
            name: "passed"
            for name in ("ruff", "format", "mypy", "pytest", "skills", "build", "metadata", "smoke")
        },
        "inventory": {"schemas": {}, "skills": {}},
    }
    (directory / "prepare-report.json").write_text(json.dumps(report), encoding="utf-8")
    return artifacts, commit


@pytest.fixture(scope="session")
def real_release(tmp_path_factory):
    """Build a genuine stamped wheel/sdist in a real clean temporary repository."""
    reuse = os.environ.get("RELEASE_TEST_FIXTURE")
    if reuse:
        existing = json.loads((Path(reuse) / "fixture.json").read_text(encoding="utf-8"))
        return Path(existing["source"]), Path(reuse), existing["commit"]
    output = tmp_path_factory.mktemp("выпуск")
    timing_report = {}
    workers = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "0"))
    source = tmp_path_factory.mktemp("исходники release с пробелами")
    with timed_stage(
        timing_report, "copy_init", workers=workers, path=output / "fixture-timings.json"
    ):
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        files = {ROOT / name for name in tracked if name}
        files.update((ROOT / "scripts").glob("release*.py"))
        for path in files:
            if path.is_file():
                target = source / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "Release fixture",
            ],
            cwd=source,
            check=True,
            capture_output=True,
        )
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source).decode().strip()
        tree = (
            subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=source)
            .decode()
            .strip()
        )
        timing_report.update(source_commit=commit, source_tree=tree)
    evidence = {
        "tests": {
            "commit": commit,
            "tree": tree,
            "result": "waived",
            "reference": "fixture-only: outer pytest covers behavior; no nested suite",
        }
    }
    evidence_file = output / "evidence.json"
    evidence_file.write_text(json.dumps(evidence), encoding="utf-8")
    subprocess_run = subprocess.run
    builds = []

    def release_commands_only(command, *arguments, **options):
        assert not {"pytest", "ruff", "mypy", "sync"}.intersection(map(str, command))
        assert "check_build_metadata.py" not in {Path(str(arg)).name for arg in command}
        if "build" in command:
            builds.append(command)
        if "check_installed_package.py" in {Path(str(arg)).name for arg in command}:
            assert "--inventory-only" in command
        return subprocess_run(command, *arguments, **options)

    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(ROOT / "scripts"))
        patch.setattr(subprocess, "run", release_commands_only)
        result = invoke(
            [
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
                evidence_file,
            ]
        )
    report = json.loads((output / "prepare-report.json").read_text(encoding="utf-8"))
    assert result == 0, report
    assert report["status"] == "passed" and report["completed_stage"] == "prepared"
    assert len(builds) == 1 and report["artifact_hashes"]
    # Preserve the original preparation costs before reuse rewrites the public report.
    write_json(
        output / "prepare-timings.json",
        {
            key: report[key]
            for key in (
                "source_commit",
                "source_tree",
                "build",
                "timings",
                "installed_timings",
            )
        },
    )
    (output / "fixture.json").write_text(
        json.dumps({"source": str(source), "commit": commit}), encoding="utf-8"
    )
    return source, output, commit
