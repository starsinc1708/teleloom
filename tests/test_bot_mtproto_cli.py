import json
from types import SimpleNamespace

from typer.testing import CliRunner

from teleloom.cli import app
from teleloom.config import Settings


def test_mtproto_bot_provisioning_is_explicit_private_and_identity_checked(tmp_path, monkeypatch):
    values = {"robot:bot_token": "123456:FAKE_TOKEN", "robot:api_hash": "FAKE_API_HASH"}

    class Vault:
        priority = 1

        def get_password(self, service, key):
            return values.get(key)

        def set_password(self, service, key, value):
            values[key] = value

    Vault.__module__ = "keyring.backends.Windows"

    class SDK:
        session = object()

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def sign_in(self, *, bot_token):
            assert bot_token == "123456:FAKE_TOKEN"

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=123456, username="robot", first_name="Robot", bot=True)

    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("keyring.get_keyring", lambda: Vault())
    monkeypatch.setattr("teleloom.cli.user_client", lambda *a, **kw: SDK())
    monkeypatch.setattr("teleloom.cli.StringSession.save", lambda *a: "FAKE_PRIVATE_SESSION")
    result = CliRunner().invoke(
        app, ["auth", "bot", "--profile", "robot", "--backend", "mtproto", "--api-id", "100"]
    )
    assert result.exit_code == 0, result.output
    configured = Settings.load(tmp_path).profiles["robot"]
    assert configured.bot_backend == "mtproto"
    assert configured.kind == "bot"
    assert values["robot:session"] == "FAKE_PRIVATE_SESSION"
    assert "FAKE_PRIVATE_SESSION" not in json.dumps(configured.model_dump())
    assert "FAKE_TOKEN" not in result.output
    assert json.loads(result.output)["backend"] == "mtproto"
