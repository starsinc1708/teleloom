"""Confirmed group and account operations; SDK writers run only in the delivery worker."""

import io
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from telethon import errors, functions, types, utils

from .administration import ID, Strict, backend_required, photo_info, privacy_rule, user_info
from .models import TeleloomError, iso

UserIDs = Annotated[list[ID], Field(min_length=1, max_length=100)]
EmojiID = Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]*)$")]


class GroupTarget(Strict):
    chat_id: ID


class CreateGroup(Strict):
    kind: Literal["group_create"]
    title: str = Field(min_length=1, max_length=128)
    user_ids: UserIDs


class CreateChannel(Strict):
    kind: Literal["group_create_channel"]
    title: str = Field(min_length=1, max_length=128)
    about: str = Field(default="", max_length=255)
    megagroup: bool = False


class InviteMembers(GroupTarget):
    kind: Literal["group_invite"]
    user_ids: UserIDs
    max_requests: Literal[100] = 100


class GroupSimple(GroupTarget):
    kind: Literal["group_leave", "group_join", "group_photo_delete", "group_export_invite"]


class ImportInvite(Strict):
    kind: Literal["group_import_invite"]
    invite_hash: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("invite_hash", mode="before")
    @classmethod
    def exact_invite(cls, value: str) -> str:
        if isinstance(value, str):
            match = re.fullmatch(
                r"(?:https://t\.me/(?:\+|joinchat/)|tg://join\?invite=)([A-Za-z0-9_-]+)", value
            )
            if match:
                return match[1]
        return value


class GroupTitle(GroupTarget):
    kind: Literal["group_title"]
    title: str = Field(min_length=1, max_length=128)


class GroupAbout(GroupTarget):
    kind: Literal["group_about"]
    about: str = Field(max_length=255)


class GroupPhoto(GroupTarget):
    kind: Literal["group_photo_set"]
    source_path: str = Field(min_length=1, max_length=4096)


class AdminRights(Strict):
    change_info: bool = False
    post_messages: bool = False
    edit_messages: bool = False
    delete_messages: bool = False
    ban_users: bool = False
    invite_users: bool = False
    pin_messages: bool = False
    add_admins: bool = False
    anonymous: bool = False
    manage_call: bool = False
    manage_topics: bool = False
    other: bool = False
    post_stories: bool = False
    edit_stories: bool = False
    delete_stories: bool = False
    manage_direct_messages: bool = False


class EditAdmin(GroupTarget):
    kind: Literal["group_admin"]
    user_id: ID
    rights: AdminRights = Field(default_factory=AdminRights)
    rank: str = Field(default="", max_length=16)
    max_requests: Literal[2] = 2


class BanMember(GroupTarget):
    kind: Literal["group_ban", "group_unban", "group_remove"]
    user_id: ID
    max_requests: Literal[2] = 2


class MemberPermissions(Strict):
    send_messages: bool = True
    send_media: bool = True
    send_stickers: bool = True
    send_gifs: bool = True
    send_games: bool = True
    send_inline: bool = True
    embed_links: bool = True
    send_polls: bool = True
    change_info: bool = False
    invite_users: bool = True
    pin_messages: bool = False


class DefaultPermissions(GroupTarget):
    kind: Literal["group_permissions"]
    permissions: MemberPermissions
    until_date: datetime | None = None

    @model_validator(mode="after")
    def aware(self) -> "DefaultPermissions":
        if self.until_date:
            self.until_date = datetime.fromisoformat(iso(self.until_date))
        return self


class SlowMode(GroupTarget):
    kind: Literal["group_slow_mode"]
    seconds: Literal[0, 10, 30, 60, 300, 900, 3600] = 0


class ForumMode(GroupTarget):
    kind: Literal["group_forum"]
    enabled: bool = True
    tabs: bool = True


class CreateTopic(GroupTarget):
    kind: Literal["group_topic_create"]
    title: str = Field(min_length=1, max_length=128)
    icon_color: int | None = Field(default=None, ge=0, le=0xFFFFFF)
    icon_emoji_id: EmojiID | None = None


class EditTopic(GroupTarget):
    kind: Literal["group_topic_edit"]
    topic_id: ID
    title: str | None = Field(default=None, min_length=1, max_length=128)
    icon_emoji_id: EmojiID | None = None
    closed: bool | None = None
    hidden: bool | None = None
    max_requests: Literal[3] = 3

    @model_validator(mode="after")
    def change(self) -> "EditTopic":
        if all(getattr(self, x) is None for x in ("title", "icon_emoji_id", "closed", "hidden")):
            raise ValueError("provide a topic change")
        if self.hidden is not None and self.topic_id != "1":
            raise ValueError("only General topic 1 can be hidden")
        return self


class DeleteTopic(GroupTarget):
    kind: Literal["group_topic_delete"]
    topic_id: ID
    max_requests: int = Field(default=100, ge=1, le=1000)


AdministrationOperation = Annotated[
    CreateGroup
    | CreateChannel
    | InviteMembers
    | GroupSimple
    | ImportInvite
    | GroupTitle
    | GroupAbout
    | GroupPhoto
    | EditAdmin
    | BanMember
    | DefaultPermissions
    | SlowMode
    | ForumMode
    | CreateTopic
    | EditTopic
    | DeleteTopic,
    Field(discriminator="kind"),
]


class UpdateProfile(Strict):
    kind: Literal["account_profile"]
    first_name: str | None = Field(default=None, min_length=1, max_length=64)
    last_name: str | None = Field(default=None, max_length=64)
    about: str | None = Field(default=None, max_length=70)

    @model_validator(mode="after")
    def change(self) -> "UpdateProfile":
        if all(getattr(self, x) is None for x in ("first_name", "last_name", "about")):
            raise ValueError("provide a profile change")
        return self


class ProfilePhoto(Strict):
    kind: Literal["account_photo_set"]
    source_path: str = Field(min_length=1, max_length=4096)


class DeleteProfilePhoto(Strict):
    kind: Literal["account_photo_delete"]


class Privacy(Strict):
    kind: Literal["account_privacy"]
    key: Literal["status", "phone", "profile_photo"]
    base: Literal["allow_all", "contacts", "deny_all"] = "deny_all"
    allow_users: list[ID] = Field(default_factory=list, max_length=100)
    disallow_users: list[ID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def distinct(self) -> "Privacy":
        if set(self.allow_users) & set(self.disallow_users):
            raise ValueError("privacy exceptions cannot both allow and deny one user")
        if len(set(self.allow_users)) != len(self.allow_users) or len(
            set(self.disallow_users)
        ) != len(self.disallow_users):
            raise ValueError("privacy exception IDs must be unique")
        return self


class Command(Strict):
    command: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    description: str = Field(min_length=1, max_length=256)


class BotCommands(Strict):
    kind: Literal["account_bot_commands"]
    bot_id: ID
    commands: list[Command] = Field(default_factory=list, max_length=100)
    language_code: str = Field(default="", pattern=r"^(?:[a-z]{2})?$")

    @model_validator(mode="after")
    def unique(self) -> "BotCommands":
        if len({x.command for x in self.commands}) != len(self.commands):
            raise ValueError("command names must be unique")
        return self


AccountOperation = Annotated[
    UpdateProfile | ProfilePhoto | DeleteProfilePhoto | Privacy | BotCommands,
    Field(discriminator="kind"),
]

USER_GROUP_OPERATIONS = {
    "group_create",
    "group_create_channel",
    "group_join",
    "group_import_invite",
    "group_invite",
    "group_slow_mode",
    "group_forum",
}
BOT_API_GROUP_OPERATIONS = {
    "group_leave",
    "group_title",
    "group_about",
    "group_photo_set",
    "group_photo_delete",
    "group_admin",
    "group_ban",
    "group_unban",
    "group_remove",
    "group_permissions",
    "group_export_invite",
    "group_topic_create",
    "group_topic_edit",
    "group_topic_delete",
}


def operation_allowed(profile: Any, p: dict[str, Any]) -> None:
    from .runtime import number

    scope = "groups" if p["kind"].startswith("group_") else "account"
    if scope not in profile.manage_scopes:
        raise TeleloomError(
            "management_not_allowed",
            f"Owner must explicitly enable {scope} management through CLI.",
        )
    for key in ("chat_id", "user_id", "bot_id", "topic_id", "icon_emoji_id"):
        if p.get(key) and not (key == "icon_emoji_id" and p[key] == "0"):
            id_ = number(p[key], positive=key in {"user_id", "bot_id", "topic_id", "icon_emoji_id"})
            if key == "topic_id" and id_ >= 2**31:
                raise TeleloomError(
                    "invalid_id", "Topic IDs must fit Telegram's positive 32-bit range."
                )
    for key in ("user_ids", "allow_users", "disallow_users"):
        for id_ in p.get(key, []):
            number(id_, positive=True)
        if len(p.get(key, [])) != len(set(p.get(key, []))):
            raise TeleloomError("invalid_targets", "Selected user IDs must be unique.")
    if p["kind"] == "account_bot_commands" and (
        profile.kind != "bot" or p["bot_id"] != profile.identity.get("id")
    ):
        raise TeleloomError(
            "platform_restriction", "Command menus belong to this authenticated bot only."
        )
    if profile.kind == "bot":
        if p["kind"] in USER_GROUP_OPERATIONS or p["kind"] in {
            "account_profile",
            "account_privacy",
        }:
            raise TeleloomError(
                "platform_restriction", "This Telegram method is available only to user accounts."
            )
        if (
            profile.bot_backend == "bot_api"
            and p["kind"].startswith("group_")
            and p["kind"] not in BOT_API_GROUP_OPERATIONS
        ):
            raise backend_required(p["kind"])
        if profile.bot_backend == "bot_api":
            if p["kind"] == "group_permissions" and (
                p["until_date"]
                or len(
                    {
                        p["permissions"][k]
                        for k in ("send_stickers", "send_gifs", "send_games", "send_inline")
                    }
                )
                > 1
            ):
                raise backend_required(p["kind"])
            if (
                p["kind"] == "group_topic_edit"
                and p["topic_id"] == "1"
                and p["icon_emoji_id"] is not None
            ):
                raise backend_required(p["kind"])


def privacy_key(key: str) -> Any:
    return {
        "status": types.InputPrivacyKeyStatusTimestamp,
        "phone": types.InputPrivacyKeyPhoneNumber,
        "profile_photo": types.InputPrivacyKeyProfilePhoto,
    }[key]()


def created_receipt(response: Any) -> dict[str, Any]:
    updates = response if hasattr(response, "chats") else getattr(response, "updates", response)
    chats = getattr(updates, "chats", [])
    return {
        "accepted": True,
        "complete": not bool(getattr(response, "missing_invitees", [])),
        "chat_ids": [str(utils.get_peer_id(x)) for x in chats],
        "missing_invitees": [
            {
                "user_id": str(x.user_id),
                "premium_required": bool(getattr(x, "premium_required_for_pm", False)),
                "premium_would_allow": bool(getattr(x, "premium_would_allow_invite", False)),
            }
            for x in getattr(response, "missing_invitees", [])
        ],
    }


async def user_mutate(
    adapter: Any,
    p: dict[str, Any],
    random_id: int,
    receipts: list[dict[str, Any]],
    file_bytes: bytes | None = None,
) -> dict[str, Any]:
    kind = p["kind"]
    upload_bytes = file_bytes or b""
    if kind in {"group_photo_set", "account_photo_set"} and not upload_bytes:
        raise TeleloomError(
            "file_snapshot_missing", "Photo writes require immutable reviewed bytes."
        )
    peer: Any = await adapter._input_peer(p["chat_id"]) if p.get("chat_id") else None
    if kind == "group_create":
        users = [await adapter._input_peer(x) for x in p["user_ids"]]
        return created_receipt(
            await adapter.client(
                functions.messages.CreateChatRequest(users=users, title=p["title"])
            )
        )
    if kind == "group_create_channel":
        return created_receipt(
            await adapter.client(
                functions.channels.CreateChannelRequest(
                    title=p["title"],
                    about=p["about"],
                    megagroup=p["megagroup"],
                    broadcast=not p["megagroup"],
                )
            )
        )
    if kind in {"group_import_invite", "group_join"}:
        request = (
            functions.messages.ImportChatInviteRequest(hash=p["invite_hash"])
            if kind == "group_import_invite"
            else functions.channels.JoinChannelRequest(channel=peer)
        )
        try:
            response = await adapter.client(request)
        except errors.InviteRequestSentError:
            return {"accepted": True, "join_state": "approval_pending", "joined": False}
        except errors.UserAlreadyParticipantError:
            return {"accepted": True, "join_state": "already_joined", "joined": True}
        return {**created_receipt(response), "join_state": "joined", "joined": True}
    if kind == "group_invite":
        if isinstance(peer, types.InputPeerChannel):
            return created_receipt(
                await adapter.client(
                    functions.channels.InviteToChannelRequest(
                        channel=peer, users=[await adapter._input_peer(x) for x in p["user_ids"]]
                    )
                )
            )
        position = len(receipts)
        id_ = p["user_ids"][position]
        try:
            response = await adapter.client(
                functions.messages.AddChatUserRequest(
                    chat_id=peer.chat_id, user_id=await adapter._input_peer(id_), fwd_limit=0
                )
            )
            result = {**created_receipt(response), "user_id": id_}
        except errors.RPCError as exc:
            from .adapters import telegram_error

            error = telegram_error(exc)
            if error.code not in {"telegram_rejected", "account_restricted", "peer_unavailable"}:
                raise
            result = {
                "accepted": False,
                "user_id": id_,
                "error": {"code": error.code, "message": error.message},
            }
        more = position + 1 < len(p["user_ids"])
        if more:
            return {**result, "continue": True}
        member_results = [
            {key: item[key] for key in ("user_id", "accepted", "error") if key in item}
            for item in [*receipts, result]
        ]
        return {
            **result,
            "continue": False,
            "member_results": member_results,
            "complete": all(item["accepted"] for item in member_results),
        }
    if kind == "group_leave":
        if isinstance(peer, types.InputPeerChannel):
            await adapter.client(functions.channels.LeaveChannelRequest(channel=peer))
        else:
            await adapter.client(
                functions.messages.DeleteChatUserRequest(
                    chat_id=peer.chat_id, user_id=types.InputUserSelf()
                )
            )
    elif kind == "group_title":
        request = (
            functions.channels.EditTitleRequest(channel=peer, title=p["title"])
            if isinstance(peer, types.InputPeerChannel)
            else functions.messages.EditChatTitleRequest(chat_id=peer.chat_id, title=p["title"])
        )
        await adapter.client(request)
    elif kind == "group_about":
        await adapter.client(functions.messages.EditChatAboutRequest(peer=peer, about=p["about"]))
    elif kind in {"group_photo_set", "group_photo_delete"}:
        photo: Any = types.InputChatPhotoEmpty()
        if kind == "group_photo_set":
            photo = types.InputChatUploadedPhoto(
                file=await adapter.client.upload_file(
                    io.BytesIO(upload_bytes), file_name=p["file"]["name"]
                )
            )
        request = (
            functions.channels.EditPhotoRequest(channel=peer, photo=photo)
            if isinstance(peer, types.InputPeerChannel)
            else functions.messages.EditChatPhotoRequest(chat_id=peer.chat_id, photo=photo)
        )
        await adapter.client(request)
    elif kind == "group_admin":
        member = await adapter._input_peer(p["user_id"])
        if isinstance(peer, types.InputPeerChannel):
            await adapter.client(
                functions.channels.EditAdminRequest(
                    channel=peer,
                    user_id=member,
                    admin_rights=types.ChatAdminRights(**p["rights"]),
                    rank=p["rank"],
                )
            )
        else:
            if p["rank"] or any(
                value
                for key, value in p["rights"].items()
                if key
                not in {
                    "change_info",
                    "delete_messages",
                    "ban_users",
                    "invite_users",
                    "pin_messages",
                }
            ):
                raise TeleloomError(
                    "platform_restriction",
                    "Basic groups support an admin flag, without granular channel rights or ranks.",
                )
            await adapter.client(
                functions.messages.EditChatAdminRequest(
                    chat_id=peer.chat_id, user_id=member, is_admin=any(p["rights"].values())
                )
            )
    elif kind in {"group_ban", "group_unban", "group_remove"}:
        member = await adapter._input_peer(p["user_id"])
        if isinstance(peer, types.InputPeerChat):
            if kind == "group_ban":
                raise TeleloomError(
                    "platform_restriction",
                    "Permanent bans require a supergroup/channel; use remove for a basic group.",
                )
            if kind == "group_unban":
                raise TeleloomError(
                    "platform_restriction", "Basic groups do not maintain channel bans."
                )
            await adapter.client(
                functions.messages.DeleteChatUserRequest(chat_id=peer.chat_id, user_id=member)
            )
        else:
            banned = kind == "group_ban" or (kind == "group_remove" and not receipts)
            await adapter.client(
                functions.channels.EditBannedRequest(
                    channel=peer,
                    participant=member,
                    banned_rights=types.ChatBannedRights(until_date=None, view_messages=banned),
                )
            )
            if kind == "group_remove" and not receipts:
                return {"accepted": True, "phase": "ejected_with_ban", "continue": True}
    elif kind == "group_permissions":
        rights = {key: not value for key, value in p["permissions"].items()}
        await adapter.client(
            functions.messages.EditChatDefaultBannedRightsRequest(
                peer=peer,
                banned_rights=types.ChatBannedRights(
                    until_date=datetime.fromisoformat(p["until_date"]) if p["until_date"] else None,
                    **rights,
                ),
            )
        )
    elif kind == "group_slow_mode":
        await adapter.client(
            functions.channels.ToggleSlowModeRequest(channel=peer, seconds=p["seconds"])
        )
    elif kind == "group_forum":
        await adapter.client(
            functions.channels.ToggleForumRequest(
                channel=peer, enabled=p["enabled"], tabs=p["tabs"]
            )
        )
    elif kind == "group_export_invite":
        response = await adapter.client(functions.messages.ExportChatInviteRequest(peer=peer))
        return {"accepted": True, "invite_link": response.link}
    elif kind == "group_topic_create":
        response = await adapter.client(
            functions.messages.CreateForumTopicRequest(
                peer=peer,
                title=p["title"],
                icon_color=p["icon_color"],
                icon_emoji_id=int(p["icon_emoji_id"]) if p["icon_emoji_id"] else None,
                random_id=random_id,
            )
        )
        topics = [
            str(x.message.id)
            for x in getattr(response, "updates", [])
            if isinstance(
                getattr(getattr(x, "message", None), "action", None), types.MessageActionTopicCreate
            )
        ]
        if not topics:
            raise TeleloomError(
                "delivery_unknown",
                "Telegram acknowledged topic creation without an identifiable topic receipt.",
            )
        return {"accepted": True, "topic_ids": topics}
    elif kind == "group_topic_edit":
        await adapter.client(
            functions.messages.EditForumTopicRequest(
                peer=peer,
                topic_id=int(p["topic_id"]),
                title=p["title"],
                icon_emoji_id=int(p["icon_emoji_id"]) if p["icon_emoji_id"] else None,
                closed=p["closed"],
                hidden=p["hidden"],
            )
        )
    elif kind == "group_topic_delete":
        response = await adapter.client(
            functions.messages.DeleteTopicHistoryRequest(peer=peer, top_msg_id=int(p["topic_id"]))
        )
        return {
            "accepted": True,
            "continue": bool(response.offset),
            "remaining_offset": response.offset,
        }
    elif kind == "account_profile":
        response = await adapter.client(
            functions.account.UpdateProfileRequest(
                first_name=p["first_name"], last_name=p["last_name"], about=p["about"]
            )
        )
        return {"accepted": True, "item": user_info(response)}
    elif kind == "account_photo_set":
        response = await adapter.client(
            functions.photos.UploadProfilePhotoRequest(
                file=await adapter.client.upload_file(
                    io.BytesIO(upload_bytes), file_name=p["file"]["name"]
                )
            )
        )
        return {"accepted": True, "photo": photo_info(response.photo)}
    elif kind == "account_photo_delete":
        response = await adapter.client(
            functions.photos.GetUserPhotosRequest(
                user_id=types.InputUserSelf(), offset=0, max_id=0, limit=1
            )
        )
        photo = next(iter(response.photos), None)
        if not photo or str(photo.id) != p["photo_id"]:
            raise TeleloomError("source_changed", "The reviewed current profile photo changed.")
        if adapter.profile.kind == "bot":
            await adapter.client(
                functions.photos.UpdateProfilePhotoRequest(id=types.InputPhotoEmpty())
            )
            return {"accepted": True, "removed_current_photo_id": p["photo_id"]}
        deleted = await adapter.client(
            functions.photos.DeletePhotosRequest(id=[utils.get_input_photo(photo)])
        )
        return {"accepted": True, "deleted_photo_ids": [str(x) for x in deleted]}
    elif kind == "account_privacy":
        rules = []
        if p["disallow_users"]:
            rules.append(
                types.InputPrivacyValueDisallowUsers(
                    users=[await adapter._input_peer(x) for x in p["disallow_users"]]
                )
            )
        if p["allow_users"]:
            rules.append(
                types.InputPrivacyValueAllowUsers(
                    users=[await adapter._input_peer(x) for x in p["allow_users"]]
                )
            )
        rules.append(
            {
                "allow_all": types.InputPrivacyValueAllowAll,
                "contacts": types.InputPrivacyValueAllowContacts,
                "deny_all": types.InputPrivacyValueDisallowAll,
            }[p["base"]]()
        )
        await adapter.client(
            functions.account.SetPrivacyRequest(key=privacy_key(p["key"]), rules=rules)
        )
    elif kind == "account_bot_commands":
        await adapter.client(
            functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code=p["language_code"],
                commands=[types.BotCommand(**x) for x in p["commands"]],
            )
        )
    else:
        raise TeleloomError("invalid_operation", "Unknown confirmed management operation.")
    return {"accepted": True}


async def bot_mutate(
    adapter: Any,
    p: dict[str, Any],
    random_id: int,
    receipts: list[dict[str, Any]],
    file_bytes: bytes | None = None,
) -> dict[str, Any]:
    from aiogram.types import (
        BotCommand,
        BufferedInputFile,
        ChatPermissions,
        InputProfilePhotoStatic,
    )

    kind, bot = p["kind"], adapter.bot
    if kind in {"group_photo_set", "account_photo_set"} and not file_bytes:
        raise TeleloomError(
            "file_snapshot_missing", "Photo writes require immutable reviewed bytes."
        )
    chat = int(p["chat_id"]) if p.get("chat_id") else None
    if kind == "group_leave":
        await bot.leave_chat(chat)
    elif kind == "group_title":
        await bot.set_chat_title(chat, p["title"])
    elif kind == "group_about":
        await bot.set_chat_description(chat, p["about"])
    elif kind == "group_photo_set":
        await bot.set_chat_photo(
            chat, BufferedInputFile(file_bytes or b"", filename=p["file"]["name"])
        )
    elif kind == "group_photo_delete":
        await bot.delete_chat_photo(chat)
    elif kind == "group_admin":
        mapping = {
            "change_info": "can_change_info",
            "post_messages": "can_post_messages",
            "edit_messages": "can_edit_messages",
            "delete_messages": "can_delete_messages",
            "ban_users": "can_restrict_members",
            "invite_users": "can_invite_users",
            "pin_messages": "can_pin_messages",
            "add_admins": "can_promote_members",
            "anonymous": "is_anonymous",
            "manage_call": "can_manage_video_chats",
            "manage_topics": "can_manage_topics",
            "other": "can_manage_chat",
            "post_stories": "can_post_stories",
            "edit_stories": "can_edit_stories",
            "delete_stories": "can_delete_stories",
            "manage_direct_messages": "can_manage_direct_messages",
        }
        if not receipts:
            await bot.promote_chat_member(
                chat,
                int(p["user_id"]),
                **{mapping[key]: value for key, value in p["rights"].items()},
            )
        if p["rank"]:
            # Separate durable step: promoting may succeed while setting the title fails.
            if not receipts:
                return {"accepted": True, "phase": "rights_updated", "continue": True}
            await bot.set_chat_administrator_custom_title(chat, int(p["user_id"]), p["rank"])
    elif kind in {"group_ban", "group_unban", "group_remove"}:
        if kind == "group_unban" or (kind == "group_remove" and receipts):
            await bot.unban_chat_member(chat, int(p["user_id"]), only_if_banned=True)
        else:
            await bot.ban_chat_member(chat, int(p["user_id"]), revoke_messages=True)
            if kind == "group_remove":
                return {"accepted": True, "phase": "ejected_with_ban", "continue": True}
    elif kind == "group_permissions":
        permissions = p["permissions"]
        mapped = {
            "can_send_messages": permissions["send_messages"],
            "can_send_polls": permissions["send_polls"],
            "can_add_web_page_previews": permissions["embed_links"],
            "can_change_info": permissions["change_info"],
            "can_invite_users": permissions["invite_users"],
            "can_pin_messages": permissions["pin_messages"],
        }
        media_keys = (
            "can_send_audios",
            "can_send_documents",
            "can_send_photos",
            "can_send_videos",
            "can_send_video_notes",
            "can_send_voice_notes",
        )
        mapped.update({key: permissions["send_media"] for key in media_keys})
        combined = [
            permissions[key] for key in ("send_stickers", "send_gifs", "send_games", "send_inline")
        ]
        if len(set(combined)) > 1 or p["until_date"]:
            raise backend_required(kind)
        mapped["can_send_other_messages"] = combined[0]
        await bot.set_chat_permissions(
            chat, ChatPermissions(**mapped), use_independent_chat_permissions=True
        )
    elif kind == "group_export_invite":
        return {"accepted": True, "invite_link": await bot.export_chat_invite_link(chat)}
    elif kind == "group_topic_create":
        topic = await bot.create_forum_topic(
            chat, p["title"], icon_color=p["icon_color"], icon_custom_emoji_id=p["icon_emoji_id"]
        )
        return {"accepted": True, "topic_ids": [str(topic.message_thread_id)]}
    elif kind == "group_topic_edit":
        # Telegram Bot API splits editing and open/close into separate mutations.
        actions = []
        if p["title"] is not None or p["icon_emoji_id"] is not None:
            actions.append("edit")
        if p["closed"] is not None:
            actions.append("close" if p["closed"] else "reopen")
        if p["hidden"] is not None:
            actions.append("hide" if p["hidden"] else "unhide")
        position = len(receipts)
        action = actions[position]
        general = p["topic_id"] == "1"
        if action == "edit":
            if general:
                if p["icon_emoji_id"] is not None:
                    raise backend_required(kind)
                await bot.edit_general_forum_topic(chat, p["title"])
            else:
                await bot.edit_forum_topic(
                    chat,
                    int(p["topic_id"]),
                    name=p["title"],
                    icon_custom_emoji_id=p["icon_emoji_id"],
                )
        else:
            method = getattr(bot, action + ("_general_forum_topic" if general else "_forum_topic"))
            await method(chat) if general else await method(chat, int(p["topic_id"]))
        return {"accepted": True, "phase": action, "continue": position + 1 < len(actions)}
    elif kind == "group_topic_delete":
        await bot.delete_forum_topic(chat, int(p["topic_id"]))
    elif kind == "account_photo_set":
        await bot.set_my_profile_photo(
            InputProfilePhotoStatic(
                photo=BufferedInputFile(file_bytes or b"", filename=p["file"]["name"])
            )
        )
    elif kind == "account_photo_delete":
        await bot.remove_my_profile_photo()
    elif kind == "account_bot_commands":
        await bot.set_my_commands(
            [BotCommand(**x) for x in p["commands"]], language_code=p["language_code"] or None
        )
    else:
        raise backend_required(kind)
    return {"accepted": True}


async def state(adapter: Any, p: dict[str, Any]) -> list[dict[str, Any]]:
    """Freeze the relevant before-state without storing SDK secrets or volatile counts."""
    kind = p["kind"]
    sources = []
    if p.get("chat_id"):
        if hasattr(adapter, "client"):
            peer = await adapter._input_peer(p["chat_id"])
            if isinstance(peer, types.InputPeerChat) and (
                kind
                in {
                    "group_ban",
                    "group_unban",
                    "group_join",
                    "group_slow_mode",
                    "group_forum",
                    "group_topic_create",
                    "group_topic_edit",
                    "group_topic_delete",
                }
                or kind == "group_admin"
                and (
                    p["rank"]
                    or any(
                        value
                        for key, value in p["rights"].items()
                        if key
                        not in {
                            "change_info",
                            "delete_messages",
                            "ban_users",
                            "invite_users",
                            "pin_messages",
                        }
                    )
                )
            ):
                raise TeleloomError(
                    "platform_restriction",
                    "This operation requires a supergroup or channel rather than a basic group.",
                )
        result = await adapter.administration_read(
            {"kind": "chat", "chat_id": p["chat_id"], "include_dialog": False}, 0
        )
        item = result["item"]
        if item["id"] != p["chat_id"]:
            raise TeleloomError(
                "recipient_changed", "The resolved chat changed canonical identity."
            )
        sources.append(
            {
                "kind": "chat",
                **{
                    key: item.get(key)
                    for key in (
                        "id",
                        "title",
                        "about",
                        "photo",
                        "forum",
                        "slowmode_seconds",
                        "default_banned_rights",
                        "admin_rights",
                        "permissions",
                    )
                },
            }
        )
        if p.get("user_id"):
            result = await adapter.administration_read(
                {"kind": "member", "chat_id": p["chat_id"], "user_id": p["user_id"]}, 0
            )
            sources.append(
                {
                    "kind": "member",
                    "id": p["user_id"],
                    **{
                        key: result["item"].get(key)
                        for key in ("role", "rank", "admin_rights", "banned_rights", "permissions")
                    },
                }
            )
        if kind in {"group_topic_edit", "group_topic_delete"} and hasattr(adapter, "client"):
            response = await adapter.client(
                functions.messages.GetForumTopicsByIDRequest(
                    peer=await adapter._input_peer(p["chat_id"]), topics=[int(p["topic_id"])]
                )
            )
            topic = next((x for x in response.topics if str(x.id) == p["topic_id"]), None)
            if not topic or isinstance(topic, types.ForumTopicDeleted):
                raise TeleloomError("topic_not_found", "The selected forum topic was not found.")
            sources.append(
                {
                    "kind": "topic",
                    "id": p["topic_id"],
                    "title": topic.title,
                    "closed": topic.closed,
                    "hidden": topic.hidden,
                    "icon_emoji_id": str(topic.icon_emoji_id) if topic.icon_emoji_id else None,
                }
            )
        return sources
    if kind == "group_import_invite":
        response = await adapter.client(
            functions.messages.CheckChatInviteRequest(hash=p["invite_hash"])
        )
        chat = getattr(response, "chat", None)
        if chat:
            adapter.profile.require_read(str(utils.get_peer_id(chat)))
        return [
            {
                "kind": "invite",
                "hash": p["invite_hash"],
                "title": getattr(response, "title", getattr(chat, "title", None)),
                "chat_id": str(utils.get_peer_id(chat)) if chat else None,
                "already_joined": isinstance(response, types.ChatInviteAlready),
            }
        ]
    if kind == "account_privacy":
        response = await adapter.client(
            functions.account.GetPrivacyRequest(key=privacy_key(p["key"]))
        )
        return [
            {"kind": "privacy", "key": p["key"], "rules": [privacy_rule(x) for x in response.rules]}
        ]
    if kind == "account_bot_commands":
        if hasattr(adapter, "client"):
            commands = await adapter.client(
                functions.bots.GetBotCommandsRequest(
                    scope=types.BotCommandScopeDefault(), lang_code=p["language_code"]
                )
            )
        else:
            commands = await adapter.bot.get_my_commands(language_code=p["language_code"] or None)
        return [
            {
                "kind": "bot_commands",
                "items": [{"command": x.command, "description": x.description} for x in commands],
            }
        ]
    if kind == "account_profile":
        response = await adapter.client(
            functions.users.GetFullUserRequest(id=types.InputUserSelf())
        )
        me = await adapter.client.get_me()
        if response.full_user.id != me.id:
            raise TeleloomError(
                "peer_identity_changed", "Self profile metadata belongs to another account."
            )
        return [
            {
                "kind": "profile",
                "id": str(me.id),
                "first_name": me.first_name,
                "last_name": me.last_name,
                "about": response.full_user.about,
            }
        ]
    if kind in {"account_photo_set", "account_photo_delete"}:
        id_ = adapter.profile.identity["id"]
        if hasattr(adapter, "client"):
            response = await adapter.client(
                functions.photos.GetUserPhotosRequest(
                    user_id=types.InputUserSelf(), offset=0, max_id=0, limit=1
                )
            )
            photo = photo_info(next(iter(response.photos), None))
        else:
            response = await adapter.bot.get_user_profile_photos(int(id_), offset=0, limit=1)
            photo = {"id": response.photos[0][-1].file_unique_id} if response.photos else None
        if kind == "account_photo_delete" and not photo:
            raise TeleloomError(
                "photo_not_found", "This account has no current profile photo to delete."
            )
        return [{"kind": "profile_photo", "account_id": id_, "photo": photo}]
    return []


class Management:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def preview(
        self, profile_id: str, operation: AdministrationOperation | AccountOperation
    ) -> dict[str, Any]:
        config = self.runtime.settings.profile(profile_id)
        p = operation.model_dump(mode="json")
        operation_allowed(config, p)
        if p.get("chat_id"):
            config.require_read(p["chat_id"])
        adapter = await self.runtime.adapter(profile_id)
        sources = await state(adapter, p)
        if (
            config.kind == "bot"
            and config.bot_backend == "bot_api"
            and p["kind"] in {"group_ban", "group_remove"}
        ):
            p["effects"] = (
                "Bot API banning/ejecting in supergroups or channels also deletes all of this user's chat messages."
            )
        if p["kind"] == "group_topic_delete":
            p["effects"] = "Deleting the topic also deletes all messages in that topic."
        if p["kind"] == "account_photo_delete":
            p["photo_id"] = sources[0]["photo"]["id"]
        if p.get("source_path"):
            from .file_snapshots import FileSnapshots

            snapshots = FileSnapshots(self.runtime.settings, self.runtime.store)
            snapshots.cleanup_expired()
            p["file"] = snapshots.capture(profile_id, p["source_path"], max_bytes=10_000_000)
        targets = (
            [{"kind": "chat", "chat_id": p["chat_id"]}]
            if p.get("chat_id")
            else [
                {
                    "kind": "account",
                    "profile_id": profile_id,
                    "scope": "groups" if p["kind"].startswith("group_") else "account",
                }
            ]
        )
        return self.runtime.jobs.confirmed_preview(
            profile_id, p, targets=targets, sources=sources, resolved=sources
        )

    async def validate(
        self,
        profile_id: str,
        operation: dict[str, Any],
        sources: list[dict[str, Any]],
        receipts: list[dict[str, Any]] | None = None,
        *,
        require_source: bool = True,
    ) -> bytes | None:
        from .runtime import fingerprint

        config = self.runtime.settings.profile(profile_id)
        operation_allowed(config, operation)
        if operation.get("chat_id"):
            config.require_read(operation["chat_id"])
        if not receipts and fingerprint(
            await state(await self.runtime.adapter(profile_id), operation)
        ) != fingerprint(sources):
            raise TeleloomError(
                "source_changed",
                "The reviewed management before-state changed. Create a fresh preview.",
            )
        if operation.get("file"):
            from .file_snapshots import FileSnapshots

            return FileSnapshots(self.runtime.settings, self.runtime.store).validate(
                profile_id, operation["file"], require_source=require_source
            )
        return None


def capabilities(profile: Any) -> dict[str, Any]:
    mtproto = profile.kind == "user" or profile.bot_backend == "mtproto"
    return {
        "backend": "mtproto" if mtproto else "bot_api",
        "management_scopes": profile.model_dump().get("manage_scopes", []),
        "participants": "all_filters" if mtproto else "admins_and_exact_member",
        "audit": profile.kind == "user",
        "common_chats": profile.kind == "user",
        "full_user_metadata": mtproto,
        "avatar_history": True,
        "explicit_messages": mtproto,
        "account_privacy": profile.kind == "user",
        "profile_update": profile.kind == "user",
        "profile_photo": True,
        "bot_commands": profile.kind == "bot",
        "create_join_invite_groups": profile.kind == "user",
        "slow_mode_forum_enable": profile.kind == "user",
        "confirmed_group_management": True,
        "limits": "Telegram admin/Premium/peer access restrictions still apply; Bot API omissions can require opt-in MTProto provisioning.",
    }
