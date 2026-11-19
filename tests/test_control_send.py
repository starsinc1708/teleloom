"""Owner-only /control/send: exact preview, separate Confirm, durable receipt.

Fake owner for the boundary and tamper cases; one real owner over a real loopback
listener and MCP with fake Telegram, so the production Owner client, the plan row,
the delivery job and the receipt page are all exercised as installed.
"""

import asyncio
import re
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn

from teleloom import control
from teleloom.config import Limits, Profile, Settings
from teleloom.daemon import create_application
from teleloom.models import TeleloomError, utcnow
from teleloom.runtime import fingerprint
from teleloom.store import Store
from tests.fakes import TelegramAPI

TOKEN = "send-test-owner-token"
CATALOGUE = [
    {"name": "delivery_preview", "summary": "Preview", "writes": True},
    {"name": "delivery_execute", "summary": "Execute", "writes": True},
    {"name": "jobs_status", "summary": "Read jobs", "writes": False},
]
BASE = {
    "port": 8799,
    "profiles": {"personal": {"kind": "user"}, "other": {"kind": "user"}},
    "exposure_mode": "all",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write(settings: Settings, table: str, record: dict[str, Any]) -> None:
    store = Store(settings.data_dir)
    try:
        with store.db:
            store.put(table, record)
    finally:
        store.close()


def read(settings: Settings, table: str, id_: str) -> dict[str, Any] | None:
    store = Store(settings.data_dir)
    try:
        return store.get(table, id_)
    finally:
        store.close()


class FakeOwner:
    """Records every call and writes the same durable rows the owner would."""

    def __init__(
        self, settings: Settings, *, lose_response: bool = False, unknown: bool = False
    ) -> None:
        self.settings = settings
        self.lose_response = lose_response
        self.unknown = unknown
        self.calls: list[tuple[str, str, str, str]] = []
        self.jobs: dict[str, dict[str, Any]] = {}

    async def preview(self, profile: str, chat: str, text: str) -> dict[str, Any]:
        self.calls.append(("preview", profile, chat, text))
        payload = {
            "profile_id": profile,
            "account": {"kind": "user", "identity": {"id": "42", "username": "owner"}},
            "recipients": [chat],
            "text": text,
            "reply_to_message_id": None,
            "broadcast": False,
            "format": "plain_text",
            "resolved_targets": [{"id": chat, "title": "Engineering"}],
            "owner_authorized": True,
        }
        plan = {
            "id": uuid.uuid4().hex,
            "profile_id": profile,
            "payload": payload,
            "hash": fingerprint(payload),
            "expires_at": (utcnow() + timedelta(minutes=15)).isoformat(),
            "job_id": None,
            "resolved": [{"id": chat, "title": "Engineering"}],
        }
        write(self.settings, "plans", plan)
        return {"plan_id": plan["id"], "plan_hash": plan["hash"]}

    async def execute(self, profile: str, plan_id: str, plan_hash: str) -> dict[str, Any]:
        self.calls.append(("execute", profile, plan_id, plan_hash))
        plan = read(self.settings, "plans", plan_id)
        assert plan is not None and plan["hash"] == plan_hash
        if plan.get("job_id"):
            return {"job_id": plan["job_id"]}
        if utcnow() >= datetime.fromisoformat(plan["expires_at"]):
            raise TeleloomError("plan_expired", "Create and confirm a fresh preview.")
        job = {
            "id": uuid.uuid4().hex,
            "profile_id": profile,
            "kind": "delivery",
            "status": "needs_review" if self.unknown else "completed",
            "created_at": utcnow().isoformat(),
            "progress": 1,
            "error": None,
            "payload": plan["payload"],
            "deliveries": [
                {
                    "chat_id": plan["payload"]["recipients"][0],
                    "status": "unknown" if self.unknown else "sent",
                    "message_id": None if self.unknown else "101",
                }
            ],
        }
        write(self.settings, "jobs", job)
        plan["job_id"] = job["id"]
        write(self.settings, "plans", plan)
        self.jobs[job["id"]] = job
        if self.lose_response:
            raise TeleloomError("delivery_unknown", "The owner's answer was lost.")
        return {"job_id": job["id"]}

    async def job(self, profile: str, job_id: str) -> dict[str, Any]:
        self.calls.append(("job", profile, job_id, ""))
        return self.jobs[job_id]


def config_file(tmp_path, data=None):
    (tmp_path / "config.json").write_text(__import__("json").dumps(data or BASE), "utf-8")
    return Settings.load(tmp_path)


async def open_control(settings: Settings, owner: FakeOwner | None = None):
    instance = control.Control(settings, CATALOGUE, owner=owner)
    return instance, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url=instance.origin
    )


async def login(client: httpx.AsyncClient, instance: control.Control) -> None:
    fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
    response = await client.post(
        "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
    )
    assert response.status_code == 204, response.text


def csrf_of(body: str) -> str:
    return body.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def hash_of(body: str) -> str:
    return re.search(r'name="plan_hash" value="([^"]+)"', body).group(1)


async def submit(client, path: str, data: dict[str, str]):
    return await client.post(path, data=data, headers={"Origin": str(client.base_url).rstrip("/")})


@pytest.mark.asyncio
async def test_send_page_needs_the_owner_session_and_previews_only_the_typed_target(tmp_path):
    settings = config_file(tmp_path)
    owner = FakeOwner(settings)
    instance, client = await open_control(settings, owner)
    async with client:
        assert (await client.get("/control/send")).status_code == 401
        bearer = await client.get("/control/send", headers={"Authorization": "Bearer " + TOKEN})
        assert bearer.status_code == 401
        # The MCP bearer is not a browser session: it cannot preview or confirm.
        assert (
            await client.post(
                "/control/send/preview",
                data={"csrf": "", "profile": "personal", "chat_id": "100", "text": "x"},
                headers={"Origin": instance.origin, "Authorization": "Bearer " + TOKEN},
            )
        ).status_code == 401
        assert (
            await client.post(
                "/control/send/confirm",
                data={"csrf": "", "profile": "personal", "plan_id": "x", "plan_hash": "y"},
                headers={"Origin": instance.origin, "Authorization": "Bearer " + TOKEN},
            )
        ).status_code == 401
        assert owner.calls == []

        await login(client, instance)
        page = await client.get("/control/send")
        assert page.status_code == 200
        assert "<form" in page.text and "Preview this exact send" in page.text
        assert owner.calls == []  # a passive page reads no Telegram and mints nothing
        csrf = csrf_of(page.text)

        # A foreign Origin, a wrong CSRF token or a name is not the human step.
        foreign = await client.post(
            "/control/send/preview",
            data={"csrf": csrf, "profile": "personal", "chat_id": "100", "text": "x"},
            headers={"Origin": "https://evil.invalid"},
        )
        assert foreign.status_code == 403
        assert (
            await submit(
                client,
                "/control/send/preview",
                {"csrf": "wrong", "profile": "personal", "chat_id": "100", "text": "x"},
            )
        ).status_code == 403
        named = await submit(
            client,
            "/control/send/preview",
            {"csrf": csrf, "profile": "personal", "chat_id": "Engineering", "text": "x"},
        )
        assert named.status_code == 422 and "not an exact numeric chat ID" in named.text
        assert owner.calls == []

        accepted = await submit(
            client,
            "/control/send/preview",
            {
                "csrf": csrf,
                "profile": "personal",
                "chat_id": "-100123",
                "text": "Hello <b>owner</b>",
            },
        )
        assert accepted.status_code == 303, accepted.text
        assert accepted.headers["location"].startswith("/control/send/plan/")
        assert owner.calls == [("preview", "personal", "-100123", "Hello <b>owner</b>")]

        review = await client.get(accepted.headers["location"])
        assert review.status_code == 200
        assert "Engineering" in review.text and "Hello &lt;b&gt;owner&lt;/b&gt;" in review.text
        assert "user · 42 · owner" in review.text  # exact sender identity, not a request field
        assert "Confirm and send this exact message" in review.text
        assert hash_of(review.text)


@pytest.mark.asyncio
async def test_confirm_needs_the_reviewed_hash_and_creates_one_durable_job(tmp_path):
    settings = config_file(tmp_path)
    owner = FakeOwner(settings)
    instance, client = await open_control(settings, owner)
    async with client:
        await login(client, instance)
        page = await client.get("/control/send")
        csrf = csrf_of(page.text)
        accepted = await submit(
            client,
            "/control/send/preview",
            {"csrf": csrf, "profile": "personal", "chat_id": "100", "text": "Reviewed"},
        )
        review = await client.get(accepted.headers["location"])
        plan_id = re.search(r'name="plan_id" value="([^"]+)"', review.text).group(1)
        reviewed = hash_of(review.text)
        assert plan_id in accepted.headers["location"] and reviewed

        form = {"csrf": csrf, "profile": "personal", "plan_id": plan_id}
        tampered = await submit(
            client, "/control/send/confirm", {**form, "plan_hash": reviewed[:-1] + "0"}
        )
        assert tampered.status_code == 409 and "Nothing was sent" in tampered.text
        assert [call[0] for call in owner.calls] == ["preview"]

        no_csrf = await submit(
            client,
            "/control/send/confirm",
            {"profile": "personal", "plan_id": plan_id, "plan_hash": reviewed},
        )
        assert no_csrf.status_code == 403
        assert [call[0] for call in owner.calls] == ["preview"]

        # A plan that belongs to another profile is not reachable through this page.
        foreign = await client.get(f"/control/send/plan/{plan_id}?profile=other")
        assert foreign.status_code == 404

        confirmed = await submit(client, "/control/send/confirm", {**form, "plan_hash": reviewed})
        assert confirmed.status_code == 303, confirmed.text
        job_id = confirmed.headers["location"].split("/job/")[1].split("?")[0]
        assert [call[0] for call in owner.calls] == ["preview", "execute"]

        receipt = await client.get(confirmed.headers["location"])
        assert receipt.status_code == 200
        assert job_id in receipt.text and "completed" in receipt.text
        assert "101" in receipt.text  # the durable Telegram message identity
        assert f"/ui/jobs/{job_id}?profile=personal" in receipt.text

        # Reload/back/double click reopens the same job and never executes twice.
        repeated = await submit(client, "/control/send/confirm", {**form, "plan_hash": reviewed})
        assert repeated.status_code == 303
        assert repeated.headers["location"] == confirmed.headers["location"]
        assert [call[0] for call in owner.calls] == ["preview", "execute", "job"]
        assert read(settings, "plans", plan_id)["job_id"] == job_id


@pytest.mark.asyncio
async def test_expired_plan_is_refused_at_review_and_confirm(tmp_path):
    settings = config_file(tmp_path)
    owner = FakeOwner(settings)
    instance, client = await open_control(settings, owner)
    async with client:
        await login(client, instance)
        csrf = csrf_of((await client.get("/control/send")).text)
        accepted = await submit(
            client,
            "/control/send/preview",
            {"csrf": csrf, "profile": "personal", "chat_id": "100", "text": "Late"},
        )
        review = await client.get(accepted.headers["location"])
        plan_id = re.search(r'name="plan_id" value="([^"]+)"', review.text).group(1)
        stored = read(settings, "plans", plan_id)
        assert stored is not None
        stored["expires_at"] = (utcnow() - timedelta(minutes=1)).isoformat()
        write(settings, "plans", stored)

        late_review = await client.get(accepted.headers["location"])
        assert late_review.status_code == 409 and "expired" in late_review.text
        refused = await submit(
            client,
            "/control/send/confirm",
            {
                "csrf": csrf,
                "profile": "personal",
                "plan_id": plan_id,
                "plan_hash": hash_of(review.text),
            },
        )
        assert refused.status_code == 409 and "fresh preview" in refused.text
        assert "plan_expired" in refused.text
        assert [call[0] for call in owner.calls] == ["preview", "execute"]
        unchanged = read(settings, "plans", plan_id)
        assert unchanged is not None and unchanged["job_id"] is None  # no job, no send


@pytest.mark.asyncio
async def test_unknown_outcome_is_shown_and_never_resent(tmp_path):
    settings = config_file(tmp_path)
    owner = FakeOwner(settings, unknown=True)
    instance, client = await open_control(settings, owner)
    async with client:
        await login(client, instance)
        csrf = csrf_of((await client.get("/control/send")).text)
        accepted = await submit(
            client,
            "/control/send/preview",
            {"csrf": csrf, "profile": "personal", "chat_id": "100", "text": "Maybe"},
        )
        review = await client.get(accepted.headers["location"])
        form = {
            "csrf": csrf,
            "profile": "personal",
            "plan_id": accepted.headers["location"].split("/plan/")[1].split("?")[0],
            "plan_hash": hash_of(review.text),
        }
        confirmed = await submit(client, "/control/send/confirm", form)
        assert confirmed.status_code == 303
        receipt = await client.get(confirmed.headers["location"])
        assert "Outcome unknown" in receipt.text and "unknown" in receipt.text
        assert "never resends" in receipt.text

        repeated = await submit(client, "/control/send/confirm", form)
        assert repeated.headers["location"] == confirmed.headers["location"]
        assert [call[0] for call in owner.calls] == ["preview", "execute", "job"]


@pytest.mark.asyncio
async def test_lost_execute_answer_reports_the_durable_job_and_never_replays(tmp_path):
    settings = config_file(tmp_path)
    owner = FakeOwner(settings, lose_response=True)
    instance, client = await open_control(settings, owner)
    async with client:
        await login(client, instance)
        csrf = csrf_of((await client.get("/control/send")).text)
        accepted = await submit(
            client,
            "/control/send/preview",
            {"csrf": csrf, "profile": "personal", "chat_id": "100", "text": "Once"},
        )
        review = await client.get(accepted.headers["location"])
        plan_id = re.search(r'name="plan_id" value="([^"]+)"', review.text).group(1)
        response = await submit(
            client,
            "/control/send/confirm",
            {
                "csrf": csrf,
                "profile": "personal",
                "plan_id": plan_id,
                "plan_hash": hash_of(review.text),
            },
        )
        # The job was written before the answer was lost: the page follows the durable record.
        assert response.status_code == 303 and "/job/" in response.headers["location"]
        assert [call[0] for call in owner.calls] == ["preview", "execute"]


@pytest.mark.asyncio
async def test_real_owner_sends_one_exact_message_and_shows_its_receipt(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles={"personal": Profile(kind="user")},
        limits=Limits(interval_seconds=1),
    )
    settings.save()
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", TOKEN)
    apis: list[TelegramAPI] = []

    def factory(*args):
        api = TelegramAPI(*args)
        apis.append(api)
        return api

    app = create_application(settings, TOKEN, factory)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=settings.port, log_level="critical", access_log=False
        )
    )
    task = asyncio.create_task(server.serve())
    control_task = None
    control_server = None
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        instance = control.Control(settings, CATALOGUE)  # the real MCP Owner client
        control_server = uvicorn.Server(
            uvicorn.Config(instance, log_level="critical", access_log=False)
        )
        control_task = asyncio.create_task(control_server.serve(sockets=[instance.socket]))
        async with asyncio.timeout(10):
            while not control_server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=instance.origin, trust_env=False) as client:
            assert (await client.get("/control/send")).status_code == 401
            await login(client, instance)
            page = await client.get("/control/send")
            assert page.status_code == 200 and "Preview this exact send" in page.text
            assert all(api.sent == [] for api in apis)

            accepted = await submit(
                client,
                "/control/send/preview",
                {
                    "csrf": csrf_of(page.text),
                    "profile": "personal",
                    "chat_id": "100",
                    "text": "Привет, мир",
                },
            )
            assert accepted.status_code == 303, accepted.text
            review = await client.get(accepted.headers["location"])
            assert review.status_code == 200, review.text
            assert "Engineering" in review.text and "Привет, мир" in review.text
            assert apis[0].sent == []  # a preview resolves the target and sends nothing

            confirmed = await submit(
                client,
                "/control/send/confirm",
                {
                    "csrf": csrf_of(review.text),
                    "profile": "personal",
                    "plan_id": accepted.headers["location"].split("/plan/")[1].split("?")[0],
                    "plan_hash": hash_of(review.text),
                },
            )
            assert confirmed.status_code == 303, confirmed.text
            location = confirmed.headers["location"]
            for _ in range(60):
                receipt = await client.get(location)
                if "completed" in receipt.text:
                    break
                await asyncio.sleep(0.1)
            assert "completed" in receipt.text, receipt.text
            assert [(row[0], row[1]) for row in apis[0].sent] == [("100", "Привет, мир")]
            assert "/ui/jobs/" in receipt.text and f"port={settings.port}" not in receipt.text

            again = await submit(
                client,
                "/control/send/confirm",
                {
                    "csrf": csrf_of(review.text),
                    "profile": "personal",
                    "plan_id": accepted.headers["location"].split("/plan/")[1].split("?")[0],
                    "plan_hash": hash_of(review.text),
                },
            )
            assert again.status_code == 303  # a stale page reopens the same job
            assert again.headers["location"] == location
            assert len(apis[0].sent) == 1
    finally:
        if control_task is not None and control_server is not None:
            control_server.should_exit = True
            await asyncio.wait_for(control_task, timeout=15)
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


@pytest.mark.asyncio
async def test_send_refuses_an_exposure_that_hides_the_delivery_tools(tmp_path):
    settings = Settings.load(tmp_path)
    (tmp_path / "config.json").write_text(
        '{"profiles": {"personal": {"kind": "user"}}, "exposure_mode": "read-only"}', "utf-8"
    )
    settings = Settings.load(tmp_path)
    instance = control.Control(settings, CATALOGUE, owner=FakeOwner(settings))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url=instance.origin
    ) as client:
        await login(client, instance)
        page = await client.get("/control/send")
        assert page.status_code == 200
        assert "does not register delivery_preview, delivery_execute" in page.text
        assert '<button type="submit">Preview this exact send</button>' not in page.text
