"""Explicit, hash-checked recovery of selected backed-up skills; never restore SQLite."""

import argparse
import shutil
from pathlib import Path

from release_common import ReleaseError, executable_python, read_json, require
from release_owner import probe, skills_hashes, target_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    baseline = read_json(args.baseline)
    target = baseline["target"]
    python = executable_python(baseline["python"])
    env = target_environment(target, baseline["python"])
    data = Path(target["data_dir"])
    require(
        probe(python, "inspect", env, data)["mcp"]["daemon"]["status"] == "not_running",
        "stop_owner_before_recovery",
    )
    from filelock import FileLock

    with FileLock(data / "owner.lock", timeout=0):
        require(probe(python, "snapshot", env, data)["active_jobs"] == 0, "active_jobs")
        root = Path(target["skills_dir"]).resolve()
        recovery = args.baseline.parent / "recovery/skills"
        for name, expected in baseline["previous_skills"].items():
            source = recovery / name
            destination = root / name
            require(
                destination.resolve().parent == root and not destination.is_symlink(),
                "skill_target",
            )
            require(skills_hashes(source) == expected, "recovery_bytes_changed")
        for name in baseline["previous_skills"]:
            destination = root / name
            if destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
            shutil.copytree(recovery / name, destination)
    print('{"restored": "selected_skills", "database": "preserved"}')


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            '{"status": "failed", "error": "'
            + (str(error) if isinstance(error, ReleaseError) else "recovery_failed")
            + '"}'
        )
        raise SystemExit(1) from None
