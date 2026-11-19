"""Read-only jobs and receipts panel: pagination, policy and restart preservation."""

import html
import re
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest

from teleloom.config import Profile, Settings
from teleloom.panel import Panel
from teleloom.runtime import Runtime

TOKEN = "panel-jobs-owner-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def no_telegram(*args, **kwargs):
    raise AssertionError("the jobs panel must not open a Telegram or AI connection")


@asynccontextmanager
async def running(settings: Settings):
    runtime = Runtime(settings, adapter_factory=no_telegram)
    await runtime.start()
    panel = Panel(settings, runtime, TOKEN)
    try:
        yield panel
    finally:
        await runtime.close()


def client(panel: Panel) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=panel), base_url=panel.origin)


async def login(browser: httpx.AsyncClient, panel: Panel) -> None:
    issued = await browser.post(
        "/ui/bootstrap",
        headers={"Authorization": "Bearer " + TOKEN, "Origin": panel.origin},
    )
    assert issued.status_code == 200, issued.text
    exchanged = await browser.post(
        "/ui/session",
        json={"bootstrap": urlsplit(issued.json()["url"]).fragment},
        headers={"Origin": panel.origin},
    )
    assert exchanged.status_code == 204, exchanged.text


def binding(settings: Settings, profile: str) -> dict:
    config = settings.profiles[profile]
    return {
        "generation": config.generation,
        "kind": config.kind,
        "identity": config.identity,
    }


def job(
    store,
    settings: Settings,
    id_: str,
    *,
    profile: str = "work",
    kind: str = "sync",
    status: str = "queued",
    progress: int = 0,
    payload: dict | None = None,
    error: dict | None = None,
) -> None:
    store.put(
        "jobs",
        {
            "id": id_,
            "profile_id": profile,
            "account": binding(settings, profile),
            "kind": kind,
            "status": status,
            "created_at": "2026-01-01T00:00:00+00:00",
            "payload": payload or {},
            "progress": progress,
            "next_run": 0.0,
            "error": error,
            "result": None,
        },
    )


def delivery(store, job_id: str, position: int, status: str, **extra) -> None:
    store.put_delivery(
        job_id,
        position,
        {
            "chat_id": str(100 + position),
            "target": {"kind": "chat", "chat_id": str(100 + position)},
            "status": status,
            "random_id": position,
            "message_id": extra.pop("message_id", None),
            "error": extra.pop("error", None),
            **extra,
        },
    )


def ids_in(text: str) -> list[str]:
    return re.findall(r'/ui/jobs/([^"?]+)\?profile=', text)


def next_link(text: str, prefix: str) -> str:
    found = re.search(rf'href="({re.escape(prefix)}[^"]+)"', text)
    assert found, text
    return html.unescape(found.group(1))


@pytest.mark.asyncio
async def test_list_filters_and_keyset_pages_cover_every_job_without_side_effects(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles={
            "work": Profile(kind="user", identity={"phone": "private"}),
            "personal": Profile(kind="user", identity={"phone": "private"}),
        },
    )
    async with running(settings) as panel:
        with panel.runtime.store.db:
            for index in range(15):
                job(panel.runtime.store, settings, f"work-sync-{index:02d}")
            for index in range(10):
                job(panel.runtime.store, settings, f"personal-sync-{index:02d}", profile="personal")
            for index in range(5):
                job(
                    panel.runtime.store,
                    settings,
                    f"work-delivery-{index:02d}",
                    kind="delivery",
                    status="completed",
                )
            total = len(panel.runtime.store.jobs())
        async with client(panel) as browser:
            assert (await browser.get("/ui/jobs")).status_code == 401
            await login(browser, panel)
            first = await browser.get("/ui/jobs")
            assert first.status_code == 200
            assert "no-store" in first.headers["cache-control"]
            assert "25 job(s) shown — more pages available" in first.text
            second = await browser.get(next_link(first.text, "/ui/jobs?"))
            assert second.status_code == 200
            seen = ids_in(first.text) + ids_in(second.text)
            assert len(seen) == total and len(set(seen)) == total

            filtered = await browser.get("/ui/jobs?profile=personal")
            assert len(ids_in(filtered.text)) == 10
            assert "work-sync" not in filtered.text
            kinds = await browser.get("/ui/jobs?kind=delivery")
            assert len(ids_in(kinds.text)) == 5
            empty = await browser.get("/ui/jobs?status=needs_review")
            assert "No jobs match these filters" in empty.text
            assert "private" not in first.text and str(tmp_path) not in first.text

            bad = await browser.get("/ui/jobs?profile=missing")
            assert bad.status_code == 404
            assert "profile_not_found" in bad.text
            cursor = await browser.get("/ui/jobs?cursor=not-a-cursor")
            assert cursor.status_code == 400
            assert "invalid_cursor" in cursor.text

        # Refreshing and paging only read durable state: no job was created or changed.
        assert len(panel.runtime.store.jobs()) == total
        assert not panel.runtime.adapters


@pytest.mark.asyncio
async def test_delivery_detail_pages_receipts_and_preserves_partial_unknown_and_schedule(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles={"work": Profile(kind="user", identity={"phone": "private"})},
    )
    async with running(settings) as panel:
        with panel.runtime.store.db:
            job(
                panel.runtime.store,
                settings,
                "delivery-job",
                kind="delivery",
                status="needs_review",
                progress=9,
                payload={"recipients": [str(100 + index) for index in range(12)], "text": "secret"},
                error={"code": "mutation_partial", "message": "SECRET-DETAIL must not render"},
            )
            for position in range(9):
                delivery(
                    panel.runtime.store,
                    "delivery-job",
                    position,
                    "sent",
                    message_id=str(5000 + position),
                    attempted_at="2026-01-01T00:01:00+00:00",
                )
            delivery(
                panel.runtime.store,
                "delivery-job",
                9,
                "partial",
                receipts=[
                    {"accepted": True, "message_ids": ["6000"]},
                    {
                        "schedule_accepted": True,
                        "scheduled_for": "2026-02-01T10:00:00+00:00",
                        "delivery_confirmed": False,
                    },
                ],
            )
            delivery(
                panel.runtime.store,
                "delivery-job",
                10,
                "unknown",
                error={"code": "delivery_unknown", "message": "SECRET-DETAIL must not render"},
            )
            delivery(panel.runtime.store, "delivery-job", 11, "pending")
        async with client(panel) as browser:
            await login(browser, panel)
            page = await browser.get("/ui/jobs/delivery-job?profile=work")
            assert page.status_code == 200
            assert "delivery-job" in page.text and ">delivery<" in page.text
            assert "Needs review" in page.text
            assert "mutation_partial" in page.text
            assert "SECRET-DETAIL" not in page.text and "secret" not in page.text
            assert "Reconcile it with evidence in Telegram" in page.text
            assert "never resends" in page.text
            assert "Accepted schedule" in page.text or "accepted schedule" in page.text
            assert "Showing positions 1–10" in page.text
            assert "Partial" in page.text
            receipts_link = next_link(page.text, "/ui/jobs/delivery-job?")
            assert "r=" in receipts_link
            second = await browser.get(receipts_link)
            assert second.status_code == 200
            assert "Showing positions 11–12" in second.text
            assert "Unknown — reconcile" in second.text
            assert "6000" in page.text and "5008" in page.text
            assert 'method="post"' not in page.text and "Resend" not in page.text
            assert "action=" not in page.text


@pytest.mark.asyncio
async def test_job_and_receipt_access_follow_isolation_account_and_read_policy(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles={
            "work": Profile(kind="user", identity={"phone": "private"}),
            "personal": Profile(kind="user", identity={"phone": "private"}),
        },
    )
    async with running(settings) as panel:
        store = panel.runtime.store
        with store.db:
            job(store, settings, "visible", kind="delivery", payload={"recipients": ["100"]})
            job(store, settings, "foreign", profile="personal", kind="delivery")
            old = {
                "id": "old-account",
                "profile_id": "work",
                "account": {"generation": "previous", "kind": "user", "identity": {}},
                "kind": "sync",
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "payload": {},
                "progress": 1,
                "next_run": 0.0,
                "error": None,
                "result": None,
            }
            store.put("jobs", old)
        async with client(panel) as browser:
            await login(browser, panel)
            assert (await browser.get("/ui/jobs/foreign?profile=work")).status_code == 404
            assert (await browser.get("/ui/jobs/visible")).status_code == 404
            body = await browser.get("/ui/jobs/foreign?profile=work")
            assert "foreign" not in body.text
            assert (await browser.get("/ui/jobs/old-account?profile=work")).status_code == 403

            page = await browser.get("/ui/jobs")
            assert "visible" in page.text
            assert "Restricted under the current policy" in page.text
            assert "(<code>account_changed</code>)" in page.text

    settings.profiles["work"].read_mode = "selected"
    async with running(settings) as panel:
        with panel.runtime.store.db:
            job(
                panel.runtime.store,
                settings,
                "revoked",
                kind="delivery",
                payload={"recipients": ["200"]},
            )
        async with client(panel) as browser:
            await login(browser, panel)
            revoked = await browser.get("/ui/jobs/revoked?profile=work")
            assert revoked.status_code == 403
            assert "200" not in revoked.text
            listed = await browser.get("/ui/jobs")
            assert "revoked" in listed.text and "read_not_allowed" in listed.text

    settings.exposure_mode = "selected"
    settings.exposed_tools = []
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            assert (await browser.get("/ui/jobs")).status_code == 403
            assert "tool_not_exposed" in (await browser.get("/ui/jobs")).text


@pytest.mark.asyncio
async def test_restart_preserves_jobs_and_receipts_and_reports_unknown_outcome(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles={"work": Profile(kind="user", identity={"phone": "private"})},
    )
    async with running(settings) as panel:
        with panel.runtime.store.db:
            job(panel.runtime.store, settings, "crash", kind="delivery", status="running")
            delivery(panel.runtime.store, "crash", 0, "sending", message_id="7001")
        async with client(panel) as browser:
            await login(browser, panel)
            assert "Running" in (await browser.get("/ui/jobs")).text

    async with running(settings) as restarted:
        async with client(restarted) as browser:
            await login(browser, restarted)
            listed = await browser.get("/ui/jobs")
            assert listed.status_code == 200
            assert "crash" in listed.text and "Needs review" in listed.text
            detail = await browser.get("/ui/jobs/crash?profile=work")
            assert detail.status_code == 200
            assert "Needs review" in detail.text
            assert "Unknown — reconcile" in detail.text
            assert "7001" in detail.text
            assert "do not resend" in detail.text

    idle = Runtime(settings, adapter_factory=no_telegram)
    try:
        panel = Panel(settings, idle, TOKEN)
        async with client(panel) as browser:
            await login(browser, panel)
            page = await browser.get("/ui/jobs")
            assert page.status_code == 503
            assert "Owner unavailable" in page.text
            assert "crash" not in page.text
    finally:
        await idle.close()
