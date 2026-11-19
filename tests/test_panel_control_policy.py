"""#26: owner-only read policy and separate send/broadcast/AI grants, exact diff and Apply."""

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from teleloom import control
from teleloom.build_info import build_info
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
        "work": {"kind": "user", "read_mode": "all", "read_chats": [], "send_chats": []},
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


async def review(http, instance, profile, mode, read, send, broadcast, ai) -> str:
    params: list[tuple[str, str]] = [("profile", profile), ("review", "1"), ("mode", mode)]
    params += [("read", item) for item in read]
    params += [("send", item) for item in send]
    params += [("broadcast", item) for item in broadcast]
    params += [("ai", item) for item in ai]
    response = await http.get("/control/policy", params=params)
    assert response.status_code == 200, response.text
    return response.text


def session_csrf(instance) -> str:
    return next(iter(instance.sessions.values())).csrf


async def apply(http, instance, revision, profile, mode, read, send, broadcast, ai):
    return await http.post(
        "/control/policy/apply",
        data={
            "csrf": session_csrf(instance),
            "revision": revision,
            "profile": profile,
            "mode": mode,
            "read": read,
            "send": send,
            "broadcast": broadcast,
            "ai": ai,
        },
        headers={"Origin": instance.origin},
    )


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
        review = await http.get(
            "/control/policy",
            params=[
                ("profile", "personal"),
                ("review", "1"),
                ("mode", "selected"),
                ("read", "100"),
                ("read", "-1001234567890"),
                ("send", "100"),
                ("broadcast", "100"),
                ("ai", "100"),
            ],
        )
        assert review.status_code == 200, review.text
        assert (
            "profiles.read: {&quot;mode&quot;: &quot;all&quot;, &quot;chat_ids&quot;: []} -&gt; "
            in review.text
        )
        assert "&quot;chat_ids&quot;: [&quot;-1001234567890&quot;, &quot;100&quot;]" in review.text
        assert "profiles.ai: [] -&gt; [&quot;100&quot;]" in review.text
        assert "profiles.broadcast" not in review.text  # unchanged grants are absent from the diff

        invalid = await http.get(
            "/control/policy",
            params=[
                ("profile", "personal"),
                ("review", "1"),
                ("mode", "selected"),
                ("read", "00100"),
            ],
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
        ("selected", [], ["100"], "grant_not_readable"),
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
async def test_revoked_policy_denies_readers_and_frozen_job_evidence(tmp_path):
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
            http, instance, digest(tmp_path), "personal", "selected", [], [], [], []
        )
        assert revoked.status_code == 200, revoked.text
        assert instance.lifecycle.stopped == 1
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert stored["profiles"]["personal"]["read_chats"] == []
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
        assert grants["work"]["read"] == {"mode": "all", "chat_ids": []}
