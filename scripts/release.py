"""Prepare, publish, audit and upgrade one pinned release. See docs/release-pipeline.md."""

import argparse
import json
import re
import sys
from pathlib import Path

from release_common import (
    ReleaseError,
    private_directory,
    read_json,
    require,
    run,
    timed_stage,
    write_json,
)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="phase", required=True)
    for phase in ("prepare", "publish", "verify-assets"):
        command = sub.add_parser(phase)
        command.add_argument("--tag", required=True)
        command.add_argument("--commit", required=True)
        command.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
        if phase == "prepare":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--evidence", type=Path)
        else:
            command.add_argument("--artifacts", type=Path, required=True)
            command.add_argument("--repo", required=True)
        command.add_argument("--report", type=Path)
        command.add_argument("--dry-run", action="store_true")
    for phase in ("upgrade", "verify-owner"):
        command = sub.add_parser(phase)
        command.add_argument("--verified-release", type=Path, required=True)
        command.add_argument("--python", type=Path, required=True)
        command.add_argument("--baseline", type=Path, required=phase == "verify-owner")
        command.add_argument("--report", type=Path)
        command.add_argument("--native-observation", type=Path)
        command.add_argument("--dry-run", action="store_true")
        if phase == "upgrade":
            command.add_argument(
                "--client",
                type=Path,
                required=True,
                help="Explicit JSON target descriptor; see release documentation",
            )
            command.add_argument("--extras", required=True)
            command.add_argument("--previous-wheel", type=Path, required=True)
            command.add_argument("--previous-extras", required=True)
            command.add_argument("--state-dir", type=Path, required=True)
    return result


def main(argv=None, *, github=None, packages=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.phase == "prepare":
        path = args.report or args.output / "prepare-report.json"
    elif args.phase in {"publish", "verify-assets"}:
        path = args.report or args.artifacts.parent / f"{args.phase}-report.json"
    elif args.phase == "upgrade":
        path = args.report or args.state_dir / "upgrade-report.json"
    else:
        path = args.report or args.baseline.parent / "verify-owner-report.json"
    if args.dry_run and args.report is None:
        path = path.with_name(path.stem.removesuffix("-report") + "-dry-run-report.json")
    report = {
        "phase": args.phase,
        "status": "pending",
        "completed_stage": "initialized",
        "source_commit": getattr(args, "commit", "unknown"),
        "artifact_hashes": {},
    }
    report["report_path"] = str(path.resolve())
    protected = (
        args.phase == "upgrade" and (args.state_dir / "baseline.json").exists() and path.exists()
    )
    try:
        if args.phase == "upgrade":
            private_directory(args.state_dir)
        if args.phase in {"prepare", "publish", "verify-assets"}:
            require(bool(re.fullmatch(r"[a-f0-9]{40}", args.commit)), "unknown_source")
            require(bool(re.fullmatch(r"v\d+\.\d+\.\d+", args.tag)), "invalid_tag")
            report["tag"] = args.tag
        if args.phase == "prepare":
            source = args.source.resolve()
            with timed_stage(report, "source_validation"):
                require(
                    run(["git", "rev-parse", "HEAD"], cwd=source) == args.commit, "wrong_commit"
                )
                require(not run(["git", "status", "--porcelain"], cwd=source), "dirty_source")
            from release_artifacts import prepare

            prepare(args, report)
        elif args.phase in {"publish", "verify-assets"}:
            from release_artifacts import published

            published(args, report, github=github)
        else:
            from release_owner import owner_phase

            owner_phase(args, report, packages=packages)
        report["status"] = "dry_run" if args.dry_run else "passed"
    except Exception as error:
        report["status"] = "failed"
        # Never surface raw subprocess, SDK, config or SQLite error strings.
        report["error"] = str(error) if isinstance(error, ReleaseError) else "invalid_input"
    if protected:
        previous = read_json(path)
        report["completed_stage"] = previous["completed_stage"]
        report["owner_state"] = previous.get("owner_state", "unknown")
        report["pending_upgrade_report"] = str(path.resolve())
    else:
        write_json(path, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0 if report["status"] in {"passed", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
