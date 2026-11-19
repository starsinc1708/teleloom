"""#26: owner-only read policy and separate send/broadcast/AI grants, exact diff and Apply."""

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from teleloom import control
from teleloom.build_info import build_info
from teleloom.cli import app as cli
from teleloom.config import Settings
from teleloom.models import Message
from teleloom.store import Store
from tests.fakes import data
from tests.telegram_fakes import NOW
from tests.test_transport import client, running

BASE = {
    "port": 8799,
    "exposure_mode": "all",
    "exposed_tools": [],
    "future_setting": {"kept": "exactly"},
    "profiles": {
        "personal": {
            "kind": "user",
            "generation": "owner-personal-1",
            "identity": {"id": "7", "name": "Owner"},
            "read_mode": "all",
            "read_chats": [],
            "send_chats": ["100"],
            "broadcast_chats": ["100"],
            "jev_chats": [],
            "sync_chats": ["100"],
            "file_roots": ["/owner/export"],
            "future_profile_key": {"kept": True},
        },
        "work": {
            "kind": "user",
            "generation": "owner-work-1",
            "read_mode": "all",
            "read_chats": [],
            "send_chats": [],
        },
    },
}
CATALOGUE = [{"name": "messages_get", "summary": "Read messages", "writes": False}]


class FakeLifecycle:
    """The restarted owner's own report: scope profiles come from the written document."""

    def __init__(self) -> None:
        self.stopped = 0

    async def stop(self, settings: Settings) -> None:
        self.stopped += 1

    async def start(self, settings: Settings, handoff: str) -> None:
        pass

    async def observed(self, settings: Settings) -> dict[str, Any]:
        fresh = Settings.load(settings.data_dir)
        return {
            "service": "teleloom",
            "owner_id": "owner-1",
            "workspace_id": control.workspace_id(settings),
            "version": "0.6.0",
            "build": build_info(),
            "exposure": {"mode": "all", "selected_tools": []},
            "tools": ["messages_get"],
            "scope": {
                "tool_exposure": {"mode": "all", "selected_tools": []},
                "profiles": [
                    {"id": name, "grants": profile.grants()}
                    for name, profile in fresh.profiles.items()
                ],
            },
        }


def write_config(tmp_path, value=None) -> bytes:
    (tmp_path / "config.json").write_text(
        json.dumps(value if value is not None else BASE), encoding="utf-8"
    )
    return (tmp_path / "config.json").read_bytes()


def digest(tmp_path) -> str:
    return hashlib.sha256((tmp_path / "config.json").read_bytes()).hexdigest()


def make_control(tmp_path) -> control.Control:
    return control.Control(Settings.load(tmp_path), CATALOGUE, lifecycle=FakeLifecycle())


@pytest.fixture
async def browser(tmp_path):
    write_config(tmp_path)
    instance = make_control(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url=instance.origin
    ) as http:
        fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
        response = await http.post(
            "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
        )
        assert response.status_code == 204
        yield instance, http


def csrf_of(body: str) -> str:
    return body.split('name="csrf" value="', 1)[1].split('"', 1)[0]


async def apply(
    http, instance, revision, profile, mode, read, send, broadcast, ai, *, review_only=False
):
    fields = {
        "csrf": csrf_of((await http.get("/control/policy")).text),
        "revision": revision,
        "profile": profile,
        "mode": mode,
        "read": read,
        "send": send,
        "broadcast": broadcast,
        "ai": ai,
    }
    path = "/control/policy/apply"
    headers = {"Origin": instance.origin}
    reviewed = await http.post(path, data={**fields, "review": "1"}, headers=headers)
    if review_only or reviewed.status_code != 200:
        return reviewed
    key = reviewed.text.split('name="reviewed" value="', 1)[1].split('"', 1)[0]
    return await http.post(path, data={**fields, "reviewed": key}, headers=headers)


@pytest.mark.asyncio
async def test_policy_needs_the_owner_launch_and_review_shows_the_exact_diff(tmp_path):
    write_config(tmp_path)
    instance = make_control(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url=instance.origin
    ) as anonymous:
        assert (await anonymous.get("/control/policy")).status_code == 401
        assert (
            await anonymous.post(
                "/control/policy/apply",
                data={"profile": "personal", "mode": "all"},
                headers={"Origin": "https://evil.invalid"},
            )
        ).status_code == 403

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url=instance.origin
    ) as http:
        fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
        assert (
            await http.post(
                "/control/session",
                json={"bootstrap": fragment},
                headers={"Origin": instance.origin},
            )
        ).status_code == 204
        review = await apply(
            http,
            instance,
            digest(tmp_path),
            "personal",
            "selected",
            ["100\r\n-1001234567890"],
            ["100"],
            ["100"],
            ["100"],
            review_only=True,
        )
        assert review.status_code == 200, review.text
        assert (
            "profiles.read: {&quot;mode&quot;: &quot;all&quot;, &quot;chat_ids&quot;: []} -&gt; "
            in review.text
        )
        assert "&quot;chat_ids&quot;: [&quot;-1001234567890&quot;, &quot;100&quot;]" in review.text
        assert "profiles.ai: [] -&gt; [&quot;100&quot;]" in review.text
        assert "profiles.broadcast" not in review.text  # unchanged grants are absent from the diff

        invalid = await apply(
            http,
            instance,
            digest(tmp_path),
            "personal",
            "selected",
            ["00100"],
            [],
            [],
            [],
            review_only=True,
        )
        assert invalid.status_code == 422 and "invalid_chat_id" in invalid.text


@pytest.mark.asyncio
async def test_apply_writes_only_five_keys_verifies_and_is_idempotent(tmp_path, browser):
    instance, http = browser
    before = BASE["profiles"]["personal"]
    response = await apply(
        http,
        instance,
        digest(tmp_path),
        "personal",
        "selected",
        ["100", "-1001234567890"],
        ["100"],
        [],
        ["-1001234567890"],
    )
    assert response.status_code == 200, response.text
    assert "Applied and verified" in response.text
    assert "owner-1" in response.text

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    personal = stored["profiles"]["personal"]
    assert personal["read_mode"] == "selected"
    assert personal["read_chats"] == ["-1001234567890", "100"]  # exact IDs, numeric order
    assert personal["send_chats"] == ["100"] and personal["broadcast_chats"] == []
    assert personal["jev_chats"] == ["-1001234567890"]
    for key in ("sync_chats", "file_roots", "future_profile_key", "identity", "kind"):
        assert personal[key] == before[key]
    assert stored["profiles"]["work"] == BASE["profiles"]["work"]
    assert stored["future_setting"] == {"kept": "exactly"} and stored["port"] == 8799

    record = control.record(instance.settings)
    assert record is not None
    assert record["phase"] == "verified" and record["handoff"] is None
    assert record["proposed"]["read"] == {"mode": "selected", "chat_ids": ["-1001234567890", "100"]}

    # A repeated Apply with the same reviewed policy is a no-op: no second stop or restart.
    stopped = instance.lifecycle.stopped
    again = await apply(
        http,
        instance,
        digest(tmp_path),
        "personal",
        "selected",
        ["100", "-1001234567890"],
        ["100"],
        [],
        ["-1001234567890"],
    )
    assert again.status_code == 200 and "No change" in again.text
    assert instance.lifecycle.stopped == stopped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "read", "send", "code"),
    [
        ("selected", ["00100"], [], "invalid_chat_id"),
        ("selected", ["+100"], [], "invalid_chat_id"),
        ("selected", ["100.0"], [], "invalid_chat_id"),
        ("selected", ["-0"], [], "invalid_chat_id"),
        ("selected", [], ["200"], "grant_not_readable"),
        ("everything", ["100"], [], "invalid_read_mode"),
    ],
)
async def test_ambiguous_or_inferred_identity_is_refused_without_writing(
    tmp_path, browser, mode, read, send, code
):
    instance, http = browser
    before = (tmp_path / "config.json").read_bytes()
    response = await apply(http, instance, digest(tmp_path), "personal", mode, read, send, [], [])
    assert response.status_code == 422, response.text
    assert code in response.text
    assert (tmp_path / "config.json").read_bytes() == before
    assert instance.lifecycle.stopped == 0


@pytest.mark.asyncio
async def test_unknown_profile_and_stale_review_are_refused(tmp_path, browser):
    instance, http = browser
    before = (tmp_path / "config.json").read_bytes()
    unknown = await apply(http, instance, digest(tmp_path), "Personal", "all", [], [], [], [])
    assert unknown.status_code == 422 and "profile_not_found" in unknown.text
    assert (tmp_path / "config.json").read_bytes() == before

    stale = await apply(
        http,
        instance,
        hashlib.sha256(b"another document").hexdigest(),
        "personal",
        "all",
        [],
        [],
        [],
        [],
    )
    assert stale.status_code == 409 and "stale" in stale.text
    assert (tmp_path / "config.json").read_bytes() == before
    assert instance.lifecycle.stopped == 0


@pytest.mark.asyncio
async def test_apply_requires_the_same_payload_that_was_reviewed(tmp_path, browser):
    instance, http = browser
    fields = {
        "profile": "personal",
        "csrf": csrf_of((await http.get("/control/policy")).text),
        "revision": digest(tmp_path),
        "mode": "selected",
        "read": "100",
    }
    headers = {"Origin": instance.origin}
    path = "/control/policy/apply"
    reviewed = await http.post(path, data={**fields, "review": "1"}, headers=headers)
    assert reviewed.status_code == 200
    assert 'name="read" value="100"' in reviewed.text
    key = reviewed.text.split('name="reviewed" value="', 1)[1].split('"', 1)[0]
    changed = await http.post(
        path, data={**fields, "read": "200", "reviewed": key}, headers=headers
    )
    assert changed.status_code == 409
    assert instance.lifecycle.stopped == 0
    assert Settings.load(tmp_path).profiles["personal"].read_mode == "all"


def seed(directory):
    store = Store(directory)
    store.save_messages(
        [
            Message(profile_id="personal", chat_id=chat, id="10", date=NOW, text=f"{chat} alpha")
            for chat in ("100", "200")
        ]
    )
    store.close()


def no_connection(*args):
    raise AssertionError("Read revocation must not connect, sync or discover dialogs")


@pytest.mark.asyncio
async def test_revoked_policy_denies_readers_and_frozen_job_evidence(tmp_path, monkeypatch):
    stored = json.loads(json.dumps(BASE))
    stored["exposure_mode"] = "read-only"
    stored["profiles"]["personal"]["read_mode"] = "selected"
    stored["profiles"]["personal"]["read_chats"] = ["100"]
    write_config(tmp_path, stored)
    seed(tmp_path)
    settings = Settings.load(tmp_path)
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        allowed = data(
            await mcp.call_tool(
                "messages_search_local",
                {"profile_id": "personal", "chat_ids": ["100"], "query": "alpha"},
            )
        )
        assert allowed["ok"], allowed
        reference = allowed["data"]["evidence_ref"]
        job_id = allowed["data"]["job_id"]
        denied = data(
            await mcp.call_tool(
                "messages_search_local",
                {"profile_id": "personal", "chat_ids": ["200"], "query": "alpha"},
            )
        )
        assert not denied["ok"] and denied["error"]["code"] == "read_not_allowed"

    # Revoke chat 100 through the owner-only editor, then read the persisted policy again.
    instance = make_control(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url=instance.origin
    ) as http:
        fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
        await http.post(
            "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
        )
        revoked = await apply(
            http, instance, digest(tmp_path), "personal", "selected", [], ["100"], ["100"], []
        )
        assert revoked.status_code == 200, revoked.text
        assert instance.lifecycle.stopped == 1
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert stored["profiles"]["personal"]["read_chats"] == []
    assert stored["profiles"]["personal"]["send_chats"] == ["100"]
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    listed = CliRunner().invoke(cli, ["profile", "list"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["personal"]["read_chats"] == []
    assert stored["profiles"]["work"]["read_mode"] == "all"

    # The same public readers and the earlier frozen job now refuse that evidence.
    async with (
        running(Settings.load(tmp_path), no_connection) as app,
        client(app, Settings.load(tmp_path)) as mcp,
    ):
        gone = data(
            await mcp.call_tool(
                "messages_search_local",
                {"profile_id": "personal", "chat_ids": ["100"], "query": "alpha"},
            )
        )
        assert not gone["ok"] and gone["error"]["code"] == "read_not_allowed"
        originals = data(
            await mcp.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job_id,
                    "evidence_ref": reference,
                    "message_keys": [{"chat_id": "100", "message_id": "10"}],
                },
            )
        )
        assert not originals["ok"] and originals["error"]["code"] == "read_not_allowed"
        # The other profile was not widened or narrowed by editing this one.
        other = data(await mcp.call_tool("server_status", {}))
        grants = {p["id"]: p["grants"] for p in other["data"]["scope"]["profiles"]}
        assert grants["personal"]["read"] == {"mode": "selected", "chat_ids": []}
        assert grants["personal"]["send"] == [] and grants["personal"]["broadcast"] == []
        assert not any(
            "policy" in tool.name or "grant" in tool.name for tool in (await mcp.list_tools()).tools
        )
        assert grants["work"]["read"] == {"mode": "all", "chat_ids": []}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url=instance.origin
    ) as http:
        fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
        assert (
            await http.post(
                "/control/session",
                json={"bootstrap": fragment},
                headers={"Origin": instance.origin},
            )
        ).status_code == 204
        restored = await apply(
            http, instance, digest(tmp_path), "personal", "selected", ["100"], ["100"], ["100"], []
        )
        assert restored.status_code == 200, restored.text
    settings = Settings.load(tmp_path)
    async with running(settings, no_connection) as app, client(app, settings) as mcp:
        recovered = data(
            await mcp.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job_id,
                    "evidence_ref": reference,
                    "message_keys": [{"chat_id": "100", "message_id": "10"}],
                },
            )
        )
        assert recovered["ok"], recovered
        assert "100 alpha" in json.dumps(recovered)
