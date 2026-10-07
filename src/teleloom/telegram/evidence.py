"""Convert SDK records to original evidence; own no connections, state or delivery."""

import hashlib
from datetime import UTC
from typing import Any

from telethon import types, utils

from ..models import Chat, Message
from ..rich_reads import buttons, entities, media_evidence, reply_quote, text_evidence


def _display_name(entity: Any) -> str | None:
    if entity is None:
        return None
    return (
        getattr(entity, "title", None)
        or " ".join(
            part
            for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None))
            if part
        )
        or None
    )


def _native_chat(entity: Any) -> Chat:
    return Chat(
        id=str(utils.get_peer_id(entity)),
        title=_display_name(entity) or str(utils.get_peer_id(entity)),
        username=getattr(entity, "username", None),
        kind="group"
        if isinstance(entity, (types.Chat, types.ChatForbidden))
        or getattr(entity, "megagroup", False)
        else "channel"
        if isinstance(entity, (types.Channel, types.ChannelForbidden))
        else "private",
        is_contact=bool(getattr(entity, "contact", False))
        if isinstance(entity, types.User)
        else None,
        is_bot=bool(getattr(entity, "bot", False)) if isinstance(entity, types.User) else None,
        forum=bool(getattr(entity, "forum", False)),
    )


def _forwarded_source(raw: Any, forward: Any = None) -> dict[str, Any] | None:
    if raw is None:
        return None
    peer = getattr(raw, "from_id", None)
    date = getattr(raw, "date", None)
    source = getattr(forward, "sender", None) or getattr(forward, "chat", None)
    return {
        "sender_id": str(utils.get_peer_id(peer)) if peer else None,
        "sender_name": getattr(raw, "from_name", None) or _display_name(source),
        "sender_username": getattr(source, "username", None),
        "author_signature": getattr(raw, "post_author", None),
        "message_id": str(raw.channel_post) if getattr(raw, "channel_post", None) else None,
        "date": date.astimezone(UTC).isoformat() if date else None,
    }


def _reaction_counts(raw: Any) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    result = []
    for item in raw.results:
        reaction = item.reaction
        if isinstance(reaction, types.ReactionEmoji):
            result.append({"type": "emoji", "emoji": reaction.emoticon, "count": item.count})
        elif isinstance(reaction, types.ReactionCustomEmoji):
            result.append(
                {
                    "type": "custom_emoji",
                    "custom_emoji_id": str(reaction.document_id),
                    "count": item.count,
                }
            )
        elif isinstance(reaction, types.ReactionPaid):
            result.append({"type": "paid", "count": item.count})
    return result


def telethon_message(
    profile: str, raw: Any, *, sender: Any = None, chat_entity: Any = None
) -> Message:
    chat = str(raw.chat_id)
    peer = getattr(raw, "peer_id", None)
    channel_id = getattr(peer, "channel_id", None)
    link = f"https://t.me/c/{channel_id}/{raw.id}" if channel_id else None
    reply = getattr(raw, "reply_to", None)
    reply_peer = getattr(reply, "reply_to_peer_id", None)
    reply_id = getattr(reply, "reply_to_msg_id", None)
    thread_root = getattr(reply, "reply_to_top_id", None) or reply_id
    is_forum_topic = bool(getattr(reply, "forum_topic", False))
    if isinstance(getattr(raw, "action", None), types.MessageActionTopicCreate):
        thread_root = raw.id
        is_forum_topic = True
    forum = bool(getattr(chat_entity or getattr(raw, "chat", None), "forum", False))
    sender = sender or getattr(raw, "sender", None)
    media, preview = media_evidence(raw)
    document = getattr(raw.media, "document", None)
    if media is not None and document is not None:
        media["source_version"] = hashlib.sha256(str(document.id).encode()).hexdigest()
    return Message(
        profile_id=profile,
        chat_id=chat,
        id=str(raw.id),
        date=raw.date.astimezone(UTC),
        **text_evidence(
            raw.message or "", entities(raw.entities), getattr(raw, "rich_message", None)
        ),
        sender_id=str(raw.sender_id) if raw.sender_id else None,
        reply_to_message_id=str(reply_id) if reply_id else None,
        reply_to_chat_id=str(utils.get_peer_id(reply_peer)) if reply_peer else None,
        reply_quote=reply_quote(reply),
        buttons=buttons(getattr(raw, "reply_markup", None)),
        topic_id=str(thread_root) if is_forum_topic and thread_root else "1" if forum else None,
        thread_root_id=str(thread_root) if thread_root else None,
        sender_name=_display_name(sender),
        sender_username=getattr(sender, "username", None),
        author_signature=getattr(raw, "post_author", None),
        forwarded_from=_forwarded_source(
            getattr(raw, "fwd_from", None), getattr(raw, "forward", None)
        ),
        web_preview=preview,
        grouped_id=str(raw.grouped_id) if getattr(raw, "grouped_id", None) else None,
        kind="service"
        if isinstance(raw, types.MessageService) or getattr(raw, "action", None)
        else "message",
        views=getattr(raw, "views", None),
        reactions=_reaction_counts(getattr(raw, "reactions", None)),
        reply_count=raw.replies.replies if getattr(raw, "replies", None) else None,
        pinned=getattr(raw, "pinned", None),
        entities=entities(raw.entities),
        media=media,
        edited_at=raw.edit_date,
        outgoing=bool(raw.out),
        link=link,
    )


def bot_message(profile: str, raw: Any) -> Message:
    media = None
    for kind in (
        "photo",
        "document",
        "video",
        "voice",
        "audio",
        "sticker",
        "video_note",
        "animation",
    ):
        value = getattr(raw, kind, None)
        if value:
            selected = value[-1] if isinstance(value, list) else value
            media = {
                "type": kind,
                "file_id": selected.file_id,
                "size": selected.file_size,
                "name": getattr(selected, "file_name", None),
                "mime_type": getattr(selected, "mime_type", None)
                or ("image/jpeg" if kind == "photo" else None),
                **{
                    key: getattr(selected, key)
                    for key in (
                        "duration",
                        "width",
                        "height",
                        "emoji",
                        "file_unique_id",
                        "is_animated",
                        "is_video",
                    )
                    if getattr(selected, key, None) is not None
                },
            }
            break
    sender = getattr(raw, "sender_chat", None) or raw.from_user
    origin = getattr(raw, "forward_origin", None)
    forwarded = None
    if origin:
        source = (
            getattr(origin, "sender_user", None)
            or getattr(origin, "sender_chat", None)
            or getattr(origin, "chat", None)
        )
        forwarded = {
            "type": origin.type,
            "sender_id": str(source.id) if source else None,
            "sender_name": _display_name(source) or getattr(origin, "sender_user_name", None),
            "sender_username": getattr(source, "username", None),
            "author_signature": getattr(origin, "author_signature", None),
            "message_id": str(origin.message_id) if getattr(origin, "message_id", None) else None,
            "date": origin.date.astimezone(UTC).isoformat(),
            "automatic": bool(getattr(raw, "is_automatic_forward", False)),
        }
    topic = raw.message_thread_id if getattr(raw, "is_topic_message", False) else None
    if topic is None and getattr(raw.chat, "is_forum", False):
        topic = 1
    root = topic or (raw.reply_to_message.message_id if raw.reply_to_message else None)
    external = raw.external_reply
    external_chat = external.chat or getattr(external.origin, "chat", None) if external else None
    external_id = (
        external.message_id or getattr(external.origin, "message_id", None) if external else None
    )
    service = any(
        getattr(raw, key, None)
        for key in (
            "new_chat_members",
            "left_chat_member",
            "new_chat_title",
            "new_chat_photo",
            "delete_chat_photo",
            "group_chat_created",
            "supergroup_chat_created",
            "channel_chat_created",
            "message_auto_delete_timer_changed",
            "migrate_to_chat_id",
            "migrate_from_chat_id",
            "pinned_message",
            "forum_topic_created",
            "forum_topic_edited",
            "forum_topic_closed",
            "forum_topic_reopened",
            "general_forum_topic_hidden",
            "general_forum_topic_unhidden",
            "video_chat_started",
            "video_chat_ended",
            "video_chat_scheduled",
            "video_chat_participants_invited",
            "successful_payment",
            "refunded_payment",
            "users_shared",
            "chat_shared",
            "write_access_allowed",
            "connected_website",
            "boost_added",
            "chat_owner_left",
            "chat_owner_changed",
            "gift",
            "unique_gift",
            "gift_upgrade_sent",
            "proximity_alert_triggered",
            "chat_background_set",
            "checklist_tasks_done",
            "checklist_tasks_added",
            "direct_message_price_changed",
            "giveaway_created",
            "giveaway_completed",
            "managed_bot_created",
            "paid_message_price_changed",
            "poll_option_added",
            "poll_option_deleted",
            "suggested_post_approved",
            "suggested_post_approval_failed",
            "suggested_post_declined",
            "suggested_post_paid",
            "suggested_post_refunded",
            "web_app_data",
            "community_chat_added",
            "community_chat_removed",
            "community_chat_joined",
            "user_shared",
        )
    )
    return Message(
        profile_id=profile,
        chat_id=str(raw.chat.id),
        id=str(raw.message_id),
        date=raw.date.astimezone(UTC),
        **text_evidence(
            raw.text or raw.caption or "", entities(raw.entities or raw.caption_entities)
        ),
        sender_id=str(sender.id) if sender else None,
        sender_name=_display_name(sender),
        sender_username=getattr(sender, "username", None),
        author_signature=getattr(raw, "author_signature", None),
        forwarded_from=forwarded,
        grouped_id=getattr(raw, "media_group_id", None),
        kind="service" if service else "message",
        reply_to_message_id=str(external_id)
        if external_id is not None
        else str(raw.reply_to_message.message_id)
        if raw.reply_to_message
        else None,
        reply_quote=reply_quote(getattr(raw, "quote", None), bot=True),
        reply_to_chat_id=str(external_chat.id) if external_chat else None,
        reply_external=external is not None,
        buttons=buttons(getattr(raw, "reply_markup", None), bot=True),
        topic_id=str(topic) if topic else None,
        thread_root_id=str(root) if root else None,
        entities=entities(raw.entities or raw.caption_entities),
        media=media,
        edited_at=raw.edit_date,
        outgoing=bool(raw.from_user and raw.from_user.is_bot),
        link=(
            f"https://t.me/{raw.chat.username}/{raw.message_id}"
            if raw.chat.username
            else f"https://t.me/c/{str(raw.chat.id).removeprefix('-100')}/{raw.message_id}"
            if raw.chat.type in {"channel", "supergroup"}
            else None
        ),
    )
