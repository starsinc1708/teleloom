import json
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from teleloom import control as control_module
from teleloom.config import Settings
from teleloom.models import TeleloomError

TOKEN = "123456789:AAfakeTOKENvalue_that_must_never_leak123456"
NEW_TOKEN = "987654321:BBanotherTOKENvalue_that_must_never_leak12345"
OLD_TOKEN = "111111111:CCstoredTOKENvalue_that_must_never_leak1234567"
BASE = {
    "port": 8799,
    "profiles": {
        "robot": {
            "kind": "bot",
            "generation": "keep-this-generation",
            "identity": {"id": "42", "username": "robot", "name": "Robot"},
            "read_mode": "selected",
            "read_chats": ["100"],
            "send_chats": ["100"],
        }
    },
}


class FakeSecrets:
    def __init__(self, stored=None, *, unavailable=False):
        self.stored = dict(stored or {})
        self.unavailable = unavailable

    def get(self, profile, name):
        return self.stored.get(f"{profile}:{name}")

    def set(self, profile, name, value):
        if self.unavailable:
            raise TeleloomError(
                "credentials_unavailable", "A secure OS credential backend is required."
            )
        self.stored[f"{profile}:{name}"] = value


class FakeBotApi:
    """Stands in for getMe/getWebhookInfo: no network, no Telegram state."""

    def __init__(self, *, webhook="", error=None, identity=None):
        self.webhook = webhook
        self.error = error
        self.identity = identity or {"id": "42", "username": "fake_bot", "name": "Fake <Bot>"}
        self.calls = []

    async def __call__(self, token, polling, profile):
        self.calls.append((token, polling))
        if self.error is not None:
            raise self.error
        return self.identity


class FakeLifecycle:
    """Stands in for the owner process: records the stop/start a real one would see."""

    def __init__(self):
        self.stopped = 0
        self.started: list[str] = []

    async def stop(self, settings):
        self.stopped += 1

    async def start(self, settings, handoff):
        self.started.append(handoff)

    async def observed(self, settings):
        return {
            "service": "teleloom",
            "owner_id": "owner-1",
            "workspace_id": control_module.workspace_id(settings),
            "version": "0.5.0",
        }


def setup(tmp_path, *, secrets=None, inspect=None):
    (tmp_path / "config.json").write_text(json.dumps(BASE), encoding="utf-8")
    settings = Settings.load(tmp_path)
    panel = control_module.Control(settings, [], lifecycle=FakeLifecycle())
    store = secrets or FakeSecrets({"robot:bot_token": OLD_TOKEN})
    api = inspect or FakeBotApi()
    panel.bot.secrets, panel.bot.inspect = store, api
    return panel, panel.bot, store, api, panel


@asynccontextmanager
async def signed_in(panel, app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=panel.origin
    ) as client:
        fragment = urlsplit(panel.issue_bootstrap()["url"]).fragment
        response = await client.post(
            "/control/session", json={"bootstrap": fragment}, headers={"Origin": panel.origin}
        )
        assert response.status_code == 204, response.text
        yield client


async def csrf_of(client) -> str:
    body = (await client.get("/control/bot")).text
    return body.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def review_key(body: str) -> str:
    return body.split('name="review" value="', 1)[1].split('"', 1)[0]


async def submit(client, panel, path, csrf, **data):
    return await client.post(
        path,
        data={"csrf": csrf, **data},
        headers={"Origin": panel.origin},
    )


async def review(client, panel, csrf, name="newbot", token=TOKEN, polling=False):
    data = {"profile": name, "token": token}
    if polling:
        data["polling"] = "1"
    return await submit(client, panel, "/control/bot/review", csrf, **data)


async def test_verified_identity_saves_profile_with_empty_grants_and_no_token(tmp_path):
    panel, _, store, api, app = setup(tmp_path)
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        page = (await review(client, panel, csrf, polling=True)).text
        assert "Fake &lt;Bot&gt;" in page
        assert "read policy" in page.lower() and "selected" in page
        assert "Polling</dt><dd>on" in page
        applied = await submit(client, panel, "/control/bot/apply", csrf, review=review_key(page))
        assert applied.status_code == 200, applied.text
        for body in (page, applied.text):
            assert TOKEN not in body

    saved = Settings.load(tmp_path).profiles["newbot"]
    assert saved.kind == "bot" and saved.bot_backend == "bot_api"
    assert saved.read_mode == "selected" and saved.read_chats == []
    assert saved.polling is True and saved.generation
    grants = saved.grants()
    assert grants["read"] == {"mode": "selected", "chat_ids": []}
    assert all(chats == [] for grant, chats in grants.items() if grant != "read")
    assert saved.identity == {"id": "42", "username": "fake_bot", "name": "Fake <Bot>"}
    assert store.stored["newbot:bot_token"] == TOKEN
    assert TOKEN not in (tmp_path / "config.json").read_text(encoding="utf-8")
    assert api.calls == [(TOKEN, True)]
    # Restart preservation: the same profile and generation come back from disk.
    assert Settings.load(tmp_path).profiles["newbot"].generation == saved.generation


async def test_invalid_token_shape_and_leaking_api_error_stay_safe(tmp_path):
    panel, _, store, _, app = setup(
        tmp_path, inspect=FakeBotApi(error=ValueError(f"InvalidToken {TOKEN}"))
    )
    before = (tmp_path / "config.json").read_bytes()
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        bad_shape = await review(client, panel, csrf, token="not-a-token")
        assert bad_shape.status_code == 422 and "not a Telegram Bot API token" in bad_shape.text
        failed = await review(client, panel, csrf)
        assert failed.status_code == 422 and "could not verify" in failed.text
        assert TOKEN not in failed.text
        empty = await submit(client, panel, "/control/bot/apply", csrf, review="forged")
        assert empty.status_code == 410
    assert (tmp_path / "config.json").read_bytes() == before
    assert "newbot:bot_token" not in store.stored


async def test_replace_needs_confirmation_and_keeps_generation_and_grants(tmp_path):
    panel, _, store, _, app = setup(tmp_path)
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        page = (await review(client, panel, csrf, name="robot", token=NEW_TOKEN)).text
        assert 'name="confirm"' in page and "keep-this-generation" not in page
        refused = await submit(client, panel, "/control/bot/apply", csrf, review=review_key(page))
        assert refused.status_code == 422 and "Confirm the replacement" in refused.text
        assert Settings.load(tmp_path).profiles["robot"].generation == "keep-this-generation"
        assert store.stored["robot:bot_token"] == OLD_TOKEN
        applied = await submit(
            client, panel, "/control/bot/apply", csrf, review=review_key(page), confirm="1"
        )
        assert applied.status_code == 200, applied.text
    saved = Settings.load(tmp_path).profiles["robot"]
    assert saved.generation == "keep-this-generation"
    assert saved.send_chats == ["100"] and saved.read_chats == ["100"]
    assert store.stored["robot:bot_token"] == NEW_TOKEN


async def test_cancel_clears_pending_token_and_writes_nothing(tmp_path):
    panel, _, store, _, app = setup(tmp_path)
    before = (tmp_path / "config.json").read_bytes()
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        page = (await review(client, panel, csrf)).text
        key = review_key(page)
        cancelled = await submit(client, panel, "/control/bot/cancel", csrf)
        assert (
            cancelled.status_code == 200
            and "No profile or credential was changed" in cancelled.text
        )
        stale = await submit(client, panel, "/control/bot/apply", csrf, review=key)
        assert stale.status_code == 410
    assert (tmp_path / "config.json").read_bytes() == before
    assert store.stored == {"robot:bot_token": OLD_TOKEN}


async def test_missing_credential_backend_and_busy_owner_fail_closed(tmp_path):
    panel, _, store, _, app = setup(tmp_path, secrets=FakeSecrets(unavailable=True))
    before = (tmp_path / "config.json").read_bytes()
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        page = (await review(client, panel, csrf)).text
        refused = await submit(client, panel, "/control/bot/apply", csrf, review=review_key(page))
        assert refused.status_code == 503 and "credentials_unavailable" in refused.text
        assert TOKEN not in refused.text
    assert (tmp_path / "config.json").read_bytes() == before

    panel, _, store, _, app = setup(tmp_path)
    lock = control_module.ownership(panel.settings)
    lock.acquire()
    try:
        async with signed_in(panel, app) as client:
            csrf = await csrf_of(client)
            page = (await review(client, panel, csrf)).text
            busy = await submit(client, panel, "/control/bot/apply", csrf, review=review_key(page))
            assert busy.status_code == 409 and "owner_busy" in busy.text
    finally:
        lock.release()
    assert (tmp_path / "config.json").read_bytes() == before
    assert "newbot:bot_token" not in store.stored


async def test_webhook_conflict_is_reported_and_never_changed(tmp_path):
    panel, _, store, _, app = setup(
        tmp_path,
        inspect=FakeBotApi(
            error=TeleloomError("webhook_conflict", "Telegram still delivers updates to a webhook.")
        ),
    )
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        response = await review(client, panel, csrf, polling=True)
        assert response.status_code == 422 and "webhook_conflict" in response.text
    assert "newbot:bot_token" not in store.stored


async def test_owner_only_guards_and_csrf(tmp_path):
    panel, _, _, _, app = setup(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=panel.origin
    ) as client:
        assert (await client.get("/control/bot")).status_code == 401
        anonymous = await client.post(
            "/control/bot/review",
            data={"csrf": "x", "profile": "newbot", "token": TOKEN},
            headers={"Origin": panel.origin},
        )
        assert anonymous.status_code == 401
        async with signed_in(panel, app) as session:
            csrf = await csrf_of(session)
            wrong_origin = await session.post(
                "/control/bot/review",
                data={"csrf": csrf, "profile": "newbot", "token": TOKEN},
                headers={"Origin": "http://127.0.0.1:1"},
            )
            assert wrong_origin.status_code == 403
            no_csrf = await session.post(
                "/control/bot/review",
                data={"csrf": "wrong", "profile": "newbot", "token": TOKEN},
                headers={"Origin": panel.origin},
            )
            assert no_csrf.status_code == 403


async def test_different_bot_identity_gets_new_generation_and_reset_grants(tmp_path):
    changed = FakeBotApi(identity={"id": "99", "username": "other", "name": "Other"})
    panel, _, store, _, app = setup(tmp_path, inspect=changed)
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        page = (await review(client, panel, csrf, name="robot", token=NEW_TOKEN)).text
        assert "Different bot identity" in page and "keep-this-generation" not in page
        assert "existing grants" not in page
        applied = await submit(
            client, panel, "/control/bot/apply", csrf, review=review_key(page), confirm="1"
        )
        assert applied.status_code == 200, applied.text
    saved = Settings.load(tmp_path).profiles["robot"]
    assert saved.generation != "keep-this-generation"
    assert saved.identity["id"] == "99" and store.stored["robot:bot_token"] == NEW_TOKEN
    assert saved.read_mode == "selected" and saved.read_chats == [] and saved.send_chats == []


async def test_failed_config_save_restores_previous_token(tmp_path, monkeypatch):
    panel, _, store, _, app = setup(tmp_path)
    before = (tmp_path / "config.json").read_bytes()

    def no_space(self):
        raise OSError(28, "no space left")

    monkeypatch.setattr(Settings, "save", no_space)
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        page = (await review(client, panel, csrf, name="robot", token=NEW_TOKEN)).text
        refused = await submit(
            client, panel, "/control/bot/apply", csrf, review=review_key(page), confirm="1"
        )
        assert refused.status_code == 400 and "save_failed" in refused.text
        assert NEW_TOKEN not in refused.text
    assert store.stored["robot:bot_token"] == OLD_TOKEN
    assert (tmp_path / "config.json").read_bytes() == before


async def test_active_job_blocks_before_any_stop_or_write(tmp_path, monkeypatch):
    panel, _, store, _, app = setup(tmp_path)
    before = (tmp_path / "config.json").read_bytes()
    monkeypatch.setattr(
        control_module,
        "blockers",
        lambda settings: ["1 queued or running job(s) must finish or be paused."],
    )
    async with signed_in(panel, app) as client:
        csrf = await csrf_of(client)
        page = (await review(client, panel, csrf)).text
        refused = await submit(client, panel, "/control/bot/apply", csrf, review=review_key(page))
        assert refused.status_code == 409 and "transition_blocked" in refused.text
        assert "running job" in refused.text and TOKEN not in refused.text
    assert panel.lifecycle.stopped == 0 and panel.lifecycle.started == []
    assert (tmp_path / "config.json").read_bytes() == before
    assert "newbot:bot_token" not in store.stored
