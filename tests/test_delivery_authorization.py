"""Exact owner instructions through public MCP, without persistent grant expansion."""

from datetime import UTC, datetime, timedelta

import pytest
from telethon import functions, types

from teleloom.config import Limits, Profile, Settings
from teleloom.store import Store
from tests.fakes import TelegramAPI, data
from tests.media_fakes import MediaSDK
from tests.telegram_fakes import CHAT, SDK
from tests.telegram_fakes import factory as sdk_factory
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.mark.asyncio
@pytest.mark.parametrize("broadcast", [False, True])
async def test_owner_instruction_authorizes_only_hash_bound_delivery_plan(tmp_path, broadcast):
    apis = []

    def factory(*args):
        api = TelegramAPI(*args)
        apis.append(api)
        return api

    settings = Settings(
        data_dir=tmp_path,
        limits=Limits(interval_seconds=1),
        profiles={"personal": Profile(kind="user"), "other": Profile(kind="user")},
    )
    settings.save()
    configuration = (tmp_path / "config.json").read_bytes()
    args = {
        "profile_id": "personal",
        "recipients": ["100", "200"] if broadcast else ["100"],
        "text": "Reviewed owner request",
        "broadcast": broadcast,
    }
    async with running(settings, factory) as app, client(app, settings) as session:
        strict = data(await session.call_tool("delivery_preview", args))
        assert strict["error"]["code"] == "recipient_not_allowed"
        result = data(
            await session.call_tool("delivery_preview", {**args, "owner_authorized": True})
        )
        assert result["ok"], result
        preview = result["data"]
        assert preview["preview"]["owner_authorized"] is True
        execute = {
            "profile_id": "personal",
            "plan_id": preview["plan_id"],
            "plan_hash": preview["plan_hash"],
        }
        refused = data(await session.call_tool("delivery_execute", execute))
        assert refused["error"]["code"] == "confirmation_required"
        changed = data(
            await session.call_tool(
                "delivery_execute", {**execute, "confirmed": True, "plan_hash": "wrong"}
            )
        )
        assert changed["error"]["code"] == "plan_changed"
        foreign = data(
            await session.call_tool(
                "delivery_execute", {**execute, "confirmed": True, "profile_id": "other"}
            )
        )
        assert foreign["error"]["code"] == "plan_not_found"
        sent = data(await session.call_tool("delivery_execute", {**execute, "confirmed": True}))
        assert sent["ok"], sent
        receipt = await complete(session, "personal", sent["data"]["job_id"])
        assert receipt["status"] == "completed"
        repeated = data(await session.call_tool("delivery_execute", {**execute, "confirmed": True}))
        assert repeated["data"]["job_id"] == sent["data"]["job_id"]
        assert [item[0] for item in apis[0].sent] == args["recipients"]
        assert not settings.profile("personal").send_chats
        assert not settings.profile("personal").broadcast_chats
        assert (tmp_path / "config.json").read_bytes() == configuration
        strict = data(await session.call_tool("delivery_preview", args))
        assert strict["error"]["code"] == "recipient_not_allowed"


@pytest.mark.asyncio
async def test_owner_authorized_formatted_send_preserves_entities_in_native_request(tmp_path):
    class SendSDK(SDK):
        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            assert isinstance(request, functions.messages.SendMessageRequest)
            assert request.message == "Approved secret"
            assert isinstance(request.entities[0], types.MessageEntityBold)
            assert isinstance(request.entities[1], types.MessageEntitySpoiler)
            return types.UpdateShortSentMessage(id=20, pts=1, pts_count=1, date=datetime.now(UTC))

    sdk = SendSDK()
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, sdk_factory(sdk)) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                "message_operation_preview",
                {
                    "profile_id": "personal",
                    "owner_authorized": True,
                    "operation": {
                        "kind": "send",
                        "chat_id": CHAT,
                        "text": "Approved secret",
                        "entities": [
                            {"type": "bold", "offset": 0, "length": 8},
                            {"type": "spoiler", "offset": 9, "length": 6},
                        ],
                    },
                },
            )
        )
        assert result["ok"], result
        plan = result["data"]
        assert plan["preview"]["operation"]["owner_authorized"] is True
        job = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        assert (await complete(session, "personal", job))["status"] == "completed"
        assert len(sdk.calls) == 1 and not settings.profile("personal").send_chats


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["read", "account", "expiry"])
async def test_owner_authorization_keeps_current_read_account_and_expiry_checks(
    tmp_path, monkeypatch, change
):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings) as app, client(app, settings) as session:
        plan = data(
            await session.call_tool(
                "delivery_preview",
                {
                    "profile_id": "personal",
                    "recipients": ["100"],
                    "text": "Exact request",
                    "owner_authorized": True,
                },
            )
        )["data"]
        if change == "read":
            settings.profile("personal").read_mode = "selected"
        elif change == "account":
            settings.profile("personal").generation = "replacement-account"
        else:
            expired = datetime.now(UTC) + timedelta(minutes=16)
            monkeypatch.setattr("teleloom.jobs.utcnow", lambda: expired)
        rejected = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert (
            rejected["error"]["code"]
            == {"read": "read_not_allowed", "account": "account_changed", "expiry": "plan_expired"}[
                change
            ]
        )
        assert (
            data(await session.call_tool("jobs_status", {"profile_id": "personal"}))["data"]["jobs"]
            == []
        )


@pytest.mark.asyncio
async def test_owner_instruction_never_authorizes_other_mutations_or_bare_uploads(tmp_path):
    source = tmp_path / "selected.txt"
    source.write_bytes(b"Selected bytes")
    settings = Settings(data_dir=tmp_path / "owner", profiles={"personal": Profile(kind="user")})
    async with running(settings) as app, client(app, settings) as session:
        for operation in (
            {"kind": "edit", "chat_id": "100", "message_id": "1", "text": "Change"},
            {"kind": "delete", "chat_id": "100", "message_ids": ["1"]},
            {"kind": "forward", "chat_id": "100", "source_chat_id": "100", "message_ids": ["1"]},
        ):
            result = data(
                await session.call_tool(
                    "message_operation_preview",
                    {"profile_id": "personal", "operation": operation, "owner_authorized": True},
                )
            )
            assert result["error"]["code"] == "unsupported_authorization"
        upload = data(
            await session.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "owner_authorized": True,
                    "operation": {"kind": "upload_file", "source_path": str(source)},
                },
            )
        )
        assert upload["error"]["code"] == "unsupported_authorization"
        assert not list((settings.data_dir / "file-snapshots").glob("*.bin"))


@pytest.mark.asyncio
async def test_owner_authorization_preserves_recipient_limits_and_safe_file_paths(tmp_path):
    source = tmp_path / "selected.txt"
    source.write_bytes(b"Selected bytes")
    settings = Settings(
        data_dir=tmp_path / "owner",
        limits=Limits(recipients=1),
        profiles={"personal": Profile(kind="user")},
    )
    async with running(settings) as app, client(app, settings) as session:
        args = {"profile_id": "personal", "text": "Exact send", "owner_authorized": True}
        limit = data(
            await session.call_tool(
                "delivery_preview", {**args, "recipients": ["100", "200"], "broadcast": True}
            )
        )
        assert limit["error"]["code"] == "recipient_limit"
        duplicate = data(
            await session.call_tool("delivery_preview", {**args, "recipients": ["100", "100"]})
        )
        assert duplicate["error"]["code"] == "invalid_recipients"
        for path in ("relative.txt", str(tmp_path / ".." / tmp_path.name / source.name)):
            unsafe = data(
                await session.call_tool(
                    "media_operation_preview",
                    {
                        "profile_id": "personal",
                        "owner_authorized": True,
                        "operation": {"kind": "send_file", "chat_id": "100", "source_path": path},
                    },
                )
            )
            assert unsafe["error"]["code"] == "unsafe_file_path"
        assert not list((settings.data_dir / "file-snapshots").glob("*.bin"))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["sent", "changed", "unknown"])
async def test_exact_owner_selected_media_needs_no_directory_grant_and_never_replays(
    tmp_path, outcome
):
    source = tmp_path / "selected.txt"
    source.write_bytes(b"Selected immutable bytes")
    sdk = MediaSDK()
    sdk.fail_send = outcome == "unknown"
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={
            "personal": Profile(kind="user"),
            "other": Profile(kind="user", send_chats=[CHAT]),
        },
    )
    settings.save()
    configuration = (settings.data_dir / "config.json").read_bytes()
    args = {
        "profile_id": "personal",
        "operation": {
            "kind": "send_file",
            "chat_id": CHAT,
            "source_path": str(source),
            "caption": "Exact caption",
        },
    }
    async with running(settings, sdk_factory(sdk)) as app, client(app, settings) as session:
        plan_result = data(
            await session.call_tool("media_operation_preview", {**args, "owner_authorized": True})
        )
        assert plan_result["ok"], plan_result
        plan = plan_result["data"]
        assert plan["preview"]["operation"]["owner_authorized"] is True
        execute = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        if outcome == "changed":
            source.write_bytes(b"Changed after owner review")
            result = data(await session.call_tool("delivery_execute", execute))
            assert result["error"]["code"] == "source_changed"
            assert not sdk.uploaded
        else:
            job = data(await session.call_tool("delivery_execute", execute))["data"]["job_id"]
            receipt = await complete(session, "personal", job)
            assert receipt["deliveries"][0]["status"] == outcome
            repeated = data(await session.call_tool("delivery_execute", execute))
            assert repeated["data"]["job_id"] == job
            assert sdk.uploaded == [(source.name, b"Selected immutable bytes")]
            if outcome == "unknown":
                resumed = data(
                    await session.call_tool(
                        "jobs_control",
                        {"profile_id": "personal", "job_id": job, "action": "resume"},
                    )
                )
                assert resumed["error"]["code"] == "delivery_unknown"
        strict = data(
            await session.call_tool("media_operation_preview", {**args, "profile_id": "other"})
        )
        assert strict["error"]["code"] == "file_not_allowed"
        assert (
            not settings.profile("personal").send_chats
            and not settings.profile("personal").file_roots
        )
        assert (settings.data_dir / "config.json").read_bytes() == configuration


@pytest.mark.asyncio
async def test_owner_authorized_delivery_resumes_after_restart_with_no_grant_expansion(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    settings.save()
    store = Store(tmp_path)
    with store.db:
        store.set_state("next_send:personal", datetime.now(UTC).timestamp() + 3600)
    store.close()
    apis = []

    def factory(*args):
        api = TelegramAPI(*args)
        apis.append(api)
        return api

    async with running(settings, factory) as app, client(app, settings) as session:
        plan = data(
            await session.call_tool(
                "delivery_preview",
                {
                    "profile_id": "personal",
                    "recipients": ["100"],
                    "text": "Survives restart",
                    "owner_authorized": True,
                },
            )
        )["data"]
        job = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
    assert not apis[0].sent
    store = Store(tmp_path)
    with store.db:
        store.set_state("next_send:personal", 0)
    store.close()
    fresh = Settings.load(tmp_path)
    async with running(fresh, factory) as app, client(app, fresh) as session:
        assert (await complete(session, "personal", job))["status"] == "completed"
        assert len(apis[1].sent) == 1 and not fresh.profile("personal").send_chats
