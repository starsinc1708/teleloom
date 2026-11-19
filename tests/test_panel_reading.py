"""Bounded reading panel: explicit start, frozen pages, current-policy gates."""

import asyncio
import html
import re
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from teleloom.config import Profile, Settings
from teleloom.models import Chat, Message, TeleloomError
from teleloom.panel import Panel
from teleloom.runtime import Runtime
from teleloom.server import create_server
from tests.fakes import TelegramAPI, data

TOKEN = "panel-reading-owner-token"
NOW = datetime.now(UTC)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ReadingAPI(TelegramAPI):
    """23 rows in one readable chat; every fourth carries Telegram-shaped HTML."""

    def __init__(self, *args):
        super().__init__(*args)
        self.rows = [
            Message(
                profile_id=self.profile,
                chat_id="100",
                id=str(index),
                date=NOW - timedelta(minutes=index),
                text=f"Decision {index}"
                if index % 4
                else f'<script>alert({index})</script> decision & "quoted"',
                link=f"https://t.me/example/{index}",
                sender_id="777",
            )
            for index in range(1, 24)
        ]
        self.calls = 0

    async def history(self, chat, **kwargs):
        self.calls += 1
        return await super().history(chat, **kwargs)


@asynccontextmanager
async def running(settings: Settings, factory=ReadingAPI):
    runtime = Runtime(settings, adapter_factory=factory)
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
        "/ui/bootstrap", headers={"Authorization": "Bearer " + TOKEN, "Origin": panel.origin}
    )
    assert issued.status_code == 200, issued.text
    exchanged = await browser.post(
        "/ui/session",
        json={"bootstrap": urlsplit(issued.json()["url"]).fragment},
        headers={"Origin": panel.origin},
    )
    assert exchanged.status_code == 204, exchanged.text


def csrf_of(panel: Panel) -> str:
    return next(iter(panel.sessions.values())).csrf


def fields(csrf: str, **overrides) -> dict:
    values = {
        "csrf": csrf,
        "profile": "work",
        "chats": "100",
        "since": (NOW - timedelta(days=1)).isoformat(),
        "until": (NOW + timedelta(hours=1)).isoformat(),
        "query": "Decision",
        "max_messages": "23",
        "max_characters": "100000",
        "max_requests": "10",
    }
    values.update(overrides)
    return values


def preview_url(**overrides) -> str:
    values = fields("", **overrides)
    values.pop("csrf")
    return "/ui/read?" + urlencode(values)


def next_link(text: str, prefix: str) -> str:
    found = re.search(rf'href="({re.escape(prefix)}[^"]+)"', text)
    assert found, text
    return html.unescape(found.group(1))


async def finished(panel: Panel, job_id: str) -> dict:
    for _ in range(100):
        await asyncio.sleep(0.05)
        job = next((row for row in panel.runtime.store.jobs() if row["id"] == job_id), None)
        if job and job["status"] in {"completed", "failed", "cancelled"}:
            return job
    raise AssertionError("reading job did not finish")


def settings_for(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        port=free_port(),
        profiles={
            "work": Profile(kind="user", read_mode="selected", read_chats=["100"]),
            "personal": Profile(kind="user", read_mode="selected", read_chats=["100"]),
        },
    )


@pytest.mark.asyncio
async def test_preview_start_pages_and_refresh_use_one_frozen_job(tmp_path):
    settings = settings_for(tmp_path)
    settings.exposure_mode = "read-only"
    async with running(settings) as panel:
        async with client(panel) as browser:
            assert (await browser.get("/ui/read")).status_code == 401
            await login(browser, panel)
            # Preview only: explicit profile/chat/period/query/budgets, no job, no read.
            preview = await browser.get(preview_url())
            assert preview.status_code == 200
            assert "Nothing has been read yet" in preview.text
            for expected in ("work", "100", "Decision", "max_messages=23", "max_requests=10"):
                assert expected in preview.text
            assert 'action="/ui/read"' in preview.text and 'method="post"' in preview.text
            assert panel.runtime.store.jobs() == [] and not panel.runtime.adapters

            started = await browser.post(
                "/ui/read",
                data=fields(csrf_of(panel)),
                headers={"Origin": panel.origin},
                follow_redirects=False,
            )
            assert started.status_code == 303, started.text
            location = started.headers["location"]
            assert location.startswith("/ui/read/") and "profile=work" in location
            job_id = location.split("/ui/read/")[1].split("?")[0]
            assert (await finished(panel, job_id))["status"] == "completed"

            first = await browser.get(location)
            assert first.status_code == 200
            assert "no-store" in first.headers["cache-control"]
            nonce = re.search(r"'nonce-([^']+)'", first.headers["content-security-policy"]).group(1)
            assert f'<script nonce="{nonce}">' in first.text
            assert "replaceChildren()" in first.text and "location.reload()" in first.text
            assert "Completed" in first.text
            assert first.text.count('<td style="white-space:pre-wrap">') == 10
            assert "Decision 23" in first.text
            assert "&lt;script&gt;alert(20)&lt;/script&gt;" in first.text
            assert "<script>alert(20)" not in first.text
            assert "<code>100</code>" in first.text and "<code>23</code>" in first.text
            assert "https://t.me/example/23" in first.text
            assert "Collection is incomplete" not in first.text
            assert "23 / 23" in first.text and "100000" in first.text

            second = await browser.get(next_link(first.text, "/ui/read/"))
            assert second.status_code == 200
            assert second.text.count('<td style="white-space:pre-wrap">') == 10
            third = await browser.get(next_link(second.text, "/ui/read/"))
            assert third.status_code == 200
            assert third.text.count('<td style="white-space:pre-wrap">') == 3

            calls = panel.runtime.adapters["work"].calls
            for _ in range(2):
                assert (await browser.get(location)).status_code == 200
                assert (await browser.get(next_link(first.text, "/ui/read/"))).status_code == 200
            # Refresh, paging and back reuse the frozen job; nothing was refetched.
            assert panel.runtime.adapters["work"].calls == calls
            assert len(panel.runtime.store.jobs()) == 1


@pytest.mark.asyncio
async def test_reading_gates_deny_csrf_policy_exposure_generation_and_foreign_jobs(tmp_path):
    settings = settings_for(tmp_path)
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            denied = await browser.get(preview_url(chats="200"))
            assert denied.status_code == 200 and "read_not_allowed" in denied.text
            assert 'method="post"' not in denied.text

            no_csrf = await browser.post(
                "/ui/read", data=fields("wrong"), headers={"Origin": panel.origin}
            )
            assert no_csrf.status_code == 403 and panel.runtime.store.jobs() == []
            start = await browser.post(
                "/ui/read",
                data=fields(csrf_of(panel)),
                headers={"Origin": panel.origin},
                follow_redirects=False,
            )
            job_id = start.headers["location"].split("/ui/read/")[1].split("?")[0]
            await finished(panel, job_id)

            assert (await browser.get(f"/ui/read/{job_id}?profile=personal")).status_code == 404
            bad = await browser.get(f"/ui/read/{job_id}?profile=work&p=not-a-cursor")
            assert bad.status_code == 400 and "invalid_cursor" in bad.text
            assert "Decision" not in bad.text

            # A denied target is refused before Telegram is asked to start anything.
            refused = await browser.post(
                "/ui/read",
                data=fields(csrf_of(panel), chats="200"),
                headers={"Origin": panel.origin},
            )
            assert refused.status_code == 403 and "read_not_allowed" in refused.text
            assert len(panel.runtime.store.jobs()) == 1

    settings.exposure_mode = "selected"
    settings.exposed_tools = []
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            # The preview is refused before any start; the job page hides frozen text.
            assert "tool_not_exposed" in (await browser.get(preview_url())).text
            hidden = await browser.get(f"/ui/read/{job_id}?profile=work")
            assert hidden.status_code == 403 and "tool_not_exposed" in hidden.text
            assert "Decision" not in hidden.text

    settings.exposed_tools = ["jobs_status", "messages_search_many_start"]
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            hidden = await browser.get(f"/ui/read/{job_id}?profile=work")
            assert hidden.status_code == 403 and "tool_not_exposed" in hidden.text
            assert "Decision" not in hidden.text

    settings.exposure_mode = "all"
    settings.profiles["work"].generation = "rotated"
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            rotated = await browser.get(f"/ui/read/{job_id}?profile=work")
            assert rotated.status_code == 403 and "account_changed" in rotated.text


@pytest.mark.asyncio
async def test_restart_keeps_originals_readable_without_a_new_collection(tmp_path):
    settings = settings_for(tmp_path)
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            started = await browser.post(
                "/ui/read",
                data=fields(csrf_of(panel)),
                headers={"Origin": panel.origin},
                follow_redirects=False,
            )
            job_id = started.headers["location"].split("/ui/read/")[1].split("?")[0]
            await finished(panel, job_id)
            assert "Decision 23" in (await browser.get(f"/ui/read/{job_id}?profile=work")).text

    # A revoked target closes the whole derived scope, originals stay stored.
    settings.profiles["work"].read_chats = ["999"]
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            revoked = await browser.get(f"/ui/read/{job_id}?profile=work")
            assert revoked.status_code == 403 and "read_not_allowed" in revoked.text
            assert "Decision" not in revoked.text

    # Restored access reads the same durable evidence; nothing is recollected.
    settings.profiles["work"].read_chats = ["100"]
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            restored = await browser.get(f"/ui/read/{job_id}?profile=work")
            assert restored.status_code == 200
            assert "Decision 23" in restored.text and "Completed" in restored.text
            assert not panel.runtime.adapters
            assert len(panel.runtime.store.jobs()) == 1


class PartialAPI(ReadingAPI):
    """Second selected chat resolves but its history read fails."""

    async def resolve(self, target):
        if target == "300":
            return Chat(id="300", title="Second")
        return await super().resolve(target)

    async def history(self, chat, **kwargs):
        if chat == "300":
            raise TeleloomError("chat_unavailable", "The selected dialog was not observed.")
        return await super().history(chat, **kwargs)


@pytest.mark.asyncio
async def test_multichat_partial_coverage_parity_with_mcp_results(tmp_path):
    settings = settings_for(tmp_path)
    settings.profiles["work"].read_chats = ["100", "300"]
    async with running(settings, PartialAPI) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            started = await browser.post(
                "/ui/read",
                data=fields(csrf_of(panel), chats="100 300", max_messages="100"),
                headers={"Origin": panel.origin},
                follow_redirects=False,
            )
            job_id = started.headers["location"].split("/ui/read/")[1].split("?")[0]
            job = await finished(panel, job_id)
            # One failed chat keeps the job completed but marks the collection partial.
            assert job["status"] == "completed" and job["result"]["incomplete"] is True

            page = await browser.get(f"/ui/read/{job_id}?profile=work")
            assert page.status_code == 200
            assert "Collection is incomplete" in page.text
            assert "Per-chat coverage" in page.text
            assert "<code>300</code>" in page.text and "chat_unavailable" in page.text
            assert "Unavailable or failed" in page.text
            assert "Decision 23" in page.text

            # Existing public MCP results read the same frozen job, not a new collection.
            mcp = data(
                await create_server(settings, PartialAPI, runtime=panel.runtime).call_tool(
                    "jobs_results", {"profile_id": "work", "job_id": job_id, "limit": 10}
                )
            )["data"]
            assert [(c["chat_id"], c["status"]) for c in mcp["coverage"]["chats"]] == [
                ("100", "completed"),
                ("300", "failed"),
            ]
            assert mcp["incomplete"] is True and mcp["status"] == "completed"
            reference = re.search(r"Evidence reference</dt><dd><code>([^<]+)", page.text).group(1)
            original = data(
                await create_server(settings, PartialAPI, runtime=panel.runtime).call_tool(
                    "jobs_results",
                    {
                        "profile_id": "work",
                        "job_id": job_id,
                        "evidence_ref": reference,
                        "message_keys": [{"chat_id": "100", "message_id": "23"}],
                    },
                )
            )["data"]
            assert original["items"][0]["text"] == "Decision 23"
            assert original["items"][0]["profile_id"] == "work"
            assert mcp["unavailable"] == [
                {
                    "chat_id": "300",
                    "error": {
                        "code": "chat_unavailable",
                        "message": "The selected dialog was not observed.",
                    },
                }
            ]
            assert page.text.count('<td style="white-space:pre-wrap">') == len(mcp["items"])
            coverage = mcp["coverage"]
            assert f"{coverage['messages']} / {coverage['limits']['messages']}" in page.text
            for item in mcp["items"]:
                assert f"<code>{item['id']}</code>" in page.text
                assert item["text"] in page.text or "&lt;script&gt;" in page.text
            assert panel.runtime.adapters["work"].calls == 1


@pytest.mark.asyncio
async def test_expired_page_cursor_reveals_nothing_and_originals_survive(tmp_path):
    settings = settings_for(tmp_path)
    async with running(settings) as panel:
        async with client(panel) as browser:
            await login(browser, panel)
            started = await browser.post(
                "/ui/read",
                data=fields(csrf_of(panel)),
                headers={"Origin": panel.origin},
                follow_redirects=False,
            )
            location = started.headers["location"]
            job_id = location.split("/ui/read/")[1].split("?")[0]
            await finished(panel, job_id)
            first = await browser.get(location)
            link = next_link(first.text, "/ui/read/")
            cursor = parse_qs(urlsplit(link).query)["p"][0]
            key = f"reading_results:work:{cursor.split('.')[0]}"
            assert panel.runtime.store.state(key)
            with panel.runtime.store.db:
                panel.runtime.store.set_state(
                    key, {**panel.runtime.store.state(key), "expires_at": 0}
                )
            expired = await browser.get(link)
            assert expired.status_code == 400 and "invalid_cursor" in expired.text
            assert "Decision" not in expired.text
            # The durable originals are untouched; a fresh first page still reads them.
            assert "Decision 23" in (await browser.get(location)).text
            assert panel.runtime.adapters["work"].calls == 1


@pytest.mark.asyncio
async def test_budget_stops_and_invalid_numeric_input_are_visible(tmp_path):
    settings = settings_for(tmp_path)
    async with running(settings) as panel, client(panel) as browser:
        await login(browser, panel)
        for values, code in (
            ({"chats": "--100"}, "invalid_id"),
            ({"max_messages": "\u00b2"}, "invalid_budget"),
            ({"max_requests": "1001"}, "invalid_budget"),
        ):
            preview = await browser.get(preview_url(**values))
            assert code in preview.text
            assert 'method="post"' not in preview.text
        assert not panel.runtime.adapters

        started = await browser.post(
            "/ui/read",
            data=fields(csrf_of(panel), max_messages="11"),
            headers={"Origin": panel.origin},
        )
        location = started.headers["location"]
        job_id = urlsplit(location).path.rsplit("/", 1)[1]
        await finished(panel, job_id)
        first = await browser.get(location)
        assert "Collection is incomplete" in first.text
        assert "message_budget" in first.text and "11 / 11" in first.text
        assert first.text.count('<td style="white-space:pre-wrap">') == 10
        second = await browser.get(next_link(first.text, "/ui/read/"))
        assert second.text.count('<td style="white-space:pre-wrap">') == 1
        assert "Next page" not in second.text
