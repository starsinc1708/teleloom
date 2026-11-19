"""#24: QR/2FA onboarding of a named user profile inside owner control.

Fake Telegram login and credential store; real temporary locks, SQLite and config.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from telethon import errors
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from teleloom import control
from teleloom.build_info import build_info
from teleloom.config import Settings

BASE = {
    "port": 8799,
    "read_timeout_seconds": 30,
    "profiles": {},
    "exposure_mode": "all",
    "exposed_tools": [],
}
SECRET_HASH = "api-hash-must-not-leak"
PASSWORD = "cloud-secret"


@pytest.fixture(autouse=True)
def isolated_session_locks(tmp_path, monkeypatch):
    monkeypatch.setattr("teleloom.session_state.lock_directory", lambda: tmp_path / "session-locks")


class FakeCredentials:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, profile, name):
        return self.values.get(f"{profile}:{name}")

    def set(self, profile, name, value):
        self.values[f"{profile}:{name}"] = value


class FakeLifecycle:
    def __init__(self, owner="owner-1"):
        self.stopped = 0
        self.started: list[str] = []
        self.owner = owner

    async def stop(self, settings):
        self.stopped += 1

    async def start(self, settings, handoff):
        self.started.append(handoff)

    async def observed(self, settings):
        return {
            "service": "teleloom",
            "owner_id": self.owner,
            "workspace_id": control.workspace_id(settings),
            "version": "0.5.0",
            "build": build_info(),
            "scope": {
                "profiles": [
                    {
                        "id": name,
                        "identity": profile.identity,
                        "kind": profile.kind,
                        "backend": "mtproto" if profile.kind == "user" else profile.bot_backend,
                        "grants": profile.grants(),
                        "capabilities": {"polling_enabled": profile.polling},
                    }
                    for name, profile in Settings.load(settings.data_dir).profiles.items()
                ]
            },
        }


class QR:
    def __init__(self, client):
        self.client = client
        self.url = "tg://login?token=SECRET-QR-TOKEN"
        self.expires = datetime.now(UTC) + timedelta(seconds=60)

    async def wait(self):
        self.client.waiting.set()
        await self.client.scanned.wait()
        if self.client.password_required and not self.client.authorized:
            self.client.scanned.clear()
            raise errors.SessionPasswordNeededError(request=None)
        self.client.authorized = True
        return SimpleNamespace(id=42, username="owner", first_name="Owner")

    async def recreate(self):
        self.client.refreshes += 1
        self.url = "tg://login?token=REFRESHED-QR"
        self.client.scanned.clear()
        self.expires = datetime.now(UTC) + timedelta(seconds=60)


class FakeTelegram:
    def __init__(self, *, password_required=False):
        self.waiting = asyncio.Event()
        self.scanned = asyncio.Event()
        self.authorized = False
        self.password_required = password_required
        self.refreshes = 0
        self.qr = QR(self)
        self.session = StringSession()
        self.session.set_dc(2, "127.0.0.1", 443)
        self.session.auth_key = AuthKey(b"k" * 256)

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def qr_login(self):
        return self.qr

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(id=42, username="owner", first_name="Owner")

    async def sign_in(self, *, password):
        if password != PASSWORD:
            raise errors.PasswordHashInvalidError(request=None)
        self.authorized = True


def config(tmp_path, profiles=None):
    data = {**BASE, "profiles": profiles or {}}
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path / "config.json"


def make(tmp_path, *, telegram=None, stored=None):
    settings = Settings.load(tmp_path)
    lifecycle = FakeLifecycle()
    instance = control.Control(settings, [], lifecycle=lifecycle)
    instance.user.credentials = FakeCredentials(stored)
    instance.user.connector = lambda *args: telegram or FakeTelegram()
    instance.user.timeout = 5
    return instance, lifecycle


def client_for(instance, **kwargs):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url=instance.origin, **kwargs
    )


async def sign_in(client, instance):
    fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
    assert (
        await client.post(
            "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
        )
    ).status_code == 204


def csrf_of(body):
    return body.split('name="csrf" value="', 1)[1].split('"', 1)[0]


async def start_login(client, instance, **fields):
    page = await client.get("/control/user")
    return await client.post(
        "/control/user/start",
        data={
            "csrf": csrf_of(page.text),
            "revision": page.text.split('name="revision" value="', 1)[1].split('"', 1)[0],
            **fields,
        },
        headers={"Origin": instance.origin},
    )


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def no_secret_in(*paths):
    for path in paths:
        if path.is_file():
            raw = path.read_bytes()
            for secret in (PASSWORD, SECRET_HASH, "SECRET-QR-TOKEN"):
                assert secret.encode() not in raw, f"{secret} leaked into {path.name}"


@pytest.mark.asyncio
async def test_validation_happens_before_any_stop_or_login(tmp_path):
    path = config(tmp_path, {"personal": {"kind": "user", "identity": {"id": "7"}}})
    telegram = FakeTelegram()
    calls = []
    instance, lifecycle = make(tmp_path, telegram=telegram, stored={"newbie:api_id": "1"})
    instance.user.connector = lambda *args: calls.append(args) or telegram
    async with client_for(instance) as client:
        assert (await client.get("/control/user")).status_code == 401
        await sign_in(client, instance)
        page = await client.get("/control/user")
        assert page.status_code == 200 and "no chats" in page.text
        assert "personal" in page.text

        invalid = await start_login(client, instance, name="Bad Name")
        assert invalid.status_code == 422 and "invalid_name" in invalid.text
        exists = await start_login(client, instance, name="personal")
        assert exists.status_code == 422 and "profile_exists" in exists.text
        missing = await start_login(client, instance, name="newbie")  # api_hash absent
        assert missing.status_code == 422 and "credentials_missing" in missing.text

        forged = await client.post(
            "/control/user/start",
            data={"name": "newbie", "csrf": "x"},
            headers={"Origin": instance.origin},
        )
        assert forged.status_code == 403
        cross = await client.post(
            "/control/user/start",
            data={"name": "newbie", "csrf": csrf_of(page.text)},
            headers={"Origin": "https://evil.invalid"},
        )
        assert cross.status_code == 403

        assert calls == [] and lifecycle.stopped == 0
        assert json.loads(path.read_text())["profiles"]["personal"]["identity"] == {"id": "7"}


@pytest.mark.asyncio
async def test_qr_login_saves_selected_empty_profile_and_verified_owner(tmp_path):
    path = config(tmp_path)
    telegram = FakeTelegram()
    instance, lifecycle = make(
        tmp_path, telegram=telegram, stored={"newbie:api_id": "1", "newbie:api_hash": SECRET_HASH}
    )
    async with client_for(instance) as client:
        await sign_in(client, instance)
        response = await start_login(client, instance, name="newbie")
        assert response.status_code == 202
        await until(lambda: instance.user.flow and instance.user.flow.state["phase"] == "qr")
        state = (await client.get("/control/user/state")).json()
        assert state["phase"] == "qr" and "<svg" in state["qr_svg"] and state["expires"]
        assert "SECRET-QR-TOKEN" not in response.text

        telegram.scanned.set()
        await asyncio.wait_for(instance.user.task, 5)

        final = (await client.get("/control/user/state")).json()
        assert final["phase"] == "done" and final["identity"]["id"] == "42", final
        saved = json.loads(path.read_text())["profiles"]["newbie"]
        assert saved["read_mode"] == "selected" and saved["read_chats"] == []
        assert saved["identity"] == {"id": "42", "username": "owner", "name": "Owner"}
        assert all(
            saved[key] == []
            for key in (
                "send_chats",
                "mutation_chats",
                "broadcast_chats",
                "sync_chats",
                "jev_chats",
                "event_chats",
                "transcription_chats",
                "manage_scopes",
                "file_roots",
            )
        )
        assert instance.user.credentials.get("newbie", "session")
        assert lifecycle.stopped == 1 and len(lifecycle.started) == 1
        stored = control.record(instance.settings) or {}
        assert stored["phase"] == "verified" and stored["owner"]["owner_id"] == "owner-1"
        assert not stored["handoff"]
        no_secret_in(path, tmp_path / "workspace.sqlite")


@pytest.mark.asyncio
async def test_two_factor_wrong_password_is_explicit_and_password_is_never_stored(tmp_path):
    path = config(tmp_path)
    telegram = FakeTelegram(password_required=True)
    instance, _ = make(
        tmp_path, telegram=telegram, stored={"secure:api_id": "1", "secure:api_hash": SECRET_HASH}
    )
    async with client_for(instance) as client:
        await sign_in(client, instance)
        assert (await start_login(client, instance, name="secure")).status_code == 202
        await until(lambda: instance.user.flow and instance.user.flow.state["phase"] == "qr")
        telegram.scanned.set()
        await until(lambda: instance.user.flow.state["phase"] == "password")
        page = (await client.get("/control/user")).text
        csrf = csrf_of(page)

        denied = await client.post(
            "/control/user/password",
            json={"password": PASSWORD},
            headers={"Origin": instance.origin, "X-CSRF-Token": "wrong"},
        )
        assert denied.status_code == 403
        wrong = await client.post(
            "/control/user/password",
            json={"password": "not-it"},
            headers={"Origin": instance.origin, "X-CSRF-Token": csrf},
        )
        assert wrong.status_code == 202
        await until(
            lambda: instance.user.flow.state.get("message") == "Incorrect password. Try again."
        )
        assert (await client.get("/control/user/state")).json()["phase"] == "password"

        good = await client.post(
            "/control/user/password",
            json={"password": PASSWORD},
            headers={"Origin": instance.origin, "X-CSRF-Token": csrf},
        )
        assert good.status_code == 202
        await asyncio.wait_for(instance.user.task, 5)
        assert instance.user.snapshot()["phase"] == "done"
        no_secret_in(path, tmp_path / "workspace.sqlite")


@pytest.mark.asyncio
async def test_cancel_restores_owner_without_overwriting_working_credentials(tmp_path):
    path = config(tmp_path, {"personal": {"kind": "user", "identity": {"id": "7"}}})
    telegram = FakeTelegram()
    instance, lifecycle = make(
        tmp_path,
        telegram=telegram,
        stored={
            "personal:api_id": "1",
            "personal:api_hash": SECRET_HASH,
            "personal:session": "WORKING-SESSION",
        },
    )
    async with client_for(instance) as client:
        await sign_in(client, instance)
        assert (
            await start_login(client, instance, name="personal", replace="1")
        ).status_code == 202
        await until(lambda: telegram.waiting.is_set())
        csrf = csrf_of((await client.get("/control/user")).text)
        cancelled = await client.post(
            "/control/user/cancel", headers={"Origin": instance.origin, "X-CSRF-Token": csrf}
        )
        assert cancelled.status_code == 200
        await asyncio.wait_for(instance.user.task, 5)
        final = (await client.get("/control/user/state")).json()
        assert final["phase"] == "cancelled" and "auth_cancelled" in final["message"]
        assert instance.user.credentials.get("personal", "session") == "WORKING-SESSION"
        assert json.loads(path.read_text())["profiles"]["personal"]["identity"] == {"id": "7"}
        assert lifecycle.stopped == 1 and len(lifecycle.started) == 1


@pytest.mark.asyncio
async def test_conflict_and_timeout_fail_closed_without_writing(tmp_path):
    path = config(tmp_path, {"personal": {"kind": "user", "identity": {"id": "42"}}})
    telegram = FakeTelegram()
    instance, _ = make(
        tmp_path, telegram=telegram, stored={"second:api_id": "1", "second:api_hash": SECRET_HASH}
    )
    async with client_for(instance) as client:
        await sign_in(client, instance)
        assert (await start_login(client, instance, name="second")).status_code == 202
        await until(lambda: instance.user.flow and instance.user.flow.state["phase"] == "qr")
        telegram.scanned.set()
        await asyncio.wait_for(instance.user.task, 5)
        final = instance.user.snapshot()
        assert final["phase"] == "failed" and "account_in_use" in final["message"]
        assert instance.user.credentials.get("second", "session") is None
        assert "second" not in json.loads(path.read_text())["profiles"]

    (tmp_path / "workspace.sqlite").unlink(missing_ok=True)
    slow = FakeTelegram()  # never scanned: the flow's own deadline must fail explicitly
    instance, _ = make(
        tmp_path, telegram=slow, stored={"third:api_id": "1", "third:api_hash": SECRET_HASH}
    )
    instance.user.timeout = 0.2
    async with client_for(instance) as client:
        await sign_in(client, instance)
        assert (await start_login(client, instance, name="third")).status_code == 202
        await asyncio.wait_for(instance.user.task, 5)
        final = instance.user.snapshot()
        assert final["phase"] == "failed" and "auth_timeout" in final["message"]
        assert "third" not in json.loads(path.read_text())["profiles"]


@pytest.mark.asyncio
async def test_owner_busy_refuses_before_login(tmp_path):
    path = config(tmp_path)
    instance, lifecycle = make(tmp_path, stored={"busy:api_id": "1", "busy:api_hash": SECRET_HASH})
    holder = control.ownership(instance.settings)
    holder.acquire()
    try:
        async with client_for(instance) as client:
            await sign_in(client, instance)
            assert (await start_login(client, instance, name="busy")).status_code == 202
            await asyncio.wait_for(instance.user.task, 5)
            final = instance.user.snapshot()
            assert final["phase"] == "failed" and "owner_busy" in final["message"]
    finally:
        holder.release()
    assert "busy" not in json.loads(path.read_text())["profiles"]
    assert instance.user.credentials.get("busy", "session") is None
    assert lifecycle.stopped == 1 and lifecycle.started == []


@pytest.mark.asyncio
async def test_failed_config_save_restores_previous_session_and_restarts(tmp_path, monkeypatch):
    """A save failure must not cost the replaced profile its working credential."""
    path = config(tmp_path, {"personal": {"kind": "user", "identity": {"id": "7"}}})
    telegram = FakeTelegram()
    instance, lifecycle = make(
        tmp_path,
        telegram=telegram,
        stored={
            "personal:api_id": "1",
            "personal:api_hash": SECRET_HASH,
            "personal:session": "WORKING-SESSION",
        },
    )

    def no_space(*args):
        raise OSError(28, "no space left")

    monkeypatch.setattr("teleloom.control_user.write_config", no_space)
    async with client_for(instance) as client:
        await sign_in(client, instance)
        assert (
            await start_login(client, instance, name="personal", replace="1")
        ).status_code == 202
        await until(lambda: instance.user.flow and instance.user.flow.state["phase"] == "qr")
        telegram.scanned.set()
        await asyncio.wait_for(instance.user.task, 5)
        final = instance.user.snapshot()
        assert final["phase"] == "failed" and "save_failed" in final["message"], final
    assert instance.user.credentials.get("personal", "session") == "WORKING-SESSION"
    assert json.loads(path.read_text())["profiles"]["personal"]["identity"] == {"id": "7"}
    assert lifecycle.stopped == 1 and len(lifecycle.started) == 1
    no_secret_in(path, tmp_path / "workspace.sqlite")


class RefusingCredentials(FakeCredentials):
    def set(self, profile, name, value):
        from teleloom.models import TeleloomError

        raise TeleloomError(
            "credentials_unavailable", "A secure OS credential backend is required."
        )


@pytest.mark.asyncio
async def test_credential_backend_refusal_is_explicit_and_saves_nothing(tmp_path):
    path = config(tmp_path)
    telegram = FakeTelegram()
    instance, lifecycle = make(
        tmp_path,
        telegram=telegram,
        stored={"locked:api_id": "1", "locked:api_hash": SECRET_HASH},
    )
    instance.user.credentials = RefusingCredentials(instance.user.credentials.values)
    async with client_for(instance) as client:
        await sign_in(client, instance)
        assert (await start_login(client, instance, name="locked")).status_code == 202
        await until(lambda: instance.user.flow and instance.user.flow.state["phase"] == "qr")
        telegram.scanned.set()
        await asyncio.wait_for(instance.user.task, 5)
        final = instance.user.snapshot()
        assert final["phase"] == "failed" and "credentials_unavailable" in final["message"], final
    assert "locked" not in json.loads(path.read_text())["profiles"]
    assert len(lifecycle.started) == 1
    no_secret_in(path, tmp_path / "workspace.sqlite")


@pytest.mark.asyncio
async def test_qr_expiry_refreshes_and_connection_owner_is_kept_until_restart(
    tmp_path, monkeypatch
):
    config(tmp_path)
    telegram = FakeTelegram()
    telegram.qr.expires = datetime.now(UTC) - timedelta(seconds=1)
    instance, lifecycle = make(
        tmp_path, telegram=telegram, stored={"newbie:api_id": "1", "newbie:api_hash": SECRET_HASH}
    )
    observe = lifecycle.observed
    observing = asyncio.Event()
    finish = asyncio.Event()

    async def delayed_observation(settings):
        observing.set()
        await finish.wait()
        return await observe(settings)

    monkeypatch.setattr(lifecycle, "observed", delayed_observation)
    async with client_for(instance) as client:
        await sign_in(client, instance)
        assert (await start_login(client, instance, name="newbie")).status_code == 202
        await until(lambda: telegram.refreshes == 1 and telegram.waiting.is_set())
        await until(
            lambda: instance.user.flow.state.get("expires") == telegram.qr.expires.isoformat()
        )
        telegram.scanned.set()
        await asyncio.wait_for(observing.wait(), 3)
        state = (await client.get("/control/user/state")).json()
        assert state["phase"] == "saving" and "identity" not in state and "qr_svg" not in state
        finish.set()
        await asyncio.wait_for(instance.user.task, 3)
        assert (await client.get("/control/user/state")).json()["phase"] == "done"


@pytest.mark.asyncio
async def test_control_shutdown_cancels_qr_and_restores_owner(tmp_path):
    config(tmp_path)
    telegram = FakeTelegram()
    instance, lifecycle = make(
        tmp_path, telegram=telegram, stored={"newbie:api_id": "1", "newbie:api_hash": SECRET_HASH}
    )
    async with instance.app.router.lifespan_context(instance.app), client_for(instance) as client:
        await sign_in(client, instance)
        assert (await start_login(client, instance, name="newbie")).status_code == 202
        await asyncio.wait_for(telegram.waiting.wait(), 3)
    assert instance.user.task.done()
    assert instance.user.snapshot()["phase"] == "cancelled"
    assert lifecycle.stopped == 1 and len(lifecycle.started) == 1
    assert "newbie" not in Settings.load(tmp_path).profiles
