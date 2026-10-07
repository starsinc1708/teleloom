"""Verify artifact versions/provenance and rebuilding without Git, through uv build."""

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path


def module_version(source: str) -> str:
    tree = ast.parse(source)
    return str(
        next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
        )
    )


def wheel_metadata(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as wheel:
        info = json.loads(wheel.read("teleloom/_build_info.json"))
        version = module_version(wheel.read("teleloom/__init__.py").decode())
        metadata = Parser().parsestr(
            wheel.read(
                next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
            ).decode()
        )
        assert version == metadata["Version"] == info["package_version"], "Wheel versions disagree"
        assert set(info) == {"package_version", "build_id", "source_commit", "build_type"}
        assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", info["build_id"])
        assert re.fullmatch(r"[a-f0-9]{40}|unknown", info["source_commit"])
        assert info["build_type"] in {"release", "local", "dev", "unknown"}
        return info


def build(root: Path, output: Path, *, no_git: bool = False) -> None:
    uv = shutil.which("uv")
    assert uv, "uv is required for artifact verification"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TELELOOM_BUILD_TYPE", "TELELOOM_BUILD_ID"}
    }
    if no_git:
        env["PATH"] = ""
    subprocess.run(
        [uv, "build", "--offline", "--python", sys.executable, "--out-dir", str(output), str(root)],
        env=env,
        check=True,
        capture_output=True,
        timeout=90,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    wheels = list(args.wheel_dir.glob("teleloom-*.whl"))
    sdists = list(args.wheel_dir.glob("teleloom-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        parser.error("Expected exactly one teleloom wheel and sdist in --wheel-dir.")
    wheel, sdist = wheels[0], sdists[0]
    original = wheel_metadata(wheel)
    with tempfile.TemporaryDirectory(prefix="teleloom-artifacts-") as directory:
        base = Path(directory)
        with tarfile.open(sdist) as archive:
            archive.extractall(base / "archive", filter="data")
        source = next((base / "archive").iterdir())
        archived = json.loads(
            (source / "src/teleloom/_build_info.json").read_text(encoding="utf-8")
        )
        assert archived == original, "Wheel/sdist provenance disagree"
        project_version = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
        pkg_info = Parser().parsestr((source / "PKG-INFO").read_text(encoding="utf-8"))
        assert project_version == pkg_info["Version"] == archived["package_version"], (
            "Sdist versions disagree"
        )
        build(source, base / "rebuilt", no_git=True)
        assert wheel_metadata(next((base / "rebuilt").glob("*.whl"))) == original, (
            "Archive rebuild lost provenance"
        )
        # An unstamped source archive has no known commit, even when Git is absent.
        (source / "src/teleloom/_build_info.json").unlink()
        build(source, base / "unknown", no_git=True)
        unknown = wheel_metadata(next((base / "unknown").glob("*.whl")))
        assert unknown["source_commit"] == "unknown"
        assert unknown["build_type"] == "local"
        assert unknown["build_id"] != original["build_id"]
        module = source / "src/teleloom/__init__.py"
        module.write_text(
            module.read_text(encoding="utf-8").replace(project_version, "99.99.99"),
            encoding="utf-8",
        )
        try:
            build(source, base / "mismatch", no_git=True)
        except subprocess.CalledProcessError as error:
            assert b"Package metadata and module version disagree" in error.stderr
        else:
            raise AssertionError("A version mismatch must refuse the build")
        print(
            json.dumps(
                {
                    "wheel_sdist": "passed",
                    "archive_rebuild_without_git": "passed",
                    "unstamped_archive": "unknown",
                    "version_mismatch_rejected": "passed",
                    "build": original,
                }
            )
        )


if __name__ == "__main__":
    main()
