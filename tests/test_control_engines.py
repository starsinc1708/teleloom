import hashlib
import json
from contextlib import nullcontext
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from teleloom import control, control_engines, transcription
from teleloom.build_info import build_info
from teleloom.config import Settings
from teleloom.models import TeleloomError
from teleloom.transcription import TranscriptionManager

TOKEN = "mcp-bearer-that-must-never-authorize-management"
CATALOGUE = [{"name": "messages_get", "summary": "Read messages", "writes": False}]
KEY = "fictional-provider-key-9f2"
BASE = {
    "port": 8799,
    "jev_model": "jev-latest",
    "exposure_mode": "all",
    "exposed_tools": [],
    "unknown_root": {"kept": "exactly"},
    "profiles": {
        "personal": {
            "kind": "user",
            "generation": "g1",
            "read_mode": "selected",
            "read_chats": ["10"],
            "jev_chats": ["10"],
            "transcription_chats": ["10"],
            "transcription_external_chats": ["10"],
            "transcription": {
                "openai_model": "whisper-1",
                "groq_model": "whisper-large-v3-turbo",
                "external_daily_calls": 2,
                "external_daily_bytes": 1000,
            },
        },
        "ops": {
            "kind": "bot",
            "generation": "g2",
            "read_mode": "all",
            "transcription_chats": ["20"],
        },
    },
}


class FakeCredentials:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.written: list[tuple[str, str]] = []

    def get(self, profile: str, name: str) -> str | None:
        return self.values.get(f"{profile}:{name}")

    def set(self, profile: str, name: str, value: str) -> None:
        self.written.append((profile, name))
        self.values[f"{profile}:{name}"] = value

    def delete(self, profile: str, name: str) -> None:
        self.values.pop(f"{profile}:{name}", None)


class FakeLifecycle:
    def __init__(self) -> None:
        self.stopped = 0
        self.started: list[str] = []

    async def stop(self, settings: Settings) -> None:
        self.stopped += 1

    async def start(self, settings: Settings, handoff: str) -> None:
        self.started.append(handoff)

    async def observed(self, settings: Settings) -> dict[str, Any]:
        return {
            "service": "teleloom",
            "owner_id": "owner-1",
            "workspace_id": control.workspace_id(settings),
            "version": "0.5.0",
            "build": build_info(),
            "tools": ["messages_get"],
        }


def config_file(tmp_path, data=None):
    (tmp_path / "config.json").write_text(json.dumps(data or BASE), encoding="utf-8")
    return tmp_path / "config.json"


def model_dir(tmp_path) -> str:
    directory = tmp_path / "model"
    directory.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (directory / name).write_bytes(b"x")
    return str(directory)


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fake_engines(monkeypatch, credentials, *, installed=True):
    monkeypatch.setattr(control_engines, "Secrets", lambda: credentials)
    monkeypatch.setattr(control_engines, "_installed", lambda name: installed)
    monkeypatch.setattr(transcription, "_installed", lambda name: installed)


def engine_reader(tmp_path, credentials) -> TranscriptionManager:
    """The real public reader over the saved config; only its store/adapter are faked."""
    jobs = type(
        "Jobs",
        (),
        {
            "settings": Settings.load(tmp_path),
            "store": type("Store", (), {"db": nullcontext(), "jobs": staticmethod(lambda: [])})(),
        },
    )()
    return TranscriptionManager(jobs, None, credentials)


def make_control(tmp_path, lifecycle=None) -> control.Control:
    return control.Control(
        Settings.load(tmp_path), CATALOGUE, lifecycle=lifecycle or FakeLifecycle()
    )


async def open_page(instance) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=instance), base_url=instance.origin)


async def login(client: httpx.AsyncClient, instance) -> None:
    fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
    response = await client.post(
        "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
    )
    assert response.status_code == 204, response.text


def csrf_of(body: str) -> str:
    return body.split('name="csrf" value="', 1)[1].split('"', 1)[0]


async def review_csrf(client, profile: str = "personal", **overrides: str) -> str:
    """A token only exists on the reviewed form, exactly as the owner flow produces it."""
    fields = {
        "jev_model": "jev-latest",
        "local_model_path": "",
        "openai_endpoint": "",
        "openai_model": "whisper-1",
        "groq_model": "whisper-large-v3-turbo",
        **overrides,
    }
    page = await client.get(
        "/control/engines", params={"profile": profile, "review": "1", **fields}
    )
    assert page.status_code == 200, page.text
    return csrf_of(page.text)


async def post_apply(client, fields: dict[str, str]):
    return await client.post(
        "/control/engines/apply",
        data=fields,
        headers={"Origin": str(client.base_url).rstrip("/")},
    )


@pytest.mark.asyncio
async def test_page_reports_states_and_next_steps_without_any_provider_call(tmp_path, monkeypatch):
    config_file(tmp_path)
    credentials = FakeCredentials({"personal:groq_transcription_key": KEY})
    fake_engines(monkeypatch, credentials)
    instance = make_control(tmp_path)
    try:
        async with await open_page(instance) as client:
            await login(client, instance)
            page = await client.get("/control/engines", params={"profile": "personal"})
            assert page.status_code == 200 and "AI and transcription engines" in page.text
            assert "installed yes · configured yes · available yes" in page.text  # groq
            assert "available no" in page.text  # openai: no endpoint
            assert "No provider is called" in page.text
            assert KEY not in page.text and "fictional" not in page.text
            # Telegram audio is a user capability; the bot profile is told so.
            bot = await client.get("/control/engines", params={"profile": "ops"})
            assert "user-account capability" in bot.text
            # A package is not health, and nothing is installed from this page.
            assert credentials.written == []

        credentials.values.pop("personal:groq_transcription_key")
        uninstalled = FakeCredentials()
        fake_engines(monkeypatch, uninstalled, installed=False)
        missing = make_control(tmp_path)
        try:
            async with await open_page(missing) as client:
                await login(client, missing)
                page = await client.get("/control/engines", params={"profile": "personal"})
                assert "Install teleloom[jev]" in page.text
                assert "Install teleloom[transcription]" in page.text
        finally:
            missing.close()
    finally:
        instance.close()


@pytest.mark.asyncio
async def test_apply_writes_only_engine_fields_keeps_grants_and_new_engine_is_observable(
    tmp_path, monkeypatch
):
    path = config_file(tmp_path)
    credentials = FakeCredentials()
    fake_engines(monkeypatch, credentials)
    lifecycle = FakeLifecycle()
    instance = make_control(tmp_path, lifecycle)
    model = model_dir(tmp_path)
    try:
        async with await open_page(instance) as client:
            await login(client, instance)
            review = await client.get(
                "/control/engines",
                params={
                    "profile": "personal",
                    "review": "1",
                    "jev_model": "jev-2026-01",
                    "local_model_path": model,
                    "openai_endpoint": "",
                    "openai_model": "whisper-1",
                    "groq_model": "whisper-large-v3",
                },
            )
            assert review.status_code == 200
            assert "jev_model: jev-latest -&gt; jev-2026-01" in review.text
            assert "profiles.personal.transcription.groq_model" in review.text

            response = await post_apply(
                client,
                {
                    "csrf": csrf_of(review.text),
                    "revision": digest(path),
                    "profile": "personal",
                    "jev_model": "jev-2026-01",
                    "local_model_path": model,
                    "openai_endpoint": "",
                    "openai_model": "whisper-1",
                    "groq_model": "whisper-large-v3",
                    "groq_key": KEY,
                },
            )
            assert response.status_code == 200, response.text
            assert "Applied and verified" in response.text
            assert KEY not in response.text
            assert lifecycle.stopped == 1 and len(lifecycle.started) == 1
    finally:
        instance.close()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["jev_model"] == "jev-2026-01"
    assert saved["unknown_root"] == {"kept": "exactly"} and saved["port"] == 8799
    assert saved["profiles"]["personal"]["transcription"] == {
        "provider": "local",
        "openai_model": "whisper-1",
        "groq_model": "whisper-large-v3",
        "external_daily_calls": 2,
        "external_daily_bytes": 1000,
        "local_model_path": model,
        "openai_endpoint": None,
    }
    # Grants and the other profile are untouched by an engine selection.
    assert saved["profiles"]["personal"]["jev_chats"] == ["10"]
    assert saved["profiles"]["personal"]["transcription_external_chats"] == ["10"]
    assert saved["profiles"]["ops"] == BASE["profiles"]["ops"]
    assert credentials.written == [("personal", "groq_transcription_key")]
    assert (control.record(Settings.load(tmp_path)) or {}).get("phase") == "verified"
    assert KEY not in path.read_text(encoding="utf-8")

    # The existing public capability reader sees the new engine without any provider call.
    providers = engine_reader(tmp_path, credentials).capabilities("personal")["providers"]
    assert (
        providers["groq"]["available"] is True and providers["groq"]["model"] == "whisper-large-v3"
    )
    assert providers["local"]["available"] is True
    assert providers["openai"]["available"] is False  # no endpoint: no external fallback


@pytest.mark.asyncio
async def test_unsupported_endpoint_model_and_absent_model_refuse_before_stop(
    tmp_path, monkeypatch
):
    path = config_file(tmp_path)
    original = path.read_bytes()
    credentials = FakeCredentials()
    fake_engines(monkeypatch, credentials)
    lifecycle = FakeLifecycle()
    instance = make_control(tmp_path, lifecycle)
    try:
        async with await open_page(instance) as client:
            await login(client, instance)
            csrf = await review_csrf(client)
            base = {
                "csrf": csrf,
                "revision": digest(path),
                "profile": "personal",
                "jev_model": "jev-latest",
                "local_model_path": "",
                "openai_model": "whisper-1",
                "groq_model": "whisper-large-v3-turbo",
            }
            plain = await post_apply(
                client, {**base, "openai_endpoint": "http://provider.example.com/v1"}
            )
            assert plain.status_code == 422 and "invalid_endpoint" in plain.text
            absent = await post_apply(
                client, {**base, "openai_endpoint": "", "local_model_path": str(tmp_path / "gone")}
            )
            assert absent.status_code == 422 and "engine_unavailable" in absent.text
            spaced = await post_apply(
                client,
                {**base, "openai_endpoint": "", "groq_model": "whisper", "groq_key": "two words"},
            )
            assert spaced.status_code == 422 and "invalid_credential" in spaced.text
            blank = await post_apply(client, {**base, "openai_endpoint": "", "jev_model": " "})
            assert blank.status_code == 422 and "invalid_model" in blank.text
            unknown = await post_apply(client, {**base, "openai_endpoint": "", "profile": "nope"})
            assert unknown.status_code == 422 and "profile_not_found" in unknown.text
    finally:
        instance.close()
    assert lifecycle.stopped == 0 and path.read_bytes() == original
    assert credentials.written == []


def test_credential_write_rolls_back_when_config_write_refuses(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    original = path.read_bytes()
    credentials = FakeCredentials({"personal:groq_transcription_key": "old-key"})
    fake_engines(monkeypatch, credentials)
    proposed = control_engines.validated(
        json.loads(original),
        "personal",
        {
            "provider": "local",
            "local_model_path": "",
            "openai_endpoint": "",
            "openai_model": "whisper-1",
            "groq_model": "whisper-large-v3",
            "jev_model": "jev-latest",
        },
    )

    def disk_full(*args, **kwargs):
        raise OSError(28, "no space")

    monkeypatch.setattr(control_engines, "write_config", disk_full)
    with pytest.raises(OSError):
        control_engines.write(
            Settings.load(tmp_path),
            "personal",
            proposed,
            [
                ("personal", "groq_transcription_key", "new-key"),
                ("personal", "openai_transcription_key", "new-openai"),
            ],
            credentials,
        )
    assert credentials.values["personal:groq_transcription_key"] == "old-key"
    assert "personal:openai_transcription_key" not in credentials.values
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_engines_management_is_owner_session_only(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    original = path.read_bytes()
    credentials = FakeCredentials()
    fake_engines(monkeypatch, credentials)
    lifecycle = FakeLifecycle()
    instance = make_control(tmp_path, lifecycle)
    try:
        async with await open_page(instance) as client:
            assert (
                await client.get("/control/engines", params={"profile": "personal"})
            ).status_code == 401
            # An MCP bearer token is not an owner management session.
            denied = await client.post(
                "/control/engines/apply",
                data={"profile": "personal", "jev_model": "jev-2026-01"},
                headers={"Origin": instance.origin, "Authorization": f"Bearer {TOKEN}"},
            )
            assert denied.status_code == 401
    finally:
        instance.close()
    assert path.read_bytes() == original and credentials.written == [] and lifecycle.stopped == 0


@pytest.mark.asyncio
async def test_key_only_rotation_applies_without_touching_config_and_unchanged_is_noop(
    tmp_path, monkeypatch
):
    path = config_file(tmp_path)
    original = path.read_bytes()
    credentials = FakeCredentials()
    fake_engines(monkeypatch, credentials)
    lifecycle = FakeLifecycle()
    instance = make_control(tmp_path, lifecycle)
    try:
        async with await open_page(instance) as client:
            await login(client, instance)
            csrf = await review_csrf(client)
            fields = {
                "csrf": csrf,
                "revision": digest(path),
                "profile": "personal",
                "jev_model": "jev-latest",
                "local_model_path": "",
                "openai_endpoint": "",
                "openai_model": "whisper-1",
                "groq_model": "whisper-large-v3-turbo",
            }
            key_only = await post_apply(client, {**fields, "jev_key": KEY})
            assert key_only.status_code == 200, key_only.text
            assert "Applied and verified" in key_only.text and KEY not in key_only.text
            assert "credential is stored" in key_only.text
            assert credentials.written == [("", "typesafe_api_key")]
            unchanged = await post_apply(client, {**fields, "revision": digest(path)})
            assert unchanged.status_code == 200 and "No change" in unchanged.text
    finally:
        instance.close()
    # Rotation wrote no configuration field, so the reviewed revision stays valid.
    assert path.read_bytes() == original
    assert lifecycle.stopped == 1 and len(lifecycle.started) == 1
    # The secret is not in durable transition metadata either.
    assert KEY not in json.dumps(control.record(Settings.load(tmp_path)))


@pytest.mark.asyncio
async def test_provider_selection_is_persisted_default_and_explicit_override_wins(
    tmp_path, monkeypatch
):
    data = json.loads(json.dumps(BASE))
    data["profiles"]["personal"]["transcription"]["openai_endpoint"] = (
        "https://transcribe.invalid/v1"
    )
    path = config_file(tmp_path, data)
    credentials = FakeCredentials()
    fake_engines(monkeypatch, credentials)
    lifecycle = FakeLifecycle()
    instance = make_control(tmp_path, lifecycle)
    try:
        async with await open_page(instance) as client:
            await login(client, instance)
            review = await client.get(
                "/control/engines",
                params={
                    "profile": "personal",
                    "review": "1",
                    "provider": "openai",
                    "jev_model": "jev-latest",
                    "local_model_path": "",
                    "openai_endpoint": "https://transcribe.invalid/v1",
                    "openai_model": "whisper-1",
                    "groq_model": "whisper-large-v3-turbo",
                },
            )
            assert review.status_code == 200, review.text
            assert (
                "profiles.personal.transcription.provider: &quot;local&quot; -&gt; &quot;openai&quot;"
                in review.text
            )
            applied = await post_apply(
                client,
                {
                    "csrf": csrf_of(review.text),
                    "revision": digest(path),
                    "profile": "personal",
                    "provider": "openai",
                    "jev_model": "jev-latest",
                    "local_model_path": "",
                    "openai_endpoint": "https://transcribe.invalid/v1",
                    "openai_model": "whisper-1",
                    "groq_model": "whisper-large-v3-turbo",
                },
            )
            assert applied.status_code == 200 and "Applied and verified" in applied.text
    finally:
        instance.close()
    saved = Settings.load(tmp_path)
    assert saved.profiles["personal"].transcription.provider == "openai"
    reader = engine_reader(tmp_path, credentials)
    assert reader.capabilities("personal")["default_provider"] == "openai"
    # Omitted provider uses the stored choice (external consent is then required); an
    # explicit provider still wins and never inherits the stored external selection.
    with pytest.raises(TeleloomError) as stored:
        await reader.start("personal", "10", "1")
    assert "upload" in stored.value.message
    with pytest.raises(TeleloomError) as override:
        await reader.start("personal", "10", "1", provider="local")
    assert "local model" in override.value.message
