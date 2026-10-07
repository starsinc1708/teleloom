"""Pinned build, exact asset verification and inspect-before-mutate publication."""

import json
import re
import shutil
import tarfile
import tempfile
import tomllib
from pathlib import Path

from check_build_metadata import module_version, wheel_metadata
from release_common import (
    current_python,
    digest,
    python_command,
    read_json,
    require,
    run,
    timed_stage,
    tooling_env,
    write_json,
)

GATES = {"build", "metadata", "inventory"}
LEGACY_GATES = {"ruff", "format", "mypy", "pytest", "skills", "build", "metadata", "smoke"}


def checkpoint(report, stage):
    report["completed_stage"] = stage
    write_json(report["report_path"], report)


def expected_build(tag, commit):
    return {
        "package_version": tag.removeprefix("v"),
        "build_id": tag,
        "source_commit": commit,
        "build_type": "release",
    }


def names(tag):
    version = tag.removeprefix("v")
    return {f"teleloom-{version}-py3-none-any.whl", f"teleloom-{version}.tar.gz", "SHA256SUMS"}


def validate_bytes(directory, tag, commit):
    expected = names(tag)
    files = list(directory.iterdir())
    require(
        {path.name for path in files} == expected
        and all(path.is_file() and not path.is_symlink() for path in files),
        "asset_set",
    )
    manifest = (directory / "SHA256SUMS").read_text(encoding="utf-8")
    entries = {}
    for line in manifest.splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9._-]+)", line)
        require(match is not None, "manifest_invalid")
        sha, name = match.groups()
        require(name in expected - {"SHA256SUMS"} and name not in entries, "manifest_invalid")
        entries[name] = sha
    require(set(entries) == expected - {"SHA256SUMS"}, "manifest_invalid")
    hashes = {path.name: digest(path) for path in files}
    require(all(hashes[name] == sha for name, sha in entries.items()), "hash_mismatch")
    wheel = next(directory.glob("*.whl"))
    build = expected_build(tag, commit)
    require(wheel_metadata(wheel) == build, "release_stamp")
    with tarfile.open(next(directory.glob("*.tar.gz"))) as archive:
        members = archive.getmembers()
        member_names = [member.name for member in members]
        require(len(member_names) == len(set(member_names)), "sdist_invalid")
        for member in members:
            parts = member.name.split("/")
            require(
                parts[0] == f"teleloom-{build['package_version']}"
                and all(part not in {"", ".", ".."} for part in parts)
                and not member.issym()
                and not member.islnk()
                and (member.isfile() or member.isdir()),
                "sdist_invalid",
            )
        prefix = f"teleloom-{build['package_version']}/"

        def read(name):
            file = archive.extractfile(prefix + name)
            require(file is not None, "sdist_invalid")
            return file.read().decode("utf-8")

        require(json.loads(read("src/teleloom/_build_info.json")) == build, "release_stamp")
        require(
            tomllib.loads(read("pyproject.toml"))["project"]["version"] == build["package_version"],
            "version_mismatch",
        )
        from email.parser import Parser

        require(
            Parser().parsestr(read("PKG-INFO"))["Version"] == build["package_version"],
            "version_mismatch",
        )
    return hashes


def validate_evidence(evidence, commit, tree, source=None):
    kinds = ("tests",) if "tests" in evidence else ("standards", "spec", "ci")
    for kind in kinds:
        item = evidence[kind]
        results = {"passed", "waived", "not_required"} if kind == "tests" else {"passed"}
        require(item["result"] in results and bool(item["reference"]), "evidence_missing")
        require(bool(re.fullmatch(r"[a-f0-9]{40}", item["commit"])), "evidence_source")
        require(item["tree"] == tree, "evidence_source")
        if source:
            require(
                run(["git", "rev-parse", f"{item['commit']}^{{tree}}"], cwd=source) == tree,
                "evidence_source",
            )
        if kind == "ci":
            require(
                item["commit"] == commit and set(item["platforms"]) == {"windows", "linux"},
                "evidence_source",
            )
        if kind == "tests":
            require(item["commit"] == commit, "evidence_source")


def prepared(directory, tag, commit):
    report = read_json(directory.parent / "prepare-report.json")
    require(report["phase"] == "prepare" and report["status"] == "passed", "not_prepared")
    require(report["source_commit"] == commit and report["tag"] == tag, "wrong_commit")
    require(report["build"] == expected_build(tag, commit), "release_stamp")
    require(
        set(report["gates"]) in (GATES, LEGACY_GATES)
        and all(value == "passed" for value in report["gates"].values()),
        "required_gate",
    )
    validate_evidence(report["evidence"], commit, report["source_tree"])
    require(validate_bytes(directory, tag, commit) == report["artifact_hashes"], "hash_mismatch")
    require(
        isinstance(report["inventory"]["schemas"], dict)
        and isinstance(report["inventory"]["skills"], dict),
        "inventory_missing",
    )
    if set(report["gates"]) == GATES:
        require(
            report["inventory"]["installed"] is True
            and report["inventory"]["build"] == report["build"],
            "release_stamp",
        )
    return report


def prepare(args, report):
    source = args.source.resolve()
    report["build"] = expected_build(args.tag, args.commit)
    report["source_tree"] = run(["git", "rev-parse", "HEAD^{tree}"], cwd=source)
    local_tag = run(["git", "tag", "--list", args.tag], cwd=source)
    if local_tag:
        require(
            run(["git", "rev-parse", f"{args.tag}^{{commit}}"], cwd=source) == args.commit,
            "tag_conflict",
        )
    version = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    module = module_version((source / "src/teleloom/__init__.py").read_text(encoding="utf-8"))
    lock = tomllib.loads((source / "uv.lock").read_text(encoding="utf-8"))
    locked = [package["version"] for package in lock["package"] if package["name"] == "teleloom"]
    require(version == module == args.tag[1:] and locked == [version], "version_mismatch")
    require(args.evidence is not None, "evidence_missing")
    report["evidence"] = read_json(args.evidence)
    validate_evidence(report["evidence"], args.commit, report["source_tree"], source)
    notes_path = f"docs/releases/{args.tag}.md"
    notes_entry = run(["git", "ls-tree", args.commit, "--", notes_path], cwd=source)
    require(
        notes_entry.startswith(("100644 blob ", "100755 blob "))
        and not (source / notes_path).is_symlink(),
        "notes_untracked",
    )
    report["release_notes_sha256"] = digest(source / notes_path)
    directory = args.output.resolve() / "artifacts"
    if args.dry_run:
        report["artifacts"] = str(directory)
        return
    # Never restamp/rebuild already prepared or partial artifact directories.
    if directory.exists():
        existing = prepared(directory, args.tag, args.commit)
        report.update(
            {key: existing[key] for key in ("artifact_hashes", "inventory", "gates", "artifacts")}
        )
        checkpoint(report, "prepared_reused")
        return
    checkpoint(report, "source_validated")
    directory.mkdir(parents=True)
    uv = shutil.which("uv")
    require(uv is not None, "uv_missing")
    report["gates"] = {}
    with tempfile.TemporaryDirectory(prefix="teleloom-release-source-") as temporary:
        checkout = Path(temporary) / "source"
        with timed_stage(report, "clone") as timing:
            run(["git", "clone", "--no-local", str(source), str(checkout)], timing=timing)
            run(["git", "checkout", "--detach", args.commit], cwd=checkout, timing=timing)
        env = tooling_env()
        env.update(TELELOOM_BUILD_TYPE="release", TELELOOM_BUILD_ID=args.tag)
        build_directory = Path(temporary) / "built"
        with timed_stage(report, "build") as timing:
            run(
                [uv, "build", "--out-dir", str(build_directory)],
                cwd=checkout,
                env=env,
                timing=timing,
            )
        built = list(build_directory.glob("*.whl")) + list(build_directory.glob("*.tar.gz"))
        require(len(built) == 2, "asset_set")
        for path in built:
            shutil.copy2(path, directory / path.name)
        report["gates"]["build"] = "passed"
        hashes = {path.name: digest(path) for path in directory.iterdir()}
        (directory / "SHA256SUMS").write_text(
            "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes)), encoding="utf-8"
        )
        checkpoint(report, "build")
        with timed_stage(report, "metadata"):
            report["artifact_hashes"] = validate_bytes(directory, args.tag, args.commit)
        report["gates"]["metadata"] = "passed"
        checkpoint(report, "metadata")
        command = python_command(
            current_python(),
            Path(__file__).with_name("check_installed_package.py"),
            "--wheel-dir",
            directory,
            "--inventory",
            args.output.resolve() / "inventory.json",
            "--inventory-only",
        )
        with timed_stage(report, "inventory") as timing:
            captured = run(command, env=tooling_env(), timing=timing)
        report["installed_timings"] = [
            json.loads(line) for line in captured.splitlines() if line.startswith("{")
        ]
    report["inventory"] = read_json(args.output / "inventory.json")
    require(
        report["inventory"]["installed"] is True
        and report["inventory"]["build"] == report["build"],
        "release_stamp",
    )
    report["gates"]["inventory"] = "passed"
    report["artifacts"] = str(directory)
    checkpoint(report, "prepared")


class GitHub:
    """Remote operations are a substitutable boundary; all byte checks stay local."""

    def tag_commit(self, repo, tag):
        refs = run(
            [
                "git",
                "ls-remote",
                f"https://github.com/{repo}.git",
                f"refs/tags/{tag}",
                f"refs/tags/{tag}^{{}}",
            ]
        )
        lines = [line.split() for line in refs.splitlines()]
        return next(
            (sha for sha, ref in lines if ref.endswith("^{}")), lines[0][0] if lines else None
        )

    def push_tag(self, source, repo, tag, commit):
        # Push the pinned object directly. No replacement of local or remote refs.
        run(
            ["git", "push", f"https://github.com/{repo}.git", f"{commit}:refs/tags/{tag}"],
            cwd=source,
        )

    def release(self, repo, tag):
        releases = json.loads(run(["gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases"]))
        matches = [item for page in releases for item in page if item["tag_name"] == tag]
        require(len(matches) <= 1, "release_conflict")
        return [asset["name"] for asset in matches[0]["assets"]] if matches else None

    def create(self, repo, tag, commit, assets, notes):
        run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                repo,
                "--verify-tag",
                "--notes-file",
                str(notes),
                *map(str, assets),
            ]
        )

    def download(self, repo, tag, destination):
        run(["gh", "release", "download", tag, "--repo", repo, "--dir", str(destination)])


def published(args, report, *, github=None):
    require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo)), "invalid_repo")
    directory = args.artifacts.resolve()
    local = prepared(directory, args.tag, args.commit)
    report.update(
        {
            key: local[key]
            for key in ("artifact_hashes", "build", "source_tree", "evidence", "inventory", "gates")
        }
    )
    report["repo"] = args.repo
    checkpoint(report, "prepared_validated")
    github = github or GitHub()
    remote = github.tag_commit(args.repo, args.tag)
    require(remote in {None, args.commit}, "tag_conflict")
    assets = github.release(args.repo, args.tag)
    if args.phase == "publish" and not args.dry_run:
        source = args.source.resolve()
        require(run(["git", "rev-parse", "HEAD"], cwd=source) == args.commit, "wrong_commit")
        require(not run(["git", "status", "--porcelain"], cwd=source), "dirty_source")
        notes = source / "docs/releases" / f"{args.tag}.md"
        require(digest(notes) == local["release_notes_sha256"], "notes_changed")
        if assets is None:
            if remote is None:
                checkpoint(report, "tag_push_pending")
                github.push_tag(source, args.repo, args.tag, args.commit)
                require(github.tag_commit(args.repo, args.tag) == args.commit, "tag_conflict")
            checkpoint(report, "tag_verified")
            checkpoint(report, "publication_pending")
            github.create(args.repo, args.tag, args.commit, sorted(directory.iterdir()), notes)
            assets = github.release(args.repo, args.tag)
        else:
            require(remote == args.commit, "tag_conflict")
    if args.dry_run:
        report["plan"] = {
            "tag": args.tag,
            "commit": args.commit,
            "assets": sorted(local["artifact_hashes"]),
            "existing_release": assets is not None,
        }
        return
    require(assets is not None and len(assets) == 3 and set(assets) == names(args.tag), "asset_set")
    require(github.tag_commit(args.repo, args.tag) == args.commit, "tag_conflict")
    checkpoint(report, "remote_inspected")
    downloads = Path(report["report_path"]).parent / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    # Fresh immutable download each time; keep it for the explicitly selected installation.
    download = Path(tempfile.mkdtemp(prefix=f"{args.tag}-", dir=downloads))
    github.download(args.repo, args.tag, download)
    downloaded = validate_bytes(download, args.tag, args.commit)
    require(downloaded == local["artifact_hashes"], "hash_mismatch")
    checkpoint(report, "download_verified")
    report["artifacts"] = str(download)
    report["prepare_report"] = str(directory.parent / "prepare-report.json")
    report["download_checks"] = "passed"
    checkpoint(report, "verified_download")
