"""#28: named proxy catalogue, explicit per-profile assignment, no direct fallback."""

import json
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from teleloom import adapters, control, control_proxies
from teleloom.config import Profile, Settings
from teleloom.models import TeleloomError
from teleloom.secrets import Secrets
from tests.test_panel_control_policy import (
    digest,
    session_csrf,
    write_config,
)

PASSWORD = "s3cret-pass"
LEGACY = "socks5://legacy-user:legacy-pass@legacy.local:1080"


def fake_keyring(monkeypatch) -> dict[str, str]:
    """Temp credential store: no OS keyring, no plaintext file."""
    stored: dict[str, str] = {}

    class Backend:
        def get_password(self, service: str, key: str) -> str | None:
            return stored.get(f"{service}:{key}")

        def set_password(self, service: str, key: str, value: str) -> None:
            stored[f"{service}:{key}"] = value

    backend = Backend()
    monkeypatch.setattr(Secrets, "backend", staticmethod(lambda: backend))
    return stored


class FakeLifecycle:
    """The restarted owner's own report, including the route it now uses."""

    def __init__(self) -> None:
        self.stopped = 0

    async def stop(self, settings: Settings) -> None:
        self.stopped += 1

    async def start(self, settings: Settings, handoff: str) -> None:
        pass

    async def observed(self, settings: Settings) -> dict[str, Any]:
        from teleloom.build_info import build_info

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
                    {
                        "id": name,
                        "grants": profile.grants(),
                        "proxy": {"name": profile.proxy or "profile default"},
                    }
                    for name, profile in fresh.profiles.items()
                ],
            },
        }


def instance(tmp_path, monkeypatch) -> control.Control:
    write_config(tmp_path)
    return control.Control(Settings.load(tmp_path), [], lifecycle=FakeLifecycle())


@asynccontextmanager
async def login(instance: control.Control):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=instance), base_url=instance.origin)
    try:
        fragment = urlsplit(instance.issue_bootstrap()["url"]).fragment
        response = await http.post(
            "/control/session", json={"bootstrap": fragment}, headers={"Origin": instance.origin}
        )
        assert response.status_code == 204
        yield http
    finally:
        await http.aclose()


async def review(http, params: list[tuple[str, str]]) -> httpx.Response:
    return await http.get("/control/proxies", params=params)


async def apply(http, instance, revision: str, **fields: str) -> httpx.Response:
    return await http.post(
        "/control/proxies/apply",
        data={"csrf": session_csrf(instance), "revision": revision, **fields},
        headers={"Origin": instance.origin},
    )


ENDPOINT = {
    "action": "endpoint",
    "name": "corp",
    "protocol": "http",
    "host": "127.0.0.1",
    "port": "8080",
}


@pytest.mark.asyncio
async def test_create_assign_two_profiles_apply_and_no_plaintext(tmp_path, monkeypatch):
    stored = fake_keyring(monkeypatch)
    stored["teleloom:work:proxy"] = LEGACY
    instance = make_control_proxy(tmp_path)
    async with login(instance) as http:
        created = await review(http, [*ENDPOINT.items(), ("review", "1")])
        assert created.status_code == 200, created.text
        assert "proxies.corp: null -&gt; " in created.text
        assert 'name="username"' not in created.text.split("</form>", 1)[0]
        assert 'name="password"' not in created.text.split("</form>", 1)[0]
        applied = await apply(
            http, instance, digest(tmp_path), **ENDPOINT, username="corp-user", password=PASSWORD
        )
        assert applied.status_code == 200, applied.text
        assert PASSWORD not in applied.text and "corp-user" not in applied.text
        assert "corp-user" not in created.text and PASSWORD not in created.text
        settings = Settings.load(tmp_path)
        assert adapters.proxy_url("work", settings.profiles["work"], Secrets(), settings) == LEGACY

        for profile in ("personal", "work"):
            assigned = await review(
                http,
                [("action", "assign"), ("profile", profile), ("choice", "corp"), ("review", "1")],
            )
            assert assigned.status_code == 200, assigned.text
            assert f"profiles.{profile}.proxy: profile default -&gt; corp" in assigned.text
            applied = await apply(
                http,
                instance,
                digest(tmp_path),
                action="assign",
                profile=profile,
                choice="corp",
            )
            assert applied.status_code == 200, applied.text
            assert "Applied and verified" in applied.text

        async def failed_connect(host, port):
            assert (host, port) == ("127.0.0.1", 8080)
            raise OSError

        monkeypatch.setattr(control_proxies.asyncio, "open_connection", failed_connect)
        fields = {"name": "corp", "csrf": session_csrf(instance)}
        refused = await http.post(
            "/control/proxies/check", data=fields, headers={"Origin": "https://outside.invalid"}
        )
        assert refused.status_code == 403
        checked = await http.post(
            "/control/proxies/check", data=fields, headers={"Origin": instance.origin}
        )
        assert checked.status_code == 200 and "Route check: unavailable" in checked.text

    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["proxies"]["corp"] == {"protocol": "http", "host": "127.0.0.1", "port": 8080}
    assert data["profiles"]["personal"]["proxy"] == "corp"
    assert data["profiles"]["work"]["proxy"] == "corp"
    assert PASSWORD not in raw and "corp-user" not in raw
    assert json.loads(stored["teleloom:proxy:corp"]) == {
        "username": "corp-user",
        "password": PASSWORD,
    }
    assert stored["teleloom:work:proxy"] == LEGACY  # legacy secret is untouched

    record = control.record(instance.settings)
    assert record is not None and PASSWORD not in json.dumps(record)
    assert instance.lifecycle.stopped == 3

    # Two profiles resolve the stored assignment after reload; one shared endpoint.
    settings = Settings.load(tmp_path)
    for name in ("personal", "work"):
        route = adapters.proxy_url(name, settings.profiles[name], Secrets(), settings)
        assert route == f"http://corp-user:{PASSWORD}@127.0.0.1:8080"
        assert LEGACY not in route


@pytest.mark.asyncio
async def test_migration_preserves_legacy_route_and_secret(tmp_path, monkeypatch):
    stored = fake_keyring(monkeypatch)
    stored["teleloom:personal:proxy"] = LEGACY
    instance = make_control_proxy(tmp_path)
    async with login(instance) as http:
        migrated = await review(http, [("action", "migrate"), ("review", "1")])
        assert migrated.status_code == 200, migrated.text
        assert "profiles.personal.proxy: profile default -&gt; legacy_personal" in migrated.text
        applied = await apply(http, instance, digest(tmp_path), action="migrate")
        assert applied.status_code == 200, applied.text
        assert "Applied and verified" in applied.text

    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["proxies"]["legacy_personal"] == {
        "protocol": "socks5",
        "host": "legacy.local",
        "port": 1080,
    }
    assert data["profiles"]["personal"]["proxy"] == "legacy_personal"
    assert "proxy" not in data["profiles"]["work"]
    assert stored["teleloom:personal:proxy"] == LEGACY
    assert json.loads(stored["teleloom:proxy:legacy_personal"]) == {
        "username": "legacy-user",
        "password": "legacy-pass",
    }
    settings = Settings.load(tmp_path)
    assert (
        adapters.proxy_url("personal", settings.profiles["personal"], Secrets(), settings) == LEGACY
    )
    # The unassigned profile keeps its own legacy route; no assignment is inferred.
    assert adapters.proxy_url("work", settings.profiles["work"], Secrets(), settings) is None


@pytest.mark.asyncio
async def test_migration_never_replaces_an_existing_named_endpoint(tmp_path, monkeypatch):
    stored = fake_keyring(monkeypatch)
    stored["teleloom:personal:proxy"] = LEGACY
    instance = make_control_proxy(tmp_path)
    path = tmp_path / "config.json"
    data = json.loads(path.read_bytes())
    data["proxies"] = {"legacy_personal": {"protocol": "http", "host": "other.local", "port": 8080}}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    async with login(instance) as http:
        response = await apply(http, instance, digest(tmp_path), action="migrate")
        assert response.status_code == 422 and "proxy_exists" in response.text
    assert path.read_bytes() == before
    assert control.record(instance.settings) is None
    assert stored == {"teleloom:personal:proxy": LEGACY}


@pytest.mark.asyncio
async def test_invalid_endpoint_and_unknown_assignment_are_refused_before_write(
    tmp_path, monkeypatch
):
    fake_keyring(monkeypatch)
    instance = make_control_proxy(tmp_path)
    before = (tmp_path / "config.json").read_bytes()
    async with login(instance) as http:
        for fields in (
            {"protocol": "socks6", "host": "127.0.0.1", "port": "1080"},
            {"protocol": "http", "host": "http://proxy.local", "port": "8080"},
            {"protocol": "http", "host": "proxy.local", "port": "0"},
            {"protocol": "http", "host": "proxy.local/path", "port": "8080"},
        ):
            refused = await review(
                http, [("action", "endpoint"), ("name", "corp"), ("review", "1"), *fields.items()]
            )
            assert refused.status_code == 422, refused.text
            assert "invalid_proxy" in refused.text
        unknown = await apply(
            http, instance, digest(tmp_path), action="assign", profile="personal", choice="missing"
        )
        assert unknown.status_code == 422 and "proxy_unavailable" in unknown.text
        stale = await apply(
            http, instance, "0" * 64, action="assign", profile="personal", choice="direct"
        )
        assert stale.status_code == 409 and "stale" in stale.text
    assert (tmp_path / "config.json").read_bytes() == before
    assert instance.lifecycle.stopped == 0
    assert control.record(instance.settings) is None


@pytest.mark.asyncio
async def test_missing_endpoint_never_falls_back_to_direct(tmp_path, monkeypatch):
    fake_keyring(monkeypatch)
    instance = make_control_proxy(tmp_path)
    async with login(instance) as http:
        assert (
            await apply(http, instance, digest(tmp_path), **ENDPOINT, username="u", password="p")
        ).status_code == 200
        assert (
            await apply(
                http, instance, digest(tmp_path), action="assign", profile="personal", choice="corp"
            )
        ).status_code == 200

    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    data["proxies"] = {}  # the endpoint disappears behind the assignment
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    settings = Settings.load(tmp_path)
    with pytest.raises(TeleloomError) as failure:
        adapters.proxy_url("personal", settings.profiles["personal"], Secrets(), settings)
    assert failure.value.code == "proxy_unavailable"
    with pytest.raises(TeleloomError) as failure:
        adapters.user_client(
            "personal",
            Profile(kind="user", api_id=1, proxy="corp"),
            Secrets(),
            new=True,
            settings=settings,
        )
    assert failure.value.code == "proxy_unavailable"

    # The explicit route check is bounded, explicit, and reports no fallback.
    async def refused(host: str, port: int):
        raise OSError("refused")

    async def hung(host: str, port: int):
        raise TimeoutError

    monkeypatch.setattr(control_proxies.asyncio, "open_connection", refused)
    data["proxies"] = {"corp": {"protocol": "http", "host": "127.0.0.1", "port": 8080}}
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    route_settings = Settings.load(tmp_path)
    unavailable = await control_proxies.probe(route_settings, "corp", 0.1)
    assert unavailable == {"proxy": "corp", "status": "unavailable", "direct_fallback": False}
    monkeypatch.setattr(control_proxies.asyncio, "open_connection", hung)
    assert (await control_proxies.probe(route_settings, "corp", 0.1))["status"] == "timeout"
    assert (await control_proxies.probe(route_settings, "absent", 0.1))["status"] == "unavailable"


def make_control_proxy(tmp_path) -> control.Control:
    write_config(tmp_path)
    return control.Control(Settings.load(tmp_path), [], lifecycle=FakeLifecycle())
