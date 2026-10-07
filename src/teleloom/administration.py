"""Bounded group/account metadata through the daemon's existing SDK connection."""

import io
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from telethon import functions, types, utils

from .models import TeleloomError, iso, utcnow

ID = Annotated[str, Field(pattern=r"^-?[1-9][0-9]*$")]
Limit = Annotated[int, Field(ge=1, le=100)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRead(Strict):
    kind: Literal["chat"]
    chat_id: ID
    include_dialog: bool = True


class MessageLinkRead(Strict):
    kind: Literal["message_link"]
    chat_id: ID
    message_id: ID
    thread: bool = False
    grouped: bool = False


class ParticipantsRead(Strict):
    kind: Literal["participants"]
    chat_id: ID
    filter: Literal["all", "admins", "banned"] = "all"
    query: str = Field(default="", max_length=128)
    limit: Limit = 50
    cursor: str | None = None


class MemberRead(Strict):
    kind: Literal["member"]
    chat_id: ID
    user_id: ID


class AuditRead(Strict):
    kind: Literal["audit"]
    chat_id: ID
    query: str = Field(default="", max_length=128)
    limit: Limit = 50
    cursor: str | None = None


class CommonRead(Strict):
    kind: Literal["common_chats"]
    user_id: ID
    limit: Limit = 50
    cursor: str | None = None


AdministrationRead = Annotated[
    ChatRead | MessageLinkRead | ParticipantsRead | MemberRead | AuditRead | CommonRead,
    Field(discriminator="kind"),
]


class SelfRead(Strict):
    kind: Literal["me", "privacy", "bot_commands"]


class UserRead(Strict):
    kind: Literal["user", "status", "bot_info"]
    user_id: ID


class PhotosRead(Strict):
    kind: Literal["photos"]
    user_id: ID
    limit: Limit = 10
    cursor: str | None = None


AccountRead = Annotated[SelfRead | UserRead | PhotosRead, Field(discriminator="kind")]


def timestamp(value: Any) -> str | None:
    return iso(value) if isinstance(value, datetime) else None


def photo_info(photo: Any) -> dict[str, Any] | None:
    if not photo or isinstance(
        photo, (types.PhotoEmpty, types.UserProfilePhotoEmpty, types.ChatPhotoEmpty)
    ):
        return None
    result: dict[str, Any] = {"id": str(getattr(photo, "id", getattr(photo, "photo_id", "")))}
    if hasattr(photo, "date"):
        result["date"] = timestamp(photo.date)
    result["has_video"] = bool(
        getattr(photo, "has_video", False) or getattr(photo, "video_sizes", None)
    )
    result["sizes"] = [
        {
            "type": size.type,
            "width": getattr(size, "w", None),
            "height": getattr(size, "h", None),
            "bytes": getattr(size, "size", None),
        }
        for size in getattr(photo, "sizes", [])
    ]
    return result


def user_info(user: Any) -> dict[str, Any]:
    status = getattr(user, "status", None)
    return {
        "id": str(user.id),
        "first_name": getattr(user, "first_name", None),
        "last_name": getattr(user, "last_name", None),
        "username": getattr(user, "username", None),
        "usernames": [x.username for x in getattr(user, "usernames", None) or []],
        "username_flags": [
            {"username": x.username, "active": bool(x.active), "editable": bool(x.editable)}
            for x in getattr(user, "usernames", None) or []
        ],
        "lang_code": getattr(user, "lang_code", None),
        "restriction_reasons": [
            {"platform": x.platform, "reason": x.reason, "text": x.text}
            for x in getattr(user, "restriction_reason", None) or []
        ],
        "bot": bool(getattr(user, "bot", getattr(user, "is_bot", False))),
        "verified": bool(getattr(user, "verified", False)),
        "premium": bool(getattr(user, "premium", getattr(user, "is_premium", False))),
        "deleted": bool(getattr(user, "deleted", False)),
        "flags": {
            key: bool(getattr(user, key, False))
            for key in (
                "is_self",
                "contact",
                "mutual_contact",
                "close_friend",
                "restricted",
                "scam",
                "fake",
                "support",
                "bot_nochats",
                "bot_inline_geo",
                "bot_attach_menu",
                "bot_business",
            )
        },
        "photo": photo_info(getattr(user, "photo", None)),
        "status": {
            "kind": type(status).__name__,
            "was_online": timestamp(getattr(status, "was_online", None)),
            "expires": timestamp(getattr(status, "expires", None)),
        }
        if status
        else None,
        "untrusted": True,
    }


def user_matches(user: dict[str, Any] | None, query: str) -> bool:
    return any(
        query.casefold() in ((user or {}).get(key) or "").casefold()
        for key in ("first_name", "last_name", "username")
    )


def rights_info(rights: Any) -> dict[str, Any] | None:
    if rights is None:
        return None
    return {
        key: timestamp(value) if isinstance(value, datetime) else value
        for key, value in vars(rights).items()
        if isinstance(value, (bool, int, datetime))
    }


def member_info(member: Any, user: Any | None = None) -> dict[str, Any]:
    peer = getattr(member, "peer", None)
    id_ = getattr(member, "user_id", None)
    if id_ is None and peer is not None:
        id_ = utils.get_peer_id(peer)
    return {
        "id": str(id_),
        "user": user_info(user) if user else None,
        "role": type(member).__name__,
        "rank": getattr(member, "rank", None),
        "date": timestamp(getattr(member, "date", None)),
        "admin_rights": rights_info(getattr(member, "admin_rights", None)),
        "banned_rights": rights_info(getattr(member, "banned_rights", None)),
    }


def chat_info(chat: Any, full: Any = None) -> dict[str, Any]:
    return {
        "id": str(utils.get_peer_id(chat)),
        "title": getattr(chat, "title", ""),
        "kind": "group"
        if isinstance(chat, types.Chat) or getattr(chat, "megagroup", False)
        else "channel",
        "username": getattr(chat, "username", None),
        "forum": bool(getattr(chat, "forum", False)),
        "photo": photo_info(getattr(full, "chat_photo", None) or getattr(chat, "photo", None)),
        "about": getattr(full, "about", None),
        "participants_count": getattr(
            full, "participants_count", getattr(chat, "participants_count", None)
        ),
        "linked_chat_id": str(utils.get_peer_id(types.PeerChannel(full.linked_chat_id)))
        if getattr(full, "linked_chat_id", None)
        else None,
        "slowmode_seconds": getattr(full, "slowmode_seconds", None),
        "default_banned_rights": rights_info(getattr(chat, "default_banned_rights", None)),
        "admin_rights": rights_info(getattr(chat, "admin_rights", None)),
        "untrusted": True,
    }


async def user_read(adapter: Any, p: dict[str, Any], offset: int = 0) -> dict[str, Any]:
    kind = p["kind"]
    if kind == "common_chats":
        response = await adapter.client(
            functions.messages.GetCommonChatsRequest(
                user_id=await adapter._input_peer(p["user_id"]), max_id=offset, limit=p["limit"]
            )
        )
        chats = response.chats
        return {
            "items": [chat_info(x) for x in chats],
            "next_position": chats[-1].id if len(chats) == p["limit"] else None,
        }
    peer = await adapter._input_peer(p["chat_id"])
    if kind == "message_link":
        if adapter.profile.kind != "user" or not isinstance(peer, types.InputPeerChannel):
            raise TeleloomError(
                "platform_restriction",
                "Telegram exported message links require a user and a channel/supergroup.",
            )
        response = await adapter.client(
            functions.channels.ExportMessageLinkRequest(
                channel=peer, id=int(p["message_id"]), thread=p["thread"], grouped=p["grouped"]
            )
        )
        return {
            "item": {"chat_id": p["chat_id"], "message_id": p["message_id"], "link": response.link}
        }
    if kind == "chat":
        chat = await adapter.client.get_entity(peer)
        if str(utils.get_peer_id(chat)) != p["chat_id"]:
            raise TeleloomError(
                "peer_identity_changed", "The resolved chat changed canonical identity."
            )
        if isinstance(peer, types.InputPeerChannel):
            response = await adapter.client(functions.channels.GetFullChannelRequest(channel=peer))
        elif isinstance(peer, types.InputPeerChat):
            response = await adapter.client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
        elif isinstance(peer, (types.InputPeerUser, types.InputPeerSelf)):
            result = await user_account_read(adapter, {"kind": "user", "user_id": p["chat_id"]})
            item = {
                **result["item"],
                "kind": "private",
                "title": " ".join(x for x in [chat.first_name, chat.last_name] if x),
            }
            response = None
        else:
            raise TeleloomError(
                "invalid_chat_kind", "Select a group or channel; use account_read for users."
            )
        if response:
            if response.full_chat.id != chat.id:
                raise TeleloomError(
                    "peer_identity_changed", "Telegram full-chat metadata belongs to another peer."
                )
            item = chat_info(chat, response.full_chat)
        if p.get("include_dialog", True) and adapter.profile.kind == "user":
            from .telegram.evidence import telethon_message

            dialog_result = await adapter.client(
                functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer=peer)])
            )
            dialog = next(
                (
                    x
                    for x in dialog_result.dialogs
                    if str(utils.get_peer_id(x.peer)) == p["chat_id"]
                ),
                None,
            )
            latest = next(
                (
                    x
                    for x in dialog_result.messages
                    if dialog
                    and x.id == dialog.top_message
                    and getattr(x, "chat_id", None) == int(p["chat_id"])
                    and getattr(x, "date", None)
                ),
                None,
            )
            item.update(
                unread_count=dialog.unread_count if dialog else None,
                unread_mark=bool(dialog.unread_mark) if dialog else None,
                archived=dialog.folder_id == 1 if dialog else None,
                unread_mentions_count=dialog.unread_mentions_count if dialog else None,
                read_inbox_max_id=str(dialog.read_inbox_max_id) if dialog else None,
                top_message_id=str(dialog.top_message) if dialog else None,
                latest_message=telethon_message(
                    adapter.profile_id, latest, chat_entity=chat
                ).model_dump(mode="json")
                if latest
                else None,
            )
            if not dialog:
                return {
                    "item": item,
                    "incomplete": True,
                    "warnings": ["Telegram did not return a dialog for this peer."],
                }
        elif p.get("include_dialog", True):
            return {
                "item": item,
                "incomplete": True,
                "warnings": [
                    "Telegram getPeerDialogs is user-only; bot unread/archive/latest dialog state is unavailable."
                ],
            }
        return {"item": item}
    if kind == "participants":
        if isinstance(peer, types.InputPeerChannel):
            filters = {
                "all": types.ChannelParticipantsSearch(p["query"]),
                "admins": types.ChannelParticipantsAdmins(),
                "banned": types.ChannelParticipantsKicked(p["query"]),
            }
            response = await adapter.client(
                functions.channels.GetParticipantsRequest(
                    channel=peer,
                    filter=filters[p["filter"]],
                    offset=offset,
                    limit=p["limit"],
                    hash=0,
                )
            )
            users = {str(x.id): x for x in response.users}
            items = [
                member_info(
                    x,
                    users.get(
                        str(getattr(x, "user_id", getattr(getattr(x, "peer", None), "user_id", "")))
                    ),
                )
                for x in response.participants
            ]
            total = response.count
            position = offset + len(response.participants)
            if p["filter"] == "admins" and p["query"]:
                items = [x for x in items if user_matches(x["user"], p["query"])]
        elif isinstance(peer, types.InputPeerChat):
            if p["filter"] == "banned":
                raise TeleloomError(
                    "platform_restriction", "Basic groups do not maintain channel bans."
                )
            response = await adapter.client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
            members = getattr(response.full_chat.participants, "participants", None)
            if members is None:
                raise TeleloomError(
                    "participants_unavailable", "Telegram did not expose this group's members."
                )
            users = {str(x.id): x for x in response.users}
            items = [
                member_info(x, users.get(str(x.user_id)))
                for x in members
                if (
                    p["filter"] == "all"
                    or (
                        p["filter"] == "admins"
                        and isinstance(
                            x, (types.ChatParticipantAdmin, types.ChatParticipantCreator)
                        )
                    )
                )
            ]
            if p["query"]:
                items = [x for x in items if user_matches(x["user"], p["query"])]
            total, items = len(items), items[offset : offset + p["limit"]]
            position = offset + len(items)
        else:
            raise TeleloomError(
                "invalid_chat_kind", "Participants require an exact group or channel."
            )
        return {
            "items": items,
            "total": total,
            "next_position": position if position > offset and position < total else None,
            "incomplete": position < total,
        }
    if kind == "member":
        if isinstance(peer, types.InputPeerChannel):
            response = await adapter.client(
                functions.channels.GetParticipantRequest(
                    channel=peer, participant=await adapter._input_peer(p["user_id"])
                )
            )
            users = {str(x.id): x for x in response.users}
            item = member_info(response.participant, users.get(p["user_id"]))
            if item["id"] != p["user_id"]:
                raise TeleloomError(
                    "peer_identity_changed", "Membership evidence belongs to another user."
                )
            return {"item": item}
        if isinstance(peer, types.InputPeerChat):
            response = await adapter.client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
            member = next(
                (
                    x
                    for x in getattr(response.full_chat.participants, "participants", [])
                    if str(x.user_id) == p["user_id"]
                ),
                None,
            )
            if not member:
                raise TeleloomError(
                    "member_not_found", "The selected user is not exposed as a member."
                )
            user = next((x for x in response.users if str(x.id) == p["user_id"]), None)
            return {"item": member_info(member, user)}
        raise TeleloomError("invalid_chat_kind", "Membership requires a group or channel.")
    if kind == "audit":
        if not isinstance(peer, types.InputPeerChannel):
            raise TeleloomError(
                "platform_restriction", "Telegram audit logs require a supergroup or channel."
            )
        response = await adapter.client(
            functions.channels.GetAdminLogRequest(
                channel=peer, q=p["query"], max_id=offset, min_id=0, limit=p["limit"]
            )
        )
        items = [
            {
                "id": str(x.id),
                "date": timestamp(x.date),
                "actor_id": str(x.user_id),
                "action": audit_action(x.action),
                "untrusted": True,
            }
            for x in response.events
        ]
        return {
            "items": items,
            "next_position": int(items[-1]["id"]) if len(items) == p["limit"] else None,
        }
    raise TeleloomError("invalid_operation", "Unknown metadata reader.")


def audit_action(action: Any) -> dict[str, Any]:
    # Explicit evidence only; raw SDK dictionaries contain access hashes and private fields.
    result: dict[str, Any] = {"kind": type(action).__name__}
    for key in ("prev_value", "new_value", "volume", "duration", "topic_id"):
        value = getattr(action, key, None)
        if isinstance(value, (str, bool, int)):
            result[key] = value
    for key in ("participant", "prev_participant", "new_participant"):
        value = getattr(action, key, None)
        if value:
            result[key] = member_info(value)
    for key in ("message", "prev_message", "new_message"):
        value = getattr(action, key, None)
        if value:
            result[key] = {
                "id": str(value.id),
                "text": getattr(value, "message", ""),
                "date": timestamp(getattr(value, "date", None)),
            }
    return result


async def user_account_read(adapter: Any, p: dict[str, Any], offset: int = 0) -> dict[str, Any]:
    kind = p["kind"]
    if kind == "me":
        return {"item": user_info(await adapter.client.get_me())}
    if kind == "bot_commands":
        response = await adapter.client(
            functions.bots.GetBotCommandsRequest(scope=types.BotCommandScopeDefault(), lang_code="")
        )
        return {"items": [{"command": x.command, "description": x.description} for x in response]}
    if kind == "privacy":
        response = await adapter.client(
            functions.account.GetPrivacyRequest(key=types.InputPrivacyKeyStatusTimestamp())
        )
        return {"key": "status", "items": [privacy_rule(x) for x in response.rules]}
    peer = await adapter._input_peer(p["user_id"])
    if kind == "photos":
        response = await adapter.client(
            functions.photos.GetUserPhotosRequest(
                user_id=peer, offset=offset, max_id=0, limit=p["limit"]
            )
        )
        items = [photo_info(x) for x in response.photos]
        total = getattr(response, "count", offset + len(items))
        return {
            "items": items,
            "total": total,
            "next_position": offset + len(items) if items and offset + len(items) < total else None,
        }
    user = await adapter.client.get_entity(peer)
    if not isinstance(user, types.User):
        raise TeleloomError("invalid_user_kind", "Select a user identity.")
    if str(user.id) != p["user_id"]:
        raise TeleloomError(
            "peer_identity_changed", "The resolved user changed canonical identity."
        )
    if kind == "status":
        return {"item": {"id": p["user_id"], "status": user_info(user)["status"]}}
    if kind == "bot_info" and not user.bot:
        raise TeleloomError("invalid_user_kind", "The selected identity is not a bot.")
    response = await adapter.client(functions.users.GetFullUserRequest(id=peer))
    full = response.full_user
    if full.id != user.id:
        raise TeleloomError("peer_identity_changed", "Full user metadata belongs to another peer.")
    item = {
        **user_info(user),
        "about": getattr(full, "about", None),
        "photo": photo_info(getattr(full, "profile_photo", None)),
        "common_chats_count": getattr(full, "common_chats_count", None),
        "blocked": getattr(full, "blocked", None),
        "private_forward_name": getattr(full, "private_forward_name", None),
        "pinned_msg_id": str(full.pinned_msg_id) if getattr(full, "pinned_msg_id", None) else None,
        "stargifts_count": getattr(full, "stargifts_count", None),
    }
    birthday = getattr(full, "birthday", None)
    item["birthday"] = (
        {"day": birthday.day, "month": birthday.month, "year": birthday.year} if birthday else None
    )
    item["personal_channel_id"] = (
        str(utils.get_peer_id(types.PeerChannel(full.personal_channel_id)))
        if getattr(full, "personal_channel_id", None)
        else None
    )
    bot = getattr(full, "bot_info", None)
    item["bot_info"] = (
        {
            "description": getattr(bot, "description", None),
            "commands": [
                {"command": x.command, "description": x.description}
                for x in getattr(bot, "commands", [])
            ],
        }
        if bot
        else None
    )
    hours = getattr(full, "business_work_hours", None)
    item["business_work_hours"] = (
        {
            "timezone_id": hours.timezone_id,
            "open_now": hours.open_now,
            "weekly_open": [
                {"start_minute": x.start_minute, "end_minute": x.end_minute}
                for x in hours.weekly_open
            ],
        }
        if hours
        else None
    )
    location = getattr(full, "business_location", None)
    item["business_location"] = {"address": location.address} if location else None
    intro = getattr(full, "business_intro", None)
    item["business_intro"] = (
        {"title": intro.title, "description": intro.description} if intro else None
    )
    return {"item": item}


def privacy_rule(rule: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": type(rule).__name__}
    for key in ("users", "chats"):
        if hasattr(rule, key):
            result[key] = [str(x) for x in getattr(rule, key)]
    return result


async def bot_read(adapter: Any, p: dict[str, Any], offset: int = 0) -> dict[str, Any]:
    kind = p["kind"]
    if kind == "member":
        member = await adapter.bot.get_chat_member(int(p["chat_id"]), int(p["user_id"]))
        if str(member.user.id) != p["user_id"]:
            raise TeleloomError(
                "peer_identity_changed", "Membership evidence belongs to another user."
            )
        return {
            "item": {
                "id": str(member.user.id),
                "user": user_info(member.user),
                "role": member.status,
                "rank": getattr(member, "custom_title", None),
                "permissions": {
                    key: value
                    for key, value in member.model_dump().items()
                    if isinstance(value, bool)
                },
            }
        }
    if kind == "participants" and p["filter"] == "admins" and not p["query"]:
        members = await adapter.bot.get_chat_administrators(int(p["chat_id"]))
        items = [
            {
                "id": str(x.user.id),
                "user": user_info(x.user),
                "role": x.status,
                "rank": getattr(x, "custom_title", None),
            }
            for x in members
        ]
        return {
            "items": items[offset : offset + p["limit"]],
            "total": len(items),
            "next_position": offset + p["limit"] if offset + p["limit"] < len(items) else None,
        }
    if kind == "chat":
        chat = await adapter.bot.get_chat(int(p["chat_id"]))
        if str(chat.id) != p["chat_id"]:
            raise TeleloomError("peer_identity_changed", "Chat metadata belongs to another peer.")
        count = await adapter.bot.get_chat_member_count(chat.id)
        return {
            "incomplete": bool(p.get("include_dialog", True)),
            "warnings": ["Bot API does not expose user dialog unread/archive/latest state."]
            if p.get("include_dialog", True)
            else [],
            "item": {
                "id": str(chat.id),
                "title": chat.title,
                "kind": chat.type,
                "username": chat.username,
                "about": chat.description,
                "participants_count": count,
                "forum": chat.is_forum,
                "photo": {"id": chat.photo.big_file_unique_id} if chat.photo else None,
                "linked_chat_id": str(chat.linked_chat_id) if chat.linked_chat_id else None,
                "permissions": chat.permissions.model_dump() if chat.permissions else None,
                "untrusted": True,
            },
        }
    raise backend_required(kind)


async def bot_account_read(adapter: Any, p: dict[str, Any], offset: int = 0) -> dict[str, Any]:
    kind = p["kind"]
    if kind == "me":
        return {"item": user_info(await adapter.bot.get_me())}
    if kind == "bot_commands":
        commands = await adapter.bot.get_my_commands()
        return {"items": [{"command": x.command, "description": x.description} for x in commands]}
    if kind == "photos":
        response = await adapter.bot.get_user_profile_photos(
            int(p["user_id"]), offset=offset, limit=p["limit"]
        )
        items = [
            {
                "id": sizes[-1].file_unique_id,
                "sizes": [
                    {
                        "width": x.width,
                        "height": x.height,
                        "bytes": x.file_size,
                        "file_id": x.file_id,
                    }
                    for x in sizes
                ],
            }
            for sizes in response.photos
        ]
        return {
            "items": items,
            "total": response.total_count,
            "next_position": offset + len(items)
            if items and offset + len(items) < response.total_count
            else None,
        }
    if kind in {"user", "bot_info"}:
        chat = await adapter.bot.get_chat(int(p["user_id"]))
        if chat.type != "private":
            raise TeleloomError("invalid_user_kind", "Select a user identity.")
        return {
            "item": {
                "id": str(chat.id),
                "first_name": chat.first_name,
                "last_name": chat.last_name,
                "username": chat.username,
                "about": chat.bio,
                "untrusted": True,
            },
            "incomplete": True,
            "warnings": [
                "Bot API omits user status, birthday and business metadata; opt into the MTProto bot backend for full metadata."
            ],
        }
    raise backend_required(kind)


def backend_required(operation: str) -> TeleloomError:
    return TeleloomError(
        "backend_required",
        "This Bot API backend omits the requested operation. Provision the opt-in MTProto bot backend with teleloom auth bot --backend mtproto.",
        details={
            "operation": operation,
            "required_backend": "mtproto",
            "platform_restriction": False,
        },
    )


async def download_avatar(
    adapter: Any,
    chat: str,
    photo_id: str | None,
    destination: Path,
    *,
    max_bytes: int,
    message_id: str | None = None,
) -> dict[str, Any]:
    from .adapters import _LimitedWriter

    adapter.profile.require_read(chat)
    if max_bytes < 1:
        raise TeleloomError("invalid_limit", "Avatar byte budget must be positive.")
    if hasattr(adapter, "client"):
        peer = await adapter._input_peer(chat)
        if adapter.profile.kind == "bot" and not isinstance(
            peer, (types.InputPeerUser, types.InputPeerSelf)
        ):
            if message_id:
                raw = await adapter.client.get_messages(peer, ids=int(message_id))
                if raw is None or str(raw.chat_id) != chat:
                    raise TeleloomError(
                        "message_unavailable", "Selected avatar service message is inaccessible."
                    )
                photos = [getattr(getattr(raw, "action", None), "photo", None)]
            else:
                response = (
                    await adapter.client(functions.channels.GetFullChannelRequest(channel=peer))
                    if isinstance(peer, types.InputPeerChannel)
                    else await adapter.client(
                        functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
                    )
                )
                photos = [response.full_chat.chat_photo]
        else:
            # ponytail: lookup at most 1000 photos; carry a photo-list offset if older selection matters.
            photos = await adapter.client.get_profile_photos(
                peer, limit=1 if photo_id is None else 1000
            )
        photo = next((x for x in photos if x and (photo_id is None or str(x.id) == photo_id)), None)
        if not photo or isinstance(photo, types.PhotoEmpty):
            raise TeleloomError(
                "photo_not_found",
                "Selected avatar was not found in the bounded lookup; MTProto bot group history requires an explicit service message_id.",
            )
        selected_id = str(photo.id)
        sizes = [
            x for x in photo.sizes if isinstance(x, (types.PhotoSize, types.PhotoSizeProgressive))
        ]
        expected = max(
            (getattr(x, "size", max(getattr(x, "sizes", [0]))) for x in sizes), default=None
        )
        if expected is not None and expected > max_bytes:
            raise TeleloomError("attachment_too_large", "Avatar exceeds the selected byte budget.")

        async def transfer(output: Any) -> None:
            await adapter.client.download_media(photo, file=output)
    else:
        if int(chat) > 0:
            photos = await adapter.bot.get_user_profile_photos(int(chat), offset=0, limit=100)
            size = next(
                (
                    row[-1]
                    for row in photos.photos
                    if photo_id is None or row[-1].file_unique_id == photo_id
                ),
                None,
            )
            if not size:
                raise TeleloomError(
                    "photo_not_found",
                    "Selected avatar was not found within the bounded Bot API lookup.",
                )
            selected_id, file_id, expected = size.file_unique_id, size.file_id, size.file_size
        else:
            entity = await adapter.bot.get_chat(int(chat))
            if not entity.photo or photo_id not in {None, entity.photo.big_file_unique_id}:
                raise TeleloomError(
                    "photo_not_found",
                    "Bot API exposes this group's current avatar only; use MTProto for history.",
                )
            selected_id, file_id, expected = (
                entity.photo.big_file_unique_id,
                entity.photo.big_file_id,
                None,
            )
        remote = await adapter.bot.get_file(file_id)
        expected = remote.file_size if remote.file_size is not None else expected
        if not remote.file_path:
            raise TeleloomError(
                "attachment_unavailable", "Telegram did not provide the selected avatar bytes."
            )
        if expected is not None and expected > max_bytes:
            raise TeleloomError("attachment_too_large", "Avatar exceeds the selected byte budget.")

        async def transfer(output: Any) -> None:
            await adapter.bot.download_file(remote.file_path, destination=output, chunk_size=65536)

    created = False
    try:
        output = _LimitedWriter(io.FileIO(destination, "x"), max_bytes)
        created = True
        with output:
            await transfer(output)
            total = output.total
        if not total or expected is not None and expected != total:
            raise TeleloomError(
                "attachment_incomplete", "Selected avatar was not downloaded completely."
            )
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return {"photo_id": selected_id, "bytes": total, "mime_type": "image/jpeg", "untrusted": True}


class Administration:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def read(
        self, profile_id: str, operation: AdministrationRead | AccountRead, *, account: bool = False
    ) -> dict[str, Any]:
        from .runtime import number

        config = self.runtime.settings.profile(profile_id)
        p = operation.model_dump(mode="json")
        for key in ("chat_id", "user_id", "message_id"):
            if p.get(key):
                id_ = number(p[key], positive=key != "chat_id")
                if key == "message_id" and id_ >= 2**31:
                    raise TeleloomError(
                        "invalid_id", "Message IDs must fit Telegram's positive 32-bit range."
                    )
        target = p.get("chat_id", p.get("user_id"))
        if target:
            config.require_read(target)
        if p["kind"] == "privacy" and "account" not in config.manage_scopes:
            raise TeleloomError(
                "management_not_allowed",
                "Owner must explicitly enable account management through CLI.",
            )
        if p["kind"] == "privacy" and config.kind != "user":
            raise TeleloomError(
                "platform_restriction", "Telegram account privacy rules require a user account."
            )
        if p["kind"] == "bot_commands" and config.kind != "bot":
            raise TeleloomError(
                "platform_restriction", "Telegram command menus belong to the authenticated bot."
            )
        if p["kind"] in {"audit", "common_chats", "message_link"} and config.kind != "user":
            raise TeleloomError(
                "platform_restriction",
                "Telegram audit, common chats and exported thread links require a user account.",
            )
        scope = [
            profile_id,
            config.generation,
            config.read_policy(),
            account,
            {k: v for k, v in p.items() if k != "cursor"},
        ]
        cursor = p.get("cursor")
        expires_at = utcnow().timestamp() + 900
        offset = 0
        if cursor:
            snapshot_id = cursor
            snapshot = self.runtime.store.state(f"administration_cursor:{snapshot_id}")
            if (
                not snapshot
                or snapshot["scope"] != scope
                or snapshot["expires_at"] <= utcnow().timestamp()
            ):
                raise TeleloomError(
                    "invalid_cursor",
                    "Metadata cursor expired or belongs to another profile/policy/query.",
                )
            offset = snapshot["position"]
            expires_at = snapshot["expires_at"]
        if offset > 10000 and p["kind"] in {"participants", "photos"}:
            raise TeleloomError(
                "read_limit", "This bounded metadata reader permits at most 10000 records."
            )
        adapter = await self.runtime.adapter(profile_id)
        result = (
            await adapter.account_read(p, offset)
            if account
            else await adapter.administration_read(p, offset)
        )
        position = result.pop("next_position", None)
        next_cursor = uuid.uuid4().hex if position else None
        result["next_cursor"] = next_cursor
        if position:
            with self.runtime.store.db:
                self.runtime.store.db.execute(
                    "DELETE FROM state WHERE key LIKE 'administration_cursor:%' AND json_extract(data,'$.expires_at')<=?",
                    (utcnow().timestamp(),),
                )
                self.runtime.store.set_state(
                    f"administration_cursor:{next_cursor}",
                    {"scope": scope, "expires_at": expires_at, "position": position},
                )
        if p["kind"] == "common_chats":
            result["items"] = [x for x in result["items"] if config.allows_read(x["id"])]
        item = result.get("item", {})
        for key in ("linked_chat_id", "personal_channel_id"):
            if item.get(key) and not config.allows_read(item[key]):
                item[key] = None
        result.update(
            profile_id=profile_id,
            source="telegram",
            untrusted=True,
            coverage={
                "mode": "bounded_live_page",
                "offset": None if p["kind"] == "common_chats" else offset,
            },
            incomplete=bool(result.get("incomplete", False) or position),
        )
        return result
