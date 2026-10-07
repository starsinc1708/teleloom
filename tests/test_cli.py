import json

from typer.testing import CliRunner

from teleloom.cli import app


def test_init_and_client_fragment_do_not_expose_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELELOOM_MCP_TOKEN", "local-secret-token")
    runner = CliRunner()
    initialized = runner.invoke(app, ["init"])
    assert initialized.exit_code == 0, initialized.output
    assert "local-secret-token" not in initialized.output
    fragment = runner.invoke(app, ["config", "client", "--client", "opencode"])
    assert fragment.exit_code == 0
    config = json.loads(fragment.output)
    entry = config["mcp"]["servers"]["teleloom"]
    assert entry["type"] == "local"
    assert "enabled" not in entry
    assert "disabled" not in entry  # V2 connects by default.
    assert "local-secret-token" not in fragment.output


def test_skill_install_is_complete_and_does_not_overwrite(tmp_path):
    runner = CliRunner()
    first = runner.invoke(app, ["skills", "install", "--target", str(tmp_path)])
    assert first.exit_code == 0, first.output
    assert len(list(tmp_path.glob("*/SKILL.md"))) == 6
    file = tmp_path / "teleloom-connect" / "SKILL.md"
    file.write_text("my existing skill", encoding="utf-8")
    second = runner.invoke(app, ["skills", "install", "--target", str(tmp_path)])
    assert second.exit_code != 0
    assert file.read_text(encoding="utf-8") == "my existing skill"


def test_init_refuses_plaintext_keyring_even_if_chained(tmp_path, monkeypatch):
    class Plaintext:
        priority = 10

        def get_password(self, *args):
            raise AssertionError("Insecure backend must not be queried")

    class Chainer:
        backends = [Plaintext()]

    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELELOOM_MCP_TOKEN", raising=False)
    monkeypatch.setattr("keyring.get_keyring", lambda: Chainer())
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 1
    assert "credentials_unavailable" in result.output


def test_all_client_fragments_parse_and_use_installed_stdio_command(tmp_path, monkeypatch):
    import sys
    import tomllib
    from pathlib import Path

    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    for client, section in [
        ("codex", "mcp_servers"),
        ("claude", "mcpServers"),
        ("opencode", "mcp"),
        ("opencode-v1", "mcp"),
        ("hermes", "mcp_servers"),
        ("pi", "mcpServers"),
    ]:
        result = runner.invoke(app, ["config", "client", "--client", client])
        assert result.exit_code == 0
        parsed = tomllib.loads(result.output) if client == "codex" else json.loads(result.output)
        servers = parsed[section]["servers"] if client == "opencode" else parsed[section]
        entry = servers["teleloom"]
        command = (
            entry["command"]
            if client.startswith("opencode")
            else [entry["command"], *entry["args"]]
        )
        expected = (
            str(Path(sys.executable).with_name("pythonw.exe"))
            if sys.platform == "win32"
            else sys.executable
        )
        assert command == [expected, "-m", "teleloom", "mcp"]
        environment = entry["environment"] if client.startswith("opencode") else entry["env"]
        assert environment == {"TELELOOM_DATA_DIR": str(tmp_path)}
        if client == "opencode-v1":
            assert entry["enabled"] is True


def test_pi_skill_target_preserves_existing_workflows(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = CliRunner()
    first = runner.invoke(app, ["skills", "install", "--client", "pi"])
    assert first.exit_code == 0, first.output
    destination = tmp_path / ".pi" / "agent" / "skills"
    assert json.loads(first.output)["destination"] == str(destination.resolve())
    assert len(list(destination.glob("*/SKILL.md"))) == 6
    existing = destination / "teleloom-read" / "SKILL.md"
    existing.write_text("Owner's existing workflow", encoding="utf-8")
    second = runner.invoke(app, ["skills", "install", "--client", "pi"])
    assert second.exit_code != 0
    assert existing.read_text(encoding="utf-8") == "Owner's existing workflow"


def test_user_session_is_saved_only_after_confirmed_authorization(tmp_path, monkeypatch):
    from tests.test_auth import TelegramLoginAPI

    values = {"personal:api_hash": "synthetic-api-hash"}

    class VaultAPI:
        priority = 5

        def get_password(self, service, username):
            return values.get(username)

        def set_password(self, service, username, password):
            values[username] = password

    VaultAPI.__module__ = "keyring.backends.Windows"

    class LoginAPI(TelegramLoginAPI):
        confirmed = False

        async def connect(self):
            self.scanned.set()

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return self.confirmed

    telegram = LoginAPI()
    telegram.session = object()
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("keyring.get_keyring", lambda: VaultAPI())
    monkeypatch.setattr("teleloom.adapters.TelegramClient", lambda *args, **kwargs: telegram)
    monkeypatch.setattr("teleloom.cli.StringSession.save", lambda session: "synthetic-session")
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    runner = CliRunner()
    args = ["auth", "user", "--profile", "personal", "--api-id", "123"]
    failed = runner.invoke(app, args)
    assert failed.exit_code == 1
    assert "personal:session" not in values
    assert json.loads(runner.invoke(app, ["profile", "list"]).output) == {}
    telegram.confirmed = True
    connected = runner.invoke(app, args)
    assert connected.exit_code == 0, connected.output
    assert values["personal:session"] == "synthetic-session"
    assert "synthetic-session" not in connected.output
    assert (
        json.loads(runner.invoke(app, ["profile", "list"]).output)["personal"]["identity"]["id"]
        == "42"
    )
