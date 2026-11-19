import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from teleloom import control
from teleloom.build_info import build_info
from teleloom.config import Settings
from teleloom.daemon import ensure_daemon
from teleloom.models import TeleloomError
from teleloom.store import Store

TOKEN = "mcp-bearer-that-must-never-authorize-management"
CATALOGUE = [
    {"name": "chat_resolve", "summary": "Resolve a peer", "writes": False},
    {"name": "messages_get", "summary": "Read messages", "writes": False},
    {"name": "send_message", "summary": "Send", "writes": True},
]
BASE = {
    "port": 8799,
    "read_timeout_seconds": 30,
    "profiles": {},
    "exposure_mode": "all",
    "exposed_tools": [],
    "future_setting": {"kept": "exactly"},
}


class FakeLifecycle:
    def __init__(
        self, *, fail_stop: bool = False, fail_start: bool = False, owner: str = "owner-1"
    ):
        self.stopped = 0
        self.started: list[str] = []
        self.fail_stop, self.fail_start, self.owner = fail_stop, fail_start, owner

    async def stop(self, settings: Settings) -> None:
        if self.fail_stop:
            raise TeleloomError("stop_failed", "injected stop failure")
        self.stopped += 1

    async def start(self, settings: Settings, handoff: str) -> None:
        self.started.append(handoff)
        if self.fail_start:
            raise TeleloomError("daemon_start_failed", "injected start failure")

    async def observed(self, settings: Settings) -> dict[str, Any]:
        # What the restarted owner reports: its own config projection plus its registered tools.
        _, data = control.read_config(settings)
        mode = data.get("exposure_mode", "all")
        selected = sorted(data.get("exposed_tools") or []) if mode == "selected" else []
        exposed = (
            [item["name"] for item in CATALOGUE]
            if mode == "all"
            else selected
            if mode == "selected"
            else [item["name"] for item in CATALOGUE if not item["writes"]]
        )
        return {
            "service": "teleloom",
            "owner_id": self.owner,
            "workspace_id": control.workspace_id(settings),
            "version": "0.5.0",
            "build": build_info(),
            "exposure": {"mode": mode, "selected_tools": selected},
            "tools": sorted(exposed),
        }


def config_file(tmp_path, data=None):
    (tmp_path / "config.json").write_text(json.dumps(data or BASE), encoding="utf-8")
    return tmp_path / "config.json"


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_control(tmp_path, lifecycle=None) -> control.Control:
    settings = Settings.load(tmp_path)
    return control.Control(settings, CATALOGUE, lifecycle=lifecycle or FakeLifecycle())


async def login(client: httpx.AsyncClient, instance: control.Control) -> None:
    fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
    response = await client.post(
        "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
    )
    assert response.status_code == 204, response.text


async def open_page(tmp_path, instance):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=instance), base_url=instance.origin)


def csrf_of(body: str) -> str:
    return body.split('name="csrf" value="', 1)[1].split('"', 1)[0]


async def apply(client, csrf: str, revision: str, mode: str, tools: list[str]):
    data = {"csrf": csrf, "revision": revision, "mode": mode, "tools": tools}
    return await client.post(
        "/control/apply", data=data, headers={"Origin": str(client.base_url).rstrip("/")}
    )


@pytest.mark.asyncio
async def test_management_boundary_is_owner_only_and_never_the_mcp_bearer(tmp_path):
    config_file(tmp_path)
    instance = make_control(tmp_path)
    assert instance.origin != f"http://127.0.0.1:{Settings.load(tmp_path).port}"
    async with await open_page(tmp_path, instance) as client:
        locked = await client.get("/control")
        assert locked.status_code == 401
        assert "teleloom control" in locked.text
        assert instance.cookie not in locked.headers.get("set-cookie", "")

        # A bearer or any request header is not a management credential.
        bearer = await client.post(
            "/control/session",
            json={"bootstrap": "x" * 43},
            headers={"Authorization": "Bearer " + TOKEN, "Origin": instance.origin},
        )
        assert bearer.status_code == 401
        assert (await client.get("/control", headers={"Host": "localhost"})).status_code == 403

        fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
        rejected = await client.post(
            "/control/session",
            json={"bootstrap": fragment},
            headers={"Origin": "https://evil.invalid"},
        )
        assert rejected.status_code == 403
        accepted = await client.post(
            "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
        )
        assert accepted.status_code == 204
        cookie = accepted.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/control" in cookie
        assert TOKEN not in cookie
        replay = await client.post(
            "/control/session",
            json={"bootstrap": fragment},
            headers={"Origin": instance.origin},
        )
        assert replay.status_code == 401

        page = await client.get("/control")
        assert page.status_code == 200 and "Tool exposure" in page.text
        assert "no-store" in page.headers["cache-control"]
        denied = await apply(client, "wrong", digest(tmp_path / "config.json"), "all", [])
        assert denied.status_code == 403
        anonymous = await client.post("/control/apply")
        assert anonymous.status_code in {403, 401}


@pytest.mark.asyncio
async def test_review_diff_apply_preserves_foreign_fields_and_verifies_restart(tmp_path):
    path = config_file(tmp_path)
    lifecycle = FakeLifecycle()
    instance = make_control(tmp_path, lifecycle)
    async with await open_page(tmp_path, instance) as client:
        await login(client, instance)
        review = await client.get(
            "/control",
            params=[
                ("review", "1"),
                ("mode", "selected"),
                ("tools", "send_message"),
                ("tools", "messages_get"),
            ],
        )
        assert review.status_code == 200
        assert "exposure_mode: all -&gt; selected" in review.text
        assert (
            "exposed_tools: [] -&gt; [&#x27;messages_get&#x27;, &#x27;send_message&#x27;]"
            in review.text
        )
        csrf = csrf_of(review.text)

        response = await apply(
            client, csrf, digest(path), "selected", ["send_message", "messages_get"]
        )
        assert response.status_code == 200, response.text
        assert "Applied and verified" in response.text
        assert lifecycle.stopped == 1 and len(lifecycle.started) == 1
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["future_setting"] == {"kept": "exactly"} and saved["port"] == 8799
        assert saved["exposure_mode"] == "selected"
        assert saved["exposed_tools"] == ["messages_get", "send_message"]
        stored = control.record(instance.settings) or {}
        assert stored["phase"] == "verified" and stored["owner"]["owner_id"] == "owner-1"
        assert stored["handoff"] is None or not stored["handoff"]

        # Repeated Apply of the unchanged configuration is a no-op: no stop, no restart.
        page = await client.get("/control")
        again = await apply(
            client, csrf_of(page.text), digest(path), "selected", ["messages_get", "send_message"]
        )
        assert again.status_code == 200 and "No change" in again.text
        assert lifecycle.stopped == 1 and len(lifecycle.started) == 1


@pytest.mark.asyncio
async def test_stale_revision_active_job_and_unknown_delivery_refuse_before_stop(tmp_path):
    path = config_file(tmp_path)
    lifecycle = FakeLifecycle()
    instance = make_control(tmp_path, lifecycle)
    async with await open_page(tmp_path, instance) as client:
        await login(client, instance)
        csrf = csrf_of((await client.get("/control")).text)

        stale = await apply(client, csrf, "0" * 64, "read-only", [])
        assert stale.status_code == 409 and "stale" in stale.text
        assert lifecycle.stopped == 0 and json.loads(path.read_text()) == BASE

        store = Store(tmp_path)
        try:
            job = {
                "id": "j1",
                "profile_id": "p",
                "kind": "export",
                "status": "queued",
                "payload": {},
                "result": None,
                "error": None,
            }
            with store.db:
                store.put("jobs", job)
        finally:
            store.close()
        blocked = await apply(client, csrf, digest(path), "read-only", [])
        assert blocked.status_code == 409 and "queued or running" in blocked.text
        assert lifecycle.stopped == 0 and json.loads(path.read_text()) == BASE

        store = Store(tmp_path)
        try:
            job["status"] = "needs_review"
            with store.db:
                store.put("jobs", job)
                store.put_delivery("j1", 0, {"status": "unknown", "chat_id": "1"})
        finally:
            store.close()
        uncertain = await apply(client, csrf, digest(path), "read-only", [])
        assert uncertain.status_code == 409 and "reconcile" in uncertain.text
        assert lifecycle.stopped == 0
        assert (control.record(instance.settings) or {}) == {}


def test_live_transition_blocks_login_and_serve_and_handoff_starts_once(tmp_path):
    config_file(tmp_path)
    settings = Settings.load(tmp_path)
    handoff = "one-use-token"
    control.save_record(
        settings,
        {
            "phase": "starting",
            "pid": os.getpid(),
            "handoff": control._digest(handoff),
            "started_at": "now",
        },
    )
    with pytest.raises(TeleloomError) as refused:
        with control.governed(settings):
            pytest.fail("an ordinary caller must not pass a live transition")
    assert refused.value.code == "transition_in_progress"

    # Ordinary serve and login cannot lift it: the owner start uses the same gate.
    with pytest.raises(TeleloomError) as served:
        asyncio.run(ensure_daemon(settings, TOKEN))
    assert served.value.code == "transition_in_progress"

    with control.governed(settings, handoff=handoff):
        pass
    assert (control.record(settings) or {}).get("handoff") is None
    with pytest.raises(TeleloomError):
        with control.governed(settings, handoff=handoff):
            pytest.fail("a consumed handoff must not authorize another start")

    value = control.record(settings) or {}
    value["phase"] = "verified"
    control.save_record(settings, value)
    with control.governed(settings):
        pass


def test_stale_record_is_abandoned_and_cross_process_lock_is_exclusive(tmp_path):
    config_file(tmp_path)
    settings = Settings.load(tmp_path)
    control.save_record(settings, {"phase": "stopped", "pid": 999_999_999, "started_at": "x"})
    with control.governed(settings):
        pass
    assert (control.record(settings) or {}).get("phase") == "abandoned"

    script = (
        "import sys, time\n"
        "from filelock import FileLock\n"
        f"lock = FileLock({str(tmp_path / 'control.lock')!r}, timeout=0)\n"
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(TeleloomError) as busy:
            with control.governed(settings):
                pytest.fail("a second management lock owner must be excluded")
        assert busy.value.code == "transition_in_progress"
    finally:
        holder.terminate()
        holder.wait(10)
    for _ in range(50):  # the OS releases the dead holder's lock shortly after exit
        try:
            with control.governed(settings):
                break
        except TeleloomError:
            time.sleep(0.1)
    else:
        pytest.fail("a dead manager's lock was never released")


@pytest.mark.asyncio
async def test_failed_stop_start_and_write_report_honest_state(tmp_path):
    path = config_file(tmp_path)
    stop_failed = FakeLifecycle(fail_stop=True)
    instance = make_control(tmp_path, stop_failed)
    async with await open_page(tmp_path, instance) as client:
        await login(client, instance)
        csrf = csrf_of((await client.get("/control")).text)
        response = await apply(client, csrf, digest(path), "read-only", [])
        assert response.status_code == 500 and "stop failed" in response.text
        assert json.loads(path.read_text()) == BASE
        stored = control.record(instance.settings) or {}
        assert stored["phase"] == "failed" and "nothing was written" in stored["reason"]

    start_failed = FakeLifecycle(fail_start=True)
    instance = make_control(tmp_path, start_failed)
    async with await open_page(tmp_path, instance) as client:
        await login(client, instance)
        csrf = csrf_of((await client.get("/control")).text)
        response = await apply(client, csrf, digest(path), "read-only", [])
        assert response.status_code == 500 and "durable" in response.text
        saved = json.loads(path.read_text())
        assert saved["exposure_mode"] == "read-only" and saved["future_setting"] == {
            "kept": "exactly"
        }
        assert start_failed.stopped == 1 and len(start_failed.started) == 1
        stored = control.record(instance.settings) or {}
        assert stored["phase"] == "failed"
        assert (json.loads(path.read_text())["exposure_mode"]) == "read-only"  # no rollback


def test_issued_url_is_a_bound_listener_not_a_checked_free_port(tmp_path):
    config_file(tmp_path)
    instance = make_control(tmp_path)
    try:
        assert instance.issue_bootstrap()["port"] == instance.socket.getsockname()[1]
        with pytest.raises(OSError):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", instance.port))
    finally:
        instance.close()


@pytest.mark.asyncio
async def test_apply_caps_body_without_trusting_content_length(tmp_path):
    config_file(tmp_path)
    instance = make_control(tmp_path)
    try:

        async def huge():
            yield b"a" * 20000

        async with await open_page(tmp_path, instance) as client:
            await login(client, instance)
            response = await client.post(
                "/control/apply",
                content=huge(),  # chunked: no Content-Length to trust
                headers={
                    "Origin": instance.origin,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        assert response.status_code == 413
    finally:
        instance.close()


def test_verification_compares_build_workspace_exposure_and_listed_tools(tmp_path):
    config_file(tmp_path)
    settings = Settings.load(tmp_path)
    observed = {
        "service": "teleloom",
        "owner_id": "owner-1",
        "workspace_id": control.workspace_id(settings),
        "build": build_info(),
        "exposure": {"mode": "selected", "selected_tools": ["messages_get"]},
        "tools": ["messages_get"],
    }
    names = {"messages_get", "chat_resolve"}
    assert control._mismatches(observed, settings, "selected", ["messages_get"], names) == []
    assert control._mismatches(
        {**observed, "build": {"build_id": "someone-else"}},
        settings,
        "selected",
        ["messages_get"],
        names,
    )
    assert control._mismatches(observed, settings, "selected", [], names)
    assert control._mismatches(
        {**observed, "workspace_id": "other"}, settings, "selected", ["messages_get"], names
    )


@pytest.mark.asyncio
async def test_transition_hands_over_starting_before_the_gate_releases(tmp_path):
    config_file(tmp_path)
    settings = Settings.load(tmp_path)
    seen: list[dict[str, Any]] = []

    class Recorder(FakeLifecycle):
        async def start(self, settings: Settings, handoff: str) -> None:
            # What a bridge or login reads at the instant the owner may start.
            seen.append({**(control.record(settings) or {}), "token": handoff})
            await super().start(settings, handoff)

    async def refuse() -> None:
        raise TeleloomError("save_failed", "injected body failure")

    value, failure = await control.transition(Recorder(), settings, {"profile": "work"}, refuse)
    assert seen[0]["phase"] == "starting"
    assert seen[0]["handoff"] == control._digest(seen[0]["token"])
    assert failure is not None and failure.code == "save_failed"
    assert value["phase"] == "failed" and "save_failed" in value["reason"]
