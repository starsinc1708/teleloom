import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from teleloom import control
from teleloom.config import Settings

CATALOGUE = [{"name": "messages_get", "summary": "Read", "writes": False}]
BASE = {"port": 8799, "read_timeout_seconds": 30, "profiles": {}, "exposure_mode": "all"}


class FakeLifecycle:
    def __init__(self):
        self.stopped = 0

    async def stop(self, settings):
        self.stopped += 1

    async def start(self, settings, handoff):
        raise AssertionError("client files never restart the owner")

    async def observed(self, settings):
        raise AssertionError("client files never touch the owner")


class FakeNative:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr("teleloom.cli.shutil.which", lambda name: name)
        self.config = tmp_path / "hermes home" / "profiles" / "work" / "config.yaml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            yaml.safe_dump(
                {"model": "owner-model", "mcp_servers": {"other": {"command": "other"}}}
            ),
            encoding="utf-8",
        )
        self.opencode = tmp_path / "opencode.json"
        self.opencode.write_text(
            json.dumps(
                {
                    "model": "owner-model",
                    "mcp": {"servers": {"other": {"type": "remote", "url": "https://x.test"}}},
                }
            ),
            encoding="utf-8",
        )
        self.calls: list[list[str]] = []
        self.behavior = "save"
        monkeypatch.setattr(subprocess, "run", self.run)

    def run(self, argv, **kwargs):
        assert isinstance(argv, list)
        assert kwargs.get("shell", False) is False
        assert kwargs.get("timeout") == 60
        self.calls.append(argv)
        client, *args = argv
        path = self.config if client == "hermes" else self.opencode
        current = (
            yaml.safe_load(path.read_text(encoding="utf-8"))
            if client == "hermes"
            else json.loads(path.read_text(encoding="utf-8"))
        )
        if args == ["config", "path"]:
            return subprocess.CompletedProcess(argv, 0, str(self.config) + "\n", "")
        if args == ["config", "get", "mcp_servers", "--json"]:
            if "mcp_servers" not in current:
                return subprocess.CompletedProcess(argv, 1, "", "Config key not set: mcp_servers")
            return subprocess.CompletedProcess(argv, 0, json.dumps(current["mcp_servers"]), "")
        if args == ["debug", "config"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"type": "document", "path": str(path), "info": current}]), ""
            )
        assert args[:3] == ["mcp", "add", "teleloom"]
        if self.behavior == "error":
            return subprocess.CompletedProcess(argv, 7, "", "synthetic-secret")
        env = dict([args[args.index("--env") + 1].split("=", 1)])
        if client == "opencode":
            assert "--global" in args
            current["mcp"]["servers"]["teleloom"] = {
                "type": "local",
                "command": args[args.index("--") + 1 :],
                "environment": env,
            }
        else:
            current.setdefault("mcp_servers", {})["teleloom"] = {
                "command": args[args.index("--command") + 1],
                "args": args[args.index("--args") + 1 :],
                "env": env,
                "enabled": True,
            }
        path.write_text(
            yaml.safe_dump(current) if client == "hermes" else json.dumps(current),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "Saved", "")


def make(tmp_path, lifecycle=None):
    (tmp_path / "config.json").write_text(json.dumps(BASE), encoding="utf-8")
    return control.Control(
        Settings.load(tmp_path), CATALOGUE, lifecycle=lifecycle or FakeLifecycle()
    )


async def client_for(instance):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=instance), base_url=instance.origin)


async def login(client, instance):
    fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
    response = await client.post(
        "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
    )
    assert response.status_code == 204


def csrf_of(body: str) -> str:
    return body.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def hash_of(body: str) -> str:
    return body.split('name="review_hash" value="', 1)[1].split('"', 1)[0]


def add_calls(native):
    return [call for call in native.calls if call[1:4] == ["mcp", "add", "teleloom"]]


@pytest.mark.asyncio
async def test_client_page_is_owner_only_and_review_writes_nothing(tmp_path, monkeypatch):
    native = FakeNative(tmp_path, monkeypatch)
    instance = make(tmp_path)
    target = tmp_path / "skills"
    before = (native.opencode.read_bytes(), native.config.read_bytes())
    try:
        async with await client_for(instance) as client:
            assert (await client.get("/control/clients")).status_code == 401
            await login(client, instance)
            review = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                    ("skills", "teleloom-send"),
                ],
            )
        assert review.status_code == 200
        assert str(target.resolve()) in review.text
        assert "teleloom-connect" in review.text and "teleloom-send" in review.text
        assert "Native invocation" in review.text and "no shell" in review.text
        assert str(native.opencode) in review.text  # effective client config file
        assert "teleloom-bridge" not in review.text  # only the selected skills
        assert add_calls(native) == []
        assert (native.opencode.read_bytes(), native.config.read_bytes()) == before
        assert not target.exists()

        # No CSRF and a foreign Origin never authorize a write.
        async with await client_for(instance) as client:
            await login(client, instance)
            denied = await client.post(
                "/control/clients/apply",
                data={"csrf": "wrong", "client": "opencode", "target": str(target)},
                headers={"Origin": instance.origin},
            )
            cross = await client.post(
                "/control/clients/apply",
                data={"csrf": "x", "client": "opencode"},
                headers={"Origin": "https://evil.invalid"},
            )
        assert denied.status_code == 403 and cross.status_code == 403
        assert add_calls(native) == [] and not target.exists()
    finally:
        instance.close()


@pytest.mark.asyncio
async def test_install_preserves_foreign_config_and_selected_skills_are_idempotent(
    tmp_path, monkeypatch
):
    native = FakeNative(tmp_path, monkeypatch)
    lifecycle = FakeLifecycle()
    instance = make(tmp_path, lifecycle)
    target = tmp_path / "skills"
    settings = Settings.load(tmp_path)
    try:
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                ],
            )
            response = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        assert response.status_code == 200, response.text
        assert "Saved the client connection" in response.text
        assert "Reconnect the client" in response.text and "unverified" in response.text
        assert [folder.name for folder in target.glob("teleloom-*")] == ["teleloom-connect"]
        saved = json.loads(native.opencode.read_text(encoding="utf-8"))
        assert saved["model"] == "owner-model"
        assert saved["mcp"]["servers"]["other"]["url"] == "https://x.test"
        entry = saved["mcp"]["servers"]["teleloom"]
        assert entry["command"] == [sys.executable, "-m", "teleloom", "mcp"] or (
            Path(entry["command"][0]).name.lower() in {"python.exe", "pythonw.exe"}
            and entry["command"][1:] == ["-m", "teleloom", "mcp"]
        )
        assert entry["environment"]["TELELOOM_DATA_DIR"] == str(settings.data_dir)
        added = add_calls(native)
        assert len(added) == 1
        assert added[0][-3:] == ["-m", "teleloom", "mcp"]
        assert "--global" in added[0] and "--env" in added[0]

        # Repeating the identical install writes no second entry and replaces no file.
        before = native.opencode.read_bytes()
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                ],
            )
            again = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        assert again.status_code == 200 and "already identical" in again.text
        assert "already present: teleloom-connect" in again.text
        assert len(add_calls(native)) == 1 and native.opencode.read_bytes() == before

        # A different entry is preserved and refused, never silently replaced.
        changed = json.loads(before)
        changed["mcp"]["servers"]["teleloom"]["command"][2] = "other-module"
        native.opencode.write_text(json.dumps(changed), encoding="utf-8")
        before = native.opencode.read_bytes()
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[("review", "1"), ("client", "opencode"), ("target", str(target))],
            )
            conflict = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "opencode",
                    "target": str(target),
                    "skills": [],
                },
                headers={"Origin": instance.origin},
            )
        assert conflict.status_code == 409 and "client_config_conflict" in conflict.text
        assert native.opencode.read_bytes() == before and len(add_calls(native)) == 1
        assert lifecycle.stopped == 0
    finally:
        instance.close()


@pytest.mark.asyncio
async def test_existing_skills_need_explicit_overwrite_and_foreign_files_survive(
    tmp_path, monkeypatch
):
    native = FakeNative(tmp_path, monkeypatch)
    instance = make(tmp_path)
    target = tmp_path / "skills"
    owned = target / "teleloom-connect" / "SKILL.md"
    owned.parent.mkdir(parents=True)
    owned.write_text("owner workflow", encoding="utf-8")
    foreign = target / "other-skill" / "SKILL.md"
    foreign.parent.mkdir()
    foreign.write_text("not teleloom", encoding="utf-8")
    note = target / "teleloom-connect" / "notes.md"
    note.write_text("keep me", encoding="utf-8")
    try:
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                ],
            )
            assert "Replace only the supplied files" in page.text
            refused = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        assert refused.status_code == 409 and "skills_exist" in refused.text
        assert owned.read_text(encoding="utf-8") == "owner workflow"
        assert add_calls(native) == []  # skills conflict is checked before the native install

        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                ],
            )
            replaced = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                    "overwrite": "1",
                },
                headers={"Origin": instance.origin},
            )
        assert replaced.status_code == 200, replaced.text
        assert owned.read_text(encoding="utf-8") != "owner workflow"
        assert note.read_text(encoding="utf-8") == "keep me"  # supplied files only
        assert foreign.read_text(encoding="utf-8") == "not teleloom"
        assert len(add_calls(native)) == 1
    finally:
        instance.close()


@pytest.mark.asyncio
async def test_native_failure_and_unsupported_clients_fail_closed(tmp_path, monkeypatch):
    native = FakeNative(tmp_path, monkeypatch)
    instance = make(tmp_path)
    target = tmp_path / "skills"
    native.behavior = "error"
    before = (native.opencode.read_bytes(), native.config.read_bytes())
    try:
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                ],
            )
            failed = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        assert failed.status_code == 422
        assert "client_install_failed" in failed.text and "synthetic-secret" not in failed.text
        assert (native.opencode.read_bytes(), native.config.read_bytes()) == before
        assert not target.exists()

        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients", params=[("review", "1"), ("client", "codex")]
            )
            assert "export only" in page.text and "mcp_servers" in page.text
            csrf_page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("skills", "teleloom-connect"),
                ],
            )
            unsupported = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(csrf_page.text),
                    "client": "codex",
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        assert unsupported.status_code == 422 and "client_install_unsupported" in unsupported.text
        assert "teleloom-connect" not in [p.name for p in tmp_path.iterdir()]
    finally:
        instance.close()


@pytest.mark.asyncio
async def test_hermes_uses_native_config_path_and_explicit_target_wins(tmp_path, monkeypatch):
    native = FakeNative(tmp_path, monkeypatch)
    instance = make(tmp_path)
    destination = native.config.parent / "skills"
    try:
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "hermes"),
                    ("skills", "teleloom-connect"),
                ],
            )
            assert str(destination.resolve()) in page.text
            assert str(native.config) in page.text
            assert str(tmp_path / ".hermes" / "skills") not in page.text
            installed = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "hermes",
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        assert installed.status_code == 200, installed.text
        saved = yaml.safe_load(native.config.read_text(encoding="utf-8"))
        assert (
            saved["model"] == "owner-model" and saved["mcp_servers"]["other"]["command"] == "other"
        )
        assert saved["mcp_servers"]["teleloom"]["args"] == ["-m", "teleloom", "mcp"]
        assert (destination / "teleloom-connect" / "SKILL.md").is_file()

        native.calls.clear()
        explicit = tmp_path / "explicit"
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[("review", "1"), ("client", "hermes"), ("target", str(explicit))],
            )
        assert str(explicit.resolve()) in page.text
        # The native install still writes the client's real config, so its path stays shown.
        assert str(native.config) in page.text
        assert ["hermes", "config", "path"] in native.calls
    finally:
        instance.close()


@pytest.mark.asyncio
async def test_review_shows_effective_config_path_and_redacted_entry_diff(tmp_path, monkeypatch):
    native = FakeNative(tmp_path, monkeypatch)
    instance = make(tmp_path)
    target = tmp_path / "skills"
    try:
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                ],
            )
            await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        saved = json.loads(native.opencode.read_text(encoding="utf-8"))
        entry = saved["mcp"]["servers"]["teleloom"]
        entry["environment"]["TELELOOM_TOKEN"] = "hunter2"
        entry["enabled"] = True
        native.opencode.write_text(json.dumps(saved), encoding="utf-8")
        async with await client_for(instance) as client:
            await login(client, instance)
            review = await client.get(
                "/control/clients",
                params=[("review", "1"), ("client", "opencode"), ("target", str(target))],
            )
        assert str(native.opencode) in review.text
        assert "Entry diff" in review.text
        assert "environment.TELELOOM_TOKEN" in review.text
        assert "extra" in review.text and "hunter2" not in review.text
        assert "value hidden" in review.text
        assert "Preserved existing settings: enabled" in review.text
        # Only the teleloom entry is described; foreign settings stay unexposed.
        assert "owner-model" not in review.text and "https://x.test" not in review.text
    finally:
        instance.close()


@pytest.mark.asyncio
async def test_stale_or_unreviewed_install_is_refused_and_rereview_recovers(tmp_path, monkeypatch):
    native = FakeNative(tmp_path, monkeypatch)
    instance = make(tmp_path)
    target = tmp_path / "skills"
    try:
        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                ],
            )
            csrf, stale_hash = csrf_of(page.text), hash_of(page.text)
            changed = json.loads(native.opencode.read_text(encoding="utf-8"))
            changed["model"] = "changed-after-review"
            native.opencode.write_text(json.dumps(changed), encoding="utf-8")
            before = native.opencode.read_bytes()
            stale = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf,
                    "review_hash": stale_hash,
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
            unreviewed = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf,
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        assert stale.status_code == 409 and "review_stale" in stale.text
        assert unreviewed.status_code == 409 and "review_required" in unreviewed.text
        assert add_calls(native) == [] and not target.exists()
        assert native.opencode.read_bytes() == before

        async with await client_for(instance) as client:
            await login(client, instance)
            page = await client.get(
                "/control/clients",
                params=[
                    ("review", "1"),
                    ("client", "opencode"),
                    ("target", str(target)),
                    ("skills", "teleloom-connect"),
                ],
            )
            recovered = await client.post(
                "/control/clients/apply",
                data={
                    "csrf": csrf_of(page.text),
                    "review_hash": hash_of(page.text),
                    "client": "opencode",
                    "target": str(target),
                    "skills": ["teleloom-connect"],
                },
                headers={"Origin": instance.origin},
            )
        assert recovered.status_code == 200, recovered.text
        assert len(add_calls(native)) == 1
        assert json.loads(native.opencode.read_text(encoding="utf-8"))["model"] == (
            "changed-after-review"
        )
    finally:
        instance.close()
