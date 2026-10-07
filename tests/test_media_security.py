"""Public media security contract, with real owner/state/process seams."""

import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.tl import types
from typer.testing import CliRunner

from teleloom.cli import app
from teleloom.config import Profile, Settings
from teleloom.daemon import ownership
from tests.fakes import data
from tests.media_fakes import MediaSDK, encoded_image
from tests.telegram_fakes import CHAT, SDK, factory
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.mark.asyncio
async def test_expired_private_media_copies_are_cleaned_through_profile_owned_public_tool(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    root = tmp_path / "selected"
    root.mkdir()
    source = root / "original.txt"
    source.write_bytes(b"Exact immutable original")
    sdk = MediaSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={"personal": Profile(kind="user", file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "upload_file", "source_path": str(source)},
                },
            )
        )["data"]
        snapshot = (
            settings.data_dir
            / "file-snapshots"
            / (plan["preview"]["operation"]["files"][0]["snapshot_id"] + ".bin")
        )
        assert snapshot.exists()
        future = datetime.now(UTC) + timedelta(hours=25)
        monkeypatch.setattr("teleloom.media.utcnow", lambda: future)
        monkeypatch.setattr("teleloom.file_snapshots.utcnow", lambda: future)
        cleanup = data(await mcp.call_tool("media_cleanup", {"profile_id": "personal"}))
        assert cleanup["ok"], cleanup
        assert not snapshot.exists() and source.read_bytes() == b"Exact immutable original"
        missing = data(await mcp.call_tool("media_cleanup", {"profile_id": "other"}))
        assert missing["error"]["code"] == "profile_not_found"


@pytest.mark.asyncio
async def test_file_paths_sizes_and_media_formats_fail_before_any_upload(tmp_path):
    import subprocess

    from teleloom.file_snapshots import FILE_LIMIT

    root = tmp_path / "selected"
    root.mkdir()
    outside = tmp_path / "unselected"
    outside.mkdir()
    forbidden = outside / "secret.txt"
    forbidden.write_bytes(b"Excluded bytes")
    plain = root / "original.txt"
    plain.write_bytes(b"Text is not Ogg Opus")
    sticker = root / "wrong.png"
    sticker.write_bytes(encoded_image())
    corrupt = root / "corrupt.gif"
    corrupt.write_bytes(b"GIF89a corrupted container")
    oversized = root / "oversized.bin"
    with oversized.open("wb") as stream:
        stream.seek(FILE_LIMIT)
        stream.write(b"x")
    paths = [
        ("relative.txt", "unsafe_file_path"),
        (str(root / ".." / "unselected" / "secret.txt"), "unsafe_file_path"),
        (str(forbidden), "file_not_allowed"),
        (str(oversized), "file_too_large"),
    ]
    if sys.platform == "win32":
        junction = root / "junction"
        linked = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True
        )
        assert linked.returncode == 0, linked.stderr
        paths.append((str(junction / "secret.txt"), "unsafe_file_path"))
    sdk = MediaSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={"personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        operations = [
            ({"kind": "send_file", "chat_id": CHAT, "source_path": path}, code)
            for path, code in paths
        ]
        operations += [
            ({"kind": "send_voice", "chat_id": CHAT, "source_path": str(plain)}, "invalid_voice"),
            (
                {"kind": "send_sticker", "chat_id": CHAT, "source_path": str(sticker)},
                "invalid_sticker",
            ),
            ({"kind": "send_gif", "chat_id": CHAT, "source_path": str(corrupt)}, "invalid_image"),
        ]
        for operation, code in operations:
            result = data(
                await mcp.call_tool(
                    "media_operation_preview", {"profile_id": "personal", "operation": operation}
                )
            )
            assert result["error"]["code"] == code, result
        assert not sdk.uploaded and not sdk.calls
        assert not list((settings.data_dir / "file-snapshots").glob("*.bin"))


def test_file_roots_are_absolute_owner_cli_permissions_behind_stopped_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(tmp_path / "owner"))
    settings = Settings(profiles={"personal": Profile(kind="user")})
    settings.save()
    root = tmp_path / "selected"
    root.mkdir()
    runner = CliRunner()
    invalid = runner.invoke(app, ["profile", "file-root", "personal", "relative", "--enable"])
    assert invalid.exit_code == 1
    assert json.loads(invalid.output)["error"]["code"] == "unsafe_file_path"
    added = runner.invoke(app, ["profile", "file-root", "personal", str(root), "--enable"])
    assert added.exit_code == 0, added.output
    assert Settings.load().profiles["personal"].file_roots == [str(root.resolve())]
    with ownership(settings):
        blocked = runner.invoke(app, ["profile", "file-root", "personal", str(root), "--disable"])
        assert blocked.exit_code == 1
        assert json.loads(blocked.output)["error"]["code"] == "owner_busy"
    root.rmdir()
    removed = runner.invoke(app, ["profile", "file-root", "personal", str(root), "--disable"])
    assert removed.exit_code == 0 and Settings.load().profiles["personal"].file_roots == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["root", "snapshot", "source", "recipient"])
async def test_worker_revalidates_frozen_media_and_permissions_before_marking_sending(
    tmp_path, change
):
    from teleloom.store import Store

    root = tmp_path / "selected"
    root.mkdir()
    source = root / "report.txt"
    source.write_bytes(b"Exact immutable original")
    sdk = MediaSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={"personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        store = Store(settings.data_dir)
        with store.db:
            store.set_state("next_send:personal", datetime.now(UTC).timestamp() + 3600)
        plan = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send_file",
                        "chat_id": CHAT,
                        "source_path": str(source),
                        "caption": "Exact caption",
                    },
                },
            )
        )["data"]
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        if change == "root":
            settings.profiles["personal"].file_roots = []
        elif change == "recipient":
            settings.profiles["personal"].send_chats = []
        elif change == "source":
            source.write_bytes(b"Changed after confirmation")
        else:
            snapshot = plan["preview"]["operation"]["files"][0]["snapshot_id"]
            (settings.data_dir / "file-snapshots" / (snapshot + ".bin")).write_bytes(
                b"Tampered copy"
            )
        with store.db:
            store.set_state("next_send:personal", 0)
        status = await complete(mcp, "personal", job)
        assert status["status"] == "failed", status
        assert (
            status["error"]["code"]
            == {
                "root": "file_not_allowed",
                "recipient": "recipient_not_allowed",
                "source": "source_changed",
                "snapshot": "snapshot_changed",
            }[change]
        )
        assert status["deliveries"][0]["status"] == "pending" and not sdk.uploaded
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "root", "snapshot", "account"])
async def test_changed_media_sources_are_denied_before_execution_and_worker_upload(
    tmp_path, change
):
    root = tmp_path / "selected"
    root.mkdir()
    source = root / "report.txt"
    source.write_bytes(b"Exact immutable original")
    sdk = MediaSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={"personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        preview = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send_file",
                        "chat_id": CHAT,
                        "source_path": str(source),
                        "caption": "Exact caption",
                    },
                },
            )
        )["data"]
        execute = {
            "profile_id": "personal",
            "plan_id": preview["plan_id"],
            "plan_hash": preview["plan_hash"],
            "confirmed": True,
        }
        if change == "source":
            source.write_bytes(b"A larger changed source file after the preview")
        elif change == "root":
            settings.profiles["personal"].file_roots = []
        elif change == "account":
            settings.profiles["personal"].generation = "replacement"
        else:
            file = preview["preview"]["operation"]["files"][0]
            (settings.data_dir / "file-snapshots" / (file["snapshot_id"] + ".bin")).write_bytes(
                b"snapshot changed"
            )
        rejected = data(await mcp.call_tool("delivery_execute", execute))
        assert (
            rejected["error"]["code"]
            == {
                "source": "source_changed",
                "root": "file_not_allowed",
                "snapshot": "snapshot_changed",
                "account": "account_changed",
            }[change]
        )
        assert not sdk.uploaded


@pytest.mark.asyncio
async def test_cached_chat_refreshes_forum_metadata_without_losing_dialog_state(tmp_path):
    class ForumSDK(SDK):
        entity = types.Channel(
            id=100,
            title="Selected group",
            date=datetime.now(UTC),
            photo=types.ChatPhotoEmpty(),
            megagroup=True,
            forum=False,
        )

        async def get_entity(self, value):
            return self.entity

    sdk = ForumSDK()
    sdk.dialogs = [
        SimpleNamespace(
            id=int(CHAT),
            name=sdk.entity.title,
            is_group=True,
            is_channel=True,
            entity=sdk.entity,
            unread_count=3,
            dialog=SimpleNamespace(
                read_inbox_max_id=4,
                top_message=10,
                unread_mark=True,
                unread_mentions_count=2,
                folder_id=1,
            ),
        )
    ]
    sdk.rows = [
        types.MessageService(
            id=5,
            peer_id=types.PeerChannel(100),
            date=datetime.now(UTC),
            action=types.MessageActionTopicCreate(title="Test topic", icon_color=0),
        )
    ]
    source = tmp_path / "topic.png"
    source.write_bytes(encoded_image())
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={
            "personal": Profile(
                kind="user",
                read_mode="selected",
                read_chats=[CHAT],
                send_chats=[CHAT],
                file_roots=[str(tmp_path)],
            )
        },
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        cached = data(await mcp.call_tool("chats_list", {"profile_id": "personal"}))
        assert cached["data"]["items"][0]["forum"] is False
    sdk.entity.forum = True
    sdk.entity.title = "Selected forum"
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        preview = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send_file",
                        "chat_id": CHAT,
                        "source_path": str(source),
                        "as_document": False,
                        "topic_id": "5",
                    },
                },
            )
        )
        assert preview["ok"], preview
        resolved = data(
            await mcp.call_tool("chat_resolve", {"profile_id": "personal", "target": CHAT})
        )["data"]
        assert resolved["forum"] is True and resolved["title"] == "Selected forum"
        for key in (
            "unread_count",
            "read_inbox_max_id",
            "top_message_id",
            "unread_mark",
            "unread_mentions_count",
            "archived",
            "muted",
        ):
            assert resolved[key] == cached["data"]["items"][0][key]
        assert all(isinstance(call, dict) for call in sdk.calls), "Preview must not upload media"
        sdk.entity.id = 101
        changed = data(
            await mcp.call_tool("chat_resolve", {"profile_id": "personal", "target": CHAT})
        )
        assert changed["error"]["code"] == "peer_identity_changed"
