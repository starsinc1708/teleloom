"""Public administration management contract; mutable fixtures stay per case."""

import json
from datetime import UTC, datetime

import pytest
from telethon import errors, functions, types

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from tests.administration_fakes import GROUP
from tests.administration_fakes import sdk as sdk
from tests.fakes import data
from tests.test_jobs import complete
from tests.test_transport import client, running


async def test_group_title_has_confirmed_exact_before_state_and_durable_receipt(tmp_path, sdk):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user",
                identity={"id": "7"},
                manage_scopes=["groups"],
                read_mode="selected",
                read_chats=[GROUP],
            )
        },
    )
    async with running(settings, adapter_factory=make_adapter) as app:
        async with client(app, settings) as session:
            plan = data(
                await session.call_tool(
                    "administration_preview",
                    {
                        "profile_id": "work",
                        "operation": {
                            "kind": "group_title",
                            "chat_id": GROUP,
                            "title": "Reviewed title",
                        },
                    },
                )
            )
            assert plan["ok"], plan
            arguments = {
                "profile_id": "work",
                "plan_id": plan["data"]["plan_id"],
                "plan_hash": plan["data"]["plan_hash"],
            }
            assert (
                data(await session.call_tool("delivery_execute", arguments))["error"]["code"]
                == "confirmation_required"
            )
            assert (
                data(
                    await session.call_tool(
                        "delivery_execute", {**arguments, "confirmed": True, "plan_hash": "wrong"}
                    )
                )["error"]["code"]
                == "plan_changed"
            )
            started = data(
                await session.call_tool("delivery_execute", {**arguments, "confirmed": True})
            )["data"]
            duplicate = data(
                await session.call_tool("delivery_execute", {**arguments, "confirmed": True})
            )["data"]
            assert started["job_id"] == duplicate["job_id"]
            finished = await complete(session, "work", started["job_id"])
            assert finished["status"] == "completed", finished
            assert finished["deliveries"][0]["receipt"]["accepted"] is True
            assert sdk.group.title == "Reviewed title"
            assert sum(isinstance(x, functions.channels.EditTitleRequest) for x in sdk.calls) == 1


@pytest.mark.parametrize("kind", ["group_join", "group_import_invite"])
@pytest.mark.parametrize(
    "error,join_state,joined",
    [
        (errors.InviteRequestSentError, "approval_pending", False),
        (errors.UserAlreadyParticipantError, "already_joined", True),
    ],
)
async def test_join_receipts_keep_approval_or_existing_membership_without_replay(
    tmp_path, sdk, kind, error, join_state, joined
):
    sdk.join_error = error(None)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"work": Profile(kind="user", identity={"id": "7"}, manage_scopes=["groups"])},
    )
    operation = {"kind": kind}
    if kind == "group_join":
        operation["chat_id"] = GROUP
    else:
        operation["invite_hash"] = "reviewed_hash"
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        preview = data(
            await session.call_tool(
                "administration_preview", {"profile_id": "work", "operation": operation}
            )
        )
        assert preview["ok"], preview
        plan = preview["data"]
        args = {
            "profile_id": "work",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        started = data(await session.call_tool("delivery_execute", args))["data"]
        result = await complete(session, "work", started["job_id"])
        assert result["status"] == "completed", result
        assert result["deliveries"][0]["receipt"] == {
            "accepted": True,
            "join_state": join_state,
            "joined": joined,
        }
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        duplicate = data(await session.call_tool("delivery_execute", args))
        assert duplicate["data"]["job_id"] == started["job_id"]
        assert (
            sum(
                isinstance(
                    request,
                    (
                        functions.channels.JoinChannelRequest,
                        functions.messages.ImportChatInviteRequest,
                    ),
                )
                for request in sdk.calls
            )
            == 1
        )


async def test_basic_group_invites_keep_successful_and_rejected_member_receipts(tmp_path, sdk):
    sdk.group = types.Chat(
        id=456,
        title="Basic group",
        photo=types.ChatPhotoEmpty(),
        participants_count=1,
        date=datetime.now(UTC),
        version=1,
    )
    profile = Profile(kind="user", identity={"id": "7"}, manage_scopes=["groups"])
    settings = Settings(data_dir=tmp_path, profiles={"work": profile})
    settings.limits.interval_seconds = 1
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        preview = data(
            await session.call_tool(
                "administration_preview",
                {
                    "profile_id": "work",
                    "operation": {
                        "kind": "group_invite",
                        "chat_id": "-456",
                        "user_ids": ["8", "9"],
                    },
                },
            )
        )
        assert preview["ok"], preview
        plan = preview["data"]
        started = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]
        finished = await complete(session, "work", started["job_id"])
        assert finished["status"] == "failed", finished
        delivery = finished["deliveries"][0]
        assert delivery["status"] == "partial"
        assert [(x["user_id"], x["accepted"]) for x in delivery["receipt"]["member_results"]] == [
            ("8", True),
            ("9", False),
        ]
        assert sum(isinstance(x, functions.messages.AddChatUserRequest) for x in sdk.calls) == 2


SDK_OPERATIONS = [
    (
        {"kind": "group_create", "title": "New", "user_ids": ["8"]},
        functions.messages.CreateChatRequest,
    ),
    (
        {"kind": "group_create_channel", "title": "New", "megagroup": True},
        functions.channels.CreateChannelRequest,
    ),
    (
        {"kind": "group_invite", "chat_id": GROUP, "user_ids": ["8"]},
        functions.channels.InviteToChannelRequest,
    ),
    ({"kind": "group_join", "chat_id": GROUP}, functions.channels.JoinChannelRequest),
    (
        {"kind": "group_import_invite", "invite_hash": "exact-invite"},
        functions.messages.ImportChatInviteRequest,
    ),
    ({"kind": "group_leave", "chat_id": GROUP}, functions.channels.LeaveChannelRequest),
    (
        {"kind": "group_about", "chat_id": GROUP, "about": "Reviewed about"},
        functions.messages.EditChatAboutRequest,
    ),
    (
        {"kind": "group_photo_set", "chat_id": GROUP, "source_path": "FILE"},
        functions.channels.EditPhotoRequest,
    ),
    ({"kind": "group_photo_delete", "chat_id": GROUP}, functions.channels.EditPhotoRequest),
    (
        {
            "kind": "group_admin",
            "chat_id": GROUP,
            "user_id": "8",
            "rights": {"delete_messages": True},
            "rank": "Editor",
        },
        functions.channels.EditAdminRequest,
    ),
    ({"kind": "group_ban", "chat_id": GROUP, "user_id": "8"}, functions.channels.EditBannedRequest),
    (
        {"kind": "group_unban", "chat_id": GROUP, "user_id": "8"},
        functions.channels.EditBannedRequest,
    ),
    (
        {"kind": "group_remove", "chat_id": GROUP, "user_id": "8"},
        functions.channels.EditBannedRequest,
    ),
    (
        {"kind": "group_permissions", "chat_id": GROUP, "permissions": {"send_media": False}},
        functions.messages.EditChatDefaultBannedRightsRequest,
    ),
    (
        {"kind": "group_slow_mode", "chat_id": GROUP, "seconds": 30},
        functions.channels.ToggleSlowModeRequest,
    ),
    ({"kind": "group_forum", "chat_id": GROUP}, functions.channels.ToggleForumRequest),
    ({"kind": "group_export_invite", "chat_id": GROUP}, functions.messages.ExportChatInviteRequest),
    (
        {"kind": "group_topic_create", "chat_id": GROUP, "title": "Reviewed topic"},
        functions.messages.CreateForumTopicRequest,
    ),
    (
        {
            "kind": "group_topic_edit",
            "chat_id": GROUP,
            "topic_id": "11",
            "icon_emoji_id": "0",
            "closed": True,
        },
        functions.messages.EditForumTopicRequest,
    ),
    (
        {"kind": "group_topic_delete", "chat_id": GROUP, "topic_id": "11"},
        functions.messages.DeleteTopicHistoryRequest,
    ),
    (
        {"kind": "account_profile", "first_name": "Reviewed owner"},
        functions.account.UpdateProfileRequest,
    ),
    (
        {"kind": "account_photo_set", "source_path": "FILE"},
        functions.photos.UploadProfilePhotoRequest,
    ),
    ({"kind": "account_photo_delete"}, functions.photos.DeletePhotosRequest),
    (
        {
            "kind": "account_privacy",
            "key": "phone",
            "base": "contacts",
            "allow_users": ["8"],
            "disallow_users": ["9"],
        },
        functions.account.SetPrivacyRequest,
    ),
    (
        {
            "kind": "account_bot_commands",
            "bot_id": "7",
            "commands": [{"command": "help", "description": "Reviewed help"}],
            "language_code": "ru",
        },
        functions.bots.SetBotCommandsRequest,
    ),
]


@pytest.mark.parametrize(
    "operation,method_type", SDK_OPERATIONS, ids=[x[0]["kind"] for x in SDK_OPERATIONS]
)
async def test_confirmed_management_operations_encode_real_telethon_requests(
    tmp_path, sdk, operation, method_type
):
    photo = tmp_path / "reviewed-photo.jpg"
    photo.write_bytes(b"\xff\xd8\xffreviewed bytes")
    operation = {
        **operation,
        **({"source_path": str(photo)} if operation.get("source_path") else {}),
    }
    bot = operation["kind"] == "account_bot_commands"
    sdk.me.bot = bot
    profile = Profile(
        kind="bot" if bot else "user",
        bot_backend="mtproto",
        identity={"id": "7"},
        manage_scopes=["groups", "account"],
        file_roots=[str(tmp_path)],
    )
    settings = Settings(data_dir=tmp_path / "state", profiles={"work": profile})
    settings.limits.interval_seconds = 1
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        tool = (
            "administration_preview"
            if operation["kind"].startswith("group_")
            else "account_preview"
        )
        preview = data(
            await session.call_tool(tool, {"profile_id": "work", "operation": operation})
        )
        assert preview["ok"], preview
        assert not any(isinstance(x, method_type) for x in sdk.calls)
        plan = preview["data"]
        assert "access_hash" not in json.dumps(plan)
        assert "SECRET_PHONE" not in json.dumps(plan)
        started = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert started["ok"], started
        finished = await complete(session, "work", started["data"]["job_id"])
        assert finished["status"] == "completed", finished
        writes = [x for x in sdk.calls if isinstance(x, method_type)]
        assert len(writes) == (2 if operation["kind"] == "group_remove" else 1)
        if operation["kind"] in {
            "group_create",
            "group_create_channel",
            "group_import_invite",
            "group_join",
        }:
            assert finished["deliveries"][0]["receipt"]["chat_ids"] == [GROUP]
        delivery = finished["deliveries"][0]
        if not operation.get("chat_id"):
            assert "chat_id" not in delivery
            assert delivery["target"] == {
                "kind": "account",
                "profile_id": "work",
                "scope": "groups" if tool == "administration_preview" else "account",
            }
        if operation.get("source_path"):
            assert sdk.uploaded == (photo.name, photo.read_bytes())


@pytest.mark.parametrize(
    "change,code",
    [
        ("scope", "management_not_allowed"),
        ("read", "read_not_allowed"),
        ("state", "source_changed"),
        ("generation", "account_changed"),
    ],
)
async def test_management_confirmation_rejects_revoked_or_changed_plan(tmp_path, sdk, change, code):
    profile = Profile(
        kind="user",
        identity={"id": "7"},
        manage_scopes=["groups"],
        read_mode="selected",
        read_chats=[GROUP],
    )
    settings = Settings(data_dir=tmp_path, profiles={"work": profile})
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        plan = data(
            await session.call_tool(
                "administration_preview",
                {
                    "profile_id": "work",
                    "operation": {"kind": "group_title", "chat_id": GROUP, "title": "Reviewed"},
                },
            )
        )["data"]
        if change == "scope":
            profile.manage_scopes = []
        elif change == "read":
            profile.read_chats = []
        elif change == "generation":
            profile.generation = "replaced"
        else:
            sdk.group.title = "Changed externally"
        rejected = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert rejected["error"]["code"] == code, rejected
        assert not any(isinstance(x, functions.channels.EditTitleRequest) for x in sdk.calls)


async def test_unknown_management_write_is_never_replayed_after_restart(tmp_path, sdk):
    sdk.title_error = ConnectionError("uncertain after external write")
    settings = Settings(
        data_dir=tmp_path,
        profiles={"work": Profile(kind="user", identity={"id": "7"}, manage_scopes=["groups"])},
    )
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        plan = data(
            await session.call_tool(
                "administration_preview",
                {
                    "profile_id": "work",
                    "operation": {"kind": "group_title", "chat_id": GROUP, "title": "Reviewed"},
                },
            )
        )["data"]
        args = {
            "profile_id": "work",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        job = data(await session.call_tool("delivery_execute", args))["data"]["job_id"]
        result = await complete(session, "work", job)
        assert result["status"] == "needs_review"
        assert result["deliveries"][0]["status"] == "unknown"
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        duplicate = data(await session.call_tool("delivery_execute", args))
        assert duplicate["data"]["job_id"] == job
        denied = data(
            await session.call_tool(
                "jobs_control", {"profile_id": "work", "job_id": job, "action": "resume"}
            )
        )
        assert denied["error"]["code"] == "delivery_unknown"
        assert sum(isinstance(x, functions.channels.EditTitleRequest) for x in sdk.calls) == 1


async def test_remove_reports_ejected_with_ban_when_unban_is_rejected(tmp_path, sdk):
    sdk.unban_error = errors.ChatAdminRequiredError(None)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"work": Profile(kind="user", identity={"id": "7"}, manage_scopes=["groups"])},
    )
    settings.limits.interval_seconds = 1
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        plan = data(
            await session.call_tool(
                "administration_preview",
                {
                    "profile_id": "work",
                    "operation": {"kind": "group_remove", "chat_id": GROUP, "user_id": "8"},
                },
            )
        )["data"]
        job = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        result = await complete(session, "work", job)
        assert result["status"] == "failed"
        delivery = result["deliveries"][0]
        assert delivery["status"] == "partial"
        assert delivery["receipts"][0]["phase"] == "ejected_with_ban"
        assert delivery["error"]["code"] == "telegram_rejected"


async def test_photo_confirmation_rejects_changed_source_bytes(tmp_path, sdk):
    photo = tmp_path / "reviewed.jpg"
    photo.write_bytes(b"\xff\xd8\xfforiginal")
    settings = Settings(
        data_dir=tmp_path / "state",
        profiles={
            "work": Profile(
                kind="user",
                identity={"id": "7"},
                manage_scopes=["account"],
                file_roots=[str(tmp_path)],
            )
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        plan = data(
            await session.call_tool(
                "account_preview",
                {
                    "profile_id": "work",
                    "operation": {"kind": "account_photo_set", "source_path": str(photo)},
                },
            )
        )["data"]
        photo.write_bytes(b"\xff\xd8\xffchanged")
        result = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert result["error"]["code"] == "source_changed"
        assert not any(isinstance(x, functions.photos.UploadProfilePhotoRequest) for x in sdk.calls)


async def test_demote_uses_explicit_empty_admin_rights(tmp_path, sdk):
    await test_confirmed_management_operations_encode_real_telethon_requests(
        tmp_path,
        sdk,
        {"kind": "group_admin", "chat_id": GROUP, "user_id": "8", "rights": {}},
        functions.channels.EditAdminRequest,
    )
    request = next(x for x in sdk.calls if isinstance(x, functions.channels.EditAdminRequest))
    assert not any(
        value for value in vars(request.admin_rights).values() if isinstance(value, bool)
    )


async def test_privacy_job_rules_become_unreadable_after_account_scope_revoke(tmp_path, sdk):
    profile = Profile(kind="user", identity={"id": "7"}, manage_scopes=["account"])
    settings = Settings(data_dir=tmp_path, profiles={"work": profile})
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        plan = data(
            await session.call_tool(
                "account_preview",
                {"profile_id": "work", "operation": {"kind": "account_privacy", "key": "status"}},
            )
        )["data"]
        job = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "work",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        profile.manage_scopes = []
        denied = data(await session.call_tool("jobs_status", {"profile_id": "work", "job_id": job}))
        assert denied["error"]["code"] == "management_not_allowed"
        assert not denied["data"]
