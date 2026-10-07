import json

import pytest
from aiogram import Bot, methods, types
from aiogram.client.session.base import BaseSession

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.test_jobs import complete
from tests.test_transport import client, running

GROUP = "-1000000000123"


@pytest.fixture
def bot_sdk(monkeypatch):
    requests = []

    class API(BaseSession):
        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            yield b""

        async def make_request(self, bot, method, timeout=None):
            requests.append(method)
            self.prepare_value(method.model_dump(warnings=False), bot=bot, files={})
            user = types.User(id=7, is_bot=True, first_name="Robot")
            if isinstance(method, methods.GetMe):
                return user
            if isinstance(method, methods.GetChat):
                return types.ChatFullInfo(
                    id=int(GROUP),
                    type="supergroup",
                    title="Reviewed group",
                    description="Before",
                    accent_color_id=0,
                    max_reaction_count=1,
                    accepted_gift_types=types.AcceptedGiftTypes(
                        unlimited_gifts=False,
                        limited_gifts=False,
                        unique_gifts=False,
                        premium_subscription=False,
                        gifts_from_channels=False,
                    ),
                    is_forum=True,
                    permissions=types.ChatPermissions(can_send_messages=True),
                )
            if isinstance(method, methods.GetChatMemberCount):
                return 1
            if isinstance(method, (methods.GetChatMember, methods.GetChatAdministrators)):
                member = types.ChatMemberAdministrator(
                    user=types.User(id=8, is_bot=False, first_name="Member"),
                    is_anonymous=False,
                    can_manage_chat=True,
                    can_delete_messages=True,
                    can_manage_video_chats=True,
                    can_restrict_members=True,
                    can_promote_members=False,
                    can_change_info=True,
                    can_invite_users=True,
                    can_post_stories=False,
                    can_edit_stories=False,
                    can_delete_stories=False,
                    can_be_edited=True,
                    can_send_welcome_messages=False,
                )
                return [member] if isinstance(method, methods.GetChatAdministrators) else member
            if isinstance(method, methods.GetUserProfilePhotos):
                return types.UserProfilePhotos(
                    total_count=1,
                    photos=[
                        [
                            types.PhotoSize(
                                file_id="PRIVATE_FILE_TOKEN",
                                file_unique_id="photo31",
                                width=100,
                                height=100,
                                file_size=10,
                            )
                        ]
                    ],
                )
            if isinstance(method, methods.GetMyCommands):
                return [types.BotCommand(command="old", description="Old menu")]
            if isinstance(method, methods.ExportChatInviteLink):
                return "https://t.me/+fake-exact-invite"
            if isinstance(method, methods.CreateForumTopic):
                return types.ForumTopic(message_thread_id=12, name=method.name, icon_color=0)
            return True

    monkeypatch.setenv("TELELOOM_ROBOT_BOT_TOKEN", "7:" + "A" * 35)
    monkeypatch.setattr("teleloom.adapters.Bot", lambda token, session: Bot(token, session=API()))
    return requests


OPERATIONS = [
    ({"kind": "group_title", "title": "Approved"}, methods.SetChatTitle),
    ({"kind": "group_about", "about": "Approved"}, methods.SetChatDescription),
    ({"kind": "group_leave"}, methods.LeaveChat),
    ({"kind": "group_photo_set", "source_path": "FILE"}, methods.SetChatPhoto),
    ({"kind": "group_photo_delete"}, methods.DeleteChatPhoto),
    (
        {
            "kind": "group_admin",
            "user_id": "8",
            "rights": {"delete_messages": True},
            "rank": "Editor",
        },
        methods.PromoteChatMember,
    ),
    ({"kind": "group_ban", "user_id": "8"}, methods.BanChatMember),
    ({"kind": "group_unban", "user_id": "8"}, methods.UnbanChatMember),
    ({"kind": "group_remove", "user_id": "8"}, methods.BanChatMember),
    (
        {"kind": "group_permissions", "permissions": {"send_media": False}},
        methods.SetChatPermissions,
    ),
    ({"kind": "group_export_invite"}, methods.ExportChatInviteLink),
    ({"kind": "group_topic_create", "title": "Approved"}, methods.CreateForumTopic),
    (
        {"kind": "group_topic_edit", "topic_id": "11", "title": "Approved", "closed": True},
        methods.EditForumTopic,
    ),
    ({"kind": "group_topic_edit", "topic_id": "1", "hidden": True}, methods.HideGeneralForumTopic),
    ({"kind": "group_topic_delete", "topic_id": "11"}, methods.DeleteForumTopic),
    ({"kind": "account_photo_set", "source_path": "FILE"}, methods.SetMyProfilePhoto),
    ({"kind": "account_photo_delete"}, methods.RemoveMyProfilePhoto),
    (
        {
            "kind": "account_bot_commands",
            "bot_id": "7",
            "commands": [{"command": "help", "description": "Reviewed help"}],
        },
        methods.SetMyCommands,
    ),
]


@pytest.mark.parametrize(
    "operation,method_type",
    OPERATIONS,
    ids=[f"{x[0]['kind']}{i}" for i, x in enumerate(OPERATIONS)],
)
async def test_confirmed_management_uses_real_aiogram_requests(
    tmp_path, bot_sdk, operation, method_type
):
    photo = tmp_path / "reviewed.jpg"
    photo.write_bytes(b"\xff\xd8\xffreviewed bytes")
    group = operation["kind"].startswith("group_")
    operation = {
        **operation,
        **({"chat_id": GROUP} if group else {}),
        **({"source_path": str(photo)} if operation.get("source_path") else {}),
    }
    settings = Settings(
        data_dir=tmp_path / "state",
        profiles={
            "robot": Profile(
                kind="bot",
                identity={"id": "7"},
                manage_scopes=["groups", "account"],
                file_roots=[str(tmp_path)],
            )
        },
    )
    settings.limits.interval_seconds = 1
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        preview = data(
            await session.call_tool(
                "administration_preview" if group else "account_preview",
                {"profile_id": "robot", "operation": operation},
            )
        )
        assert preview["ok"], preview
        plan = preview["data"]
        assert "PRIVATE_FILE_TOKEN" not in json.dumps(plan)
        assert not any(isinstance(x, method_type) for x in bot_sdk)
        started = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "robot",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert started["ok"], started
        finished = await complete(session, "robot", started["data"]["job_id"])
        assert finished["status"] == "completed", finished
        assert sum(isinstance(x, method_type) for x in bot_sdk) == 1
        if operation["kind"] == "group_remove":
            assert sum(isinstance(x, methods.UnbanChatMember) for x in bot_sdk) == 1
        if operation["kind"] == "group_admin":
            assert sum(isinstance(x, methods.SetChatAdministratorCustomTitle) for x in bot_sdk) == 1
        if operation["kind"] == "group_topic_edit" and operation.get("closed"):
            assert sum(isinstance(x, methods.CloseForumTopic) for x in bot_sdk) == 1


async def test_bot_api_gaps_and_platform_restrictions_fail_before_confirmation(tmp_path, bot_sdk):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "robot": Profile(kind="bot", identity={"id": "7"}, manage_scopes=["groups", "account"])
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        for tool, operation, code in [
            ("administration_read", {"kind": "participants", "chat_id": GROUP}, "backend_required"),
            (
                "administration_preview",
                {"kind": "group_create_channel", "title": "No"},
                "platform_restriction",
            ),
            (
                "administration_preview",
                {
                    "kind": "group_permissions",
                    "chat_id": GROUP,
                    "permissions": {"send_gifs": False},
                },
                "backend_required",
            ),
            (
                "account_preview",
                {"kind": "account_profile", "first_name": "No"},
                "platform_restriction",
            ),
            (
                "account_preview",
                {"kind": "account_bot_commands", "bot_id": "8", "commands": []},
                "platform_restriction",
            ),
        ]:
            result = data(
                await session.call_tool(tool, {"profile_id": "robot", "operation": operation})
            )
            assert result["error"]["code"] == code, result
        assert not any(
            isinstance(x, (methods.SetChatPermissions, methods.SetMyCommands)) for x in bot_sdk
        )
