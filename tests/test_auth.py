import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from telethon import errors

from teleloom.auth import BrowserLogin
from teleloom.models import TeleloomError


class QR:
    def __init__(self, client):
        self.client = client
        self.url = "tg://login?token=SECRET_QR_TOKEN"
        self.expires = datetime.now(UTC) + timedelta(seconds=60)

    async def wait(self):
        self.client.waiting.set()
        await self.client.scanned.wait()
        self.client.authorized = True
        if getattr(self.client, "needs_password", False):
            self.client.authorized = False
            raise errors.SessionPasswordNeededError(request=None)
        return SimpleNamespace(id=42, username="owner", first_name="Owner")

    async def recreate(self):
        self.client.refreshes = getattr(self.client, "refreshes", 0) + 1
        self.client.scanned.clear()
        self.expires = datetime.now(UTC) + timedelta(seconds=60)


class TelegramLoginAPI:
    def __init__(self):
        self.waiting = asyncio.Event()
        self.scanned = asyncio.Event()
        self.authorized = False
        self.qr = QR(self)

    async def qr_login(self):
        return self.qr

    async def get_me(self):
        return SimpleNamespace(id=42, username="owner", first_name="Owner")

    async def is_user_authorized(self):
        return self.authorized

    async def sign_in(self, *, password):
        if password != "cloud-secret":
            raise errors.PasswordHashInvalidError(request=None)
        self.authorized = True


async def phase(login, expected):
    async with asyncio.timeout(2):
        while login.state["phase"] != expected:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_browser_qr_login_requires_local_session_and_finishes_after_scan():
    telegram = TelegramLoginAPI()
    login = BrowserLogin(telegram, origin="http://127.0.0.1:8766", timeout=10)
    task = asyncio.create_task(login.run())
    await telegram.waiting.wait()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=login.app), base_url=login.origin
    ) as browser:
        assert (await browser.get("/state")).status_code == 403
        page = await browser.get(login.entry_path)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "SECRET_QR_TOKEN" not in page.text
        state = (await browser.get("/state")).json()
        assert state["phase"] == "qr"
        assert "<svg" in state["qr_svg"]
        telegram.scanned.set()
        identity = await task
        assert identity["id"] == "42"
        assert (await browser.get("/state")).json()["phase"] == "done"


@pytest.mark.asyncio
async def test_browser_cloud_password_retries_and_rejects_cross_origin_posts():
    telegram = TelegramLoginAPI()
    telegram.needs_password = True
    login = BrowserLogin(telegram, origin="http://127.0.0.1:8766", timeout=10)
    task = asyncio.create_task(login.run())
    await telegram.waiting.wait()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=login.app), base_url=login.origin
    ) as browser:
        await browser.get(login.entry_path)
        telegram.scanned.set()
        await phase(login, "password")
        headers = {"origin": login.origin, "x-csrf-token": login.csrf}
        assert (
            await browser.post("/password", json={"password": "cloud-secret"})
        ).status_code == 403
        assert (
            await browser.post(
                "/password",
                json={"password": "cloud-secret"},
                headers={**headers, "origin": "https://evil.invalid"},
            )
        ).status_code == 403
        assert (await browser.get("/state", headers={"host": "evil.invalid"})).status_code == 403
        await browser.post("/password", json={"password": "wrong"}, headers=headers)
        async with asyncio.timeout(2):
            while login.state.get("message") != "Incorrect password. Try again.":
                await asyncio.sleep(0.01)
        response = await browser.post(
            "/password", json={"password": "cloud-secret"}, headers=headers
        )
        assert "cloud-secret" not in response.text
        assert (await task)["id"] == "42"


@pytest.mark.asyncio
async def test_expired_qr_refreshes_and_cancel_does_not_authorize():
    telegram = TelegramLoginAPI()
    telegram.qr.expires = datetime.now(UTC) + timedelta(milliseconds=30)
    login = BrowserLogin(telegram, origin="http://127.0.0.1:8766", timeout=10)
    task = asyncio.create_task(login.run())
    async with asyncio.timeout(2):
        while not getattr(telegram, "refreshes", 0):
            await asyncio.sleep(0.01)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=login.app), base_url=login.origin
    ) as browser:
        await browser.get(login.entry_path)
        await browser.post("/cancel", headers={"origin": login.origin, "x-csrf-token": login.csrf})
    with pytest.raises(TeleloomError, match="cancelled"):
        await task
    assert not telegram.authorized


@pytest.mark.asyncio
async def test_qr_flow_has_bounded_deadline():
    login = BrowserLogin(TelegramLoginAPI(), origin="http://127.0.0.1:8766", timeout=0.02)
    with pytest.raises(TeleloomError, match="timed out"):
        await login.run()
    assert login.state["phase"] == "failed"


@pytest.mark.asyncio
async def test_password_rate_limit_reports_safe_retry_time():
    class LimitedTelegramAPI(TelegramLoginAPI):
        async def sign_in(self, *, password):
            raise errors.FloodWaitError(request=None, capture=30)

    telegram = LimitedTelegramAPI()
    telegram.needs_password = True
    login = BrowserLogin(telegram, origin="http://127.0.0.1:8766", timeout=5)
    task = asyncio.create_task(login.run())
    await telegram.waiting.wait()
    telegram.scanned.set()
    await phase(login, "password")
    await login.passwords.put("cloud-secret")
    with pytest.raises(TeleloomError) as failed:
        await task
    assert failed.value.code == "rate_limited"
    assert failed.value.retry_after == 30
    assert "30 seconds" in login.state["message"]
    assert "cloud-secret" not in str(login.state)


@pytest.mark.asyncio
@pytest.mark.parametrize("ui", ["terminal", "browser"])
async def test_terminal_qr_and_unavailable_browser_use_same_password_flow(ui, monkeypatch, capsys):
    from teleloom.auth import qr_login

    telegram = TelegramLoginAPI()
    telegram.needs_password = True
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("webbrowser.open", lambda url: False)

    def scan(qr, **kwargs):
        assert telegram.waiting.is_set()
        telegram.scanned.set()

    async def prompt(label, hidden=True):
        assert hidden
        return passwords.pop(0)

    passwords = ["wrong", "wrong", "cloud-secret"]
    monkeypatch.setattr("qrcode.QRCode.print_ascii", scan)
    monkeypatch.setattr("teleloom.auth.console_prompt", prompt)
    assert (await qr_login(telegram, ui=ui, timeout=5))["id"] == "42"
    assert not passwords
    output = capsys.readouterr().out
    assert "QR expires:" in output
    assert "cloud-secret" not in output
    if ui == "browser":
        assert "Browser could not open" in output
