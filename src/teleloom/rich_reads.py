"""Safe original Telegram text evidence and an explicitly reconstructed block view."""

from datetime import datetime
from typing import Any

from telethon import utils

from .config import Profile
from .models import TeleloomError, iso


def scoped_evidence(profile: Profile, profile_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Withhold dereferenced private context at presentation; stored originals stay intact."""
    redactions = 0

    def peer_metadata(value: Any) -> Any:
        nonlocal redactions
        if isinstance(value, list):
            return [peer_metadata(item) for item in value]
        if not isinstance(value, dict):
            return value
        kind = value.get("_")
        if (
            kind in {"Channel", "Chat", "User", "ChannelForbidden", "ChatForbidden"}
            and value.get("id")
            and not profile.allows_read(value["id"])
        ):
            redactions += 1
            return {"_": kind, "redacted": True}
        if (
            kind == "TextMentionName"
            and value.get("user_id")
            and not profile.allows_read(value["user_id"])
        ):
            redactions += 1
            return {key: peer_metadata(item) for key, item in value.items() if key != "user_id"}
        return {key: peer_metadata(item) for key, item in value.items()}

    def visit(value: Any) -> Any:
        nonlocal redactions
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        row = {key: visit(item) for key, item in value.items()}
        if {"profile_id", "chat_id", "date", "text"} <= row.keys() and (
            "id" in row or row.get("kind") == "draft"
        ):
            if row["profile_id"] != profile_id:
                raise TeleloomError("source_changed", "A message belongs to another profile.")
            profile.require_read(row["chat_id"])
            if profile.read_mode == "selected":
                reply_peer = row.get("reply_to_chat_id")
                if (reply_peer and not profile.allows_read(reply_peer)) or (
                    row.get("reply_external") and not reply_peer
                ):
                    row.update(reply_to_chat_id=None, reply_to_message_id=None, reply_quote=None)
                    redactions += 1
                forwarded = row.get("forwarded_from")
                if (
                    forwarded
                    and forwarded.get("sender_id")
                    and not profile.allows_read(forwarded["sender_id"])
                ):
                    row["forwarded_from"] = {"redacted": True}
                    redactions += 1
                if rich := row.get("rich_text"):
                    rich["blocks"] = peer_metadata(rich["blocks"])
                    paragraphs = ["\n".join(_lines(block)) for block in rich["blocks"]]
                    rich["reconstructed_text"] = "\n\n".join(text for text in paragraphs if text)
                    if row.get("text_source") == "reconstructed":
                        row["text"] = rich["reconstructed_text"]
        return row

    result: dict[str, Any] = visit(data)
    if redactions:
        result["coverage"] = {**result.get("coverage", {}), "read_policy_redactions": redactions}
        result["warnings"] = [
            *result.get("warnings", []),
            "Context metadata outside the owner's read scope was withheld; stored originals remain intact.",
        ]
    return result


# Only content/format fields: embedded SDK peers and files can contain credentials
# and access hashes. Keep their public identity/metadata, never serialize the SDK.
BLOCK_FIELDS = {
    "text",
    "texts",
    "alt",
    "source",
    "old_text",
    "title",
    "subtitle",
    "author",
    "caption",
    "credit",
    "items",
    "blocks",
    "rows",
    "cells",
    "articles",
    "cover",
    "buttons",
    "type",
    "url",
    "email",
    "phone",
    "name",
    "language",
    "num",
    "start",
    "reversed",
    "collapsed",
    "bordered",
    "striped",
    "compact",
    "header",
    "colspan",
    "rowspan",
    "align_center",
    "align_right",
    "align_left",
    "valign_middle",
    "valign_bottom",
    "ordered",
    "open",
    "rtl",
    "relative",
    "short_time",
    "long_time",
    "short_date",
    "long_date",
    "day_of_week",
    "date",
    "published_date",
    "w",
    "h",
    "autoplay",
    "loop",
    "spoiler",
    "full_width",
    "allow_scrolling",
    "document_id",
    "photo_id",
    "video_id",
    "audio_id",
    "webpage_id",
    "user_id",
    "channel",
    "style",
    "bg_primary",
    "bg_danger",
    "bg_success",
    "link",
    "checkbox",
    "checked",
    "value",
    "copy_text",
    "query",
    "same_peer",
    "requires_password",
    "button_id",
    "fwd_text",
    "html",
    "poster_photo_id",
    "author_photo_id",
    "description",
    "geo",
    "zoom",
}
ID_FIELDS = {
    "document_id",
    "photo_id",
    "video_id",
    "audio_id",
    "webpage_id",
    "user_id",
    "poster_photo_id",
    "author_photo_id",
}
TEXT_FIELDS = (
    "title",
    "subtitle",
    "author",
    "text",
    "caption",
    "credit",
    "items",
    "blocks",
    "rows",
    "articles",
    "cover",
    "source",
    "buttons",
)


def entities(raw: Any) -> list[dict[str, Any]]:
    result = []
    for entity in raw or []:
        value = (
            entity.to_dict()
            if hasattr(entity, "to_dict")
            else entity.model_dump(mode="json", exclude_none=True)
        )
        item = {
            key: iso(val) if isinstance(val, datetime) else val
            for key, val in value.items()
            if key
            in {
                "_",
                "type",
                "offset",
                "length",
                "url",
                "language",
                "collapsed",
                "date",
                "relative",
                "short_time",
                "long_time",
                "short_date",
                "long_date",
                "day_of_week",
                "unix_time",
                "date_time_format",
            }
            and val is not None
        }
        for key in ("document_id", "user_id", "custom_emoji_id"):
            val = getattr(entity, key, None)
            if val is not None:
                item[key] = str(val if isinstance(val, (int, str)) else utils.get_peer_id(val))
        if user := getattr(entity, "user", None):
            item["user"] = {
                "id": str(user.id),
                "first_name": user.first_name,
                "last_name": getattr(user, "last_name", None),
                "username": getattr(user, "username", None),
            }
        result.append(item)
    return result


def _block(node: Any, warnings: list[str], depth: int = 0) -> Any:
    if node is None or isinstance(node, (str, bool, int, float)):
        return node
    if isinstance(node, datetime):
        return iso(node)
    if depth > 64:
        warnings.append("A block exceeded the nesting budget; original text remains available.")
        return {"_": type(node).__name__, "unavailable": "nesting_limit"}
    if isinstance(node, (list, tuple)):
        return [_block(item, warnings, depth + 1) for item in node]
    kind = type(node).__name__
    if kind in {"Channel", "Chat", "User", "ChannelForbidden", "ChatForbidden"}:
        return {
            "_": kind,
            "id": str(utils.get_peer_id(node)),
            "title": getattr(node, "title", None),
            "username": getattr(node, "username", None),
        }
    if kind == "GeoPoint":
        return {
            "_": kind,
            "longitude": node.long,
            "latitude": node.lat,
            "accuracy_radius": getattr(node, "accuracy_radius", None),
        }
    if not kind.startswith(("Text", "Page", "InlineButton", "RichButton")):
        warnings.append(f"Unsupported rich node {kind}; its type is retained.")
        return {"_": kind}
    return {
        "_": kind,
        **{
            key: str(value) if key in ID_FIELDS else _block(value, warnings, depth + 1)
            for key, value in vars(node).items()
            if key in BLOCK_FIELDS and value is not None
        },
    }


def _text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return "".join(_text(part) for part in node) if isinstance(node, list) else ""
    for field in ("texts", "text", "alt", "source"):
        if field in node:
            return _text(node[field])
    return ""


def _lines(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node] if node else []
    if isinstance(node, list):
        return [line for part in node for line in _lines(part)]
    if not isinstance(node, dict):
        return []
    if node.get("_", "").startswith("Text"):
        text = _text(node)
        return [text] if text else []
    if "cells" in node:
        return [" | ".join(line for cell in node["cells"] for line in _lines(cell))]
    return [line for field in TEXT_FIELDS for line in _lines(node.get(field))]


def _emoji_nodes(node: Any) -> list[dict[str, str]]:
    if isinstance(node, list):
        return [item for child in node for item in _emoji_nodes(child)]
    if not isinstance(node, dict):
        return []
    if node.get("_") == "TextCustomEmoji":
        return [{"id": node["document_id"], "emoji": node["alt"]}]
    return [item for child in node.values() for item in _emoji_nodes(child)]


def text_evidence(
    original: str, raw_entities: list[dict[str, Any]], rich: Any = None
) -> dict[str, Any]:
    warnings: list[str] = []
    blocks = _block(rich.blocks, warnings) if rich is not None else None
    emojis = _emoji_nodes(blocks)
    encoded = original.encode("utf-16-le")
    for entity in raw_entities:
        if entity.get("_") != "MessageEntityCustomEmoji" and entity.get("type") != "custom_emoji":
            continue
        start, length = entity.get("offset", -1), entity.get("length", -1)
        try:
            if start < 0 or length <= 0 or (start + length) * 2 > len(encoded):
                raise ValueError
            alt = encoded[start * 2 : (start + length) * 2].decode("utf-16-le")
        except (ValueError, UnicodeError):
            warnings.append(
                "A custom emoji has an invalid UTF-16 span; the original entity is retained."
            )
            continue
        emojis.append(
            {"id": str(entity.get("document_id", entity.get("custom_emoji_id"))), "emoji": alt}
        )
    result: dict[str, Any] = {
        "text": original,
        "custom_emojis": list({item["id"]: item for item in emojis}.values()),
    }
    if blocks is not None:
        paragraphs = ["\n".join(_lines(block)) for block in blocks]
        reconstructed = "\n\n".join(text for text in paragraphs if text)
        result["rich_text"] = {
            "blocks": blocks,
            "reconstructed_text": reconstructed,
            "warnings": list(dict.fromkeys(warnings)),
            "partial": bool(getattr(rich, "part", False)),
            "rtl": bool(getattr(rich, "rtl", False)),
            "photos": [media_file(photo) for photo in getattr(rich, "photos", [])],
            "documents": [media_file(document) for document in getattr(rich, "documents", [])],
        }
        if not original:
            result.update(text=reconstructed, text_source="reconstructed", original_text=original)
    return result


def media_file(raw: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"id": str(raw.id), "type": type(raw).__name__}
    for key in ("size", "mime_type", "date"):
        value = getattr(raw, key, None)
        if value is not None:
            result[key] = iso(value) if isinstance(value, datetime) else value
    for attr in getattr(raw, "attributes", []):
        for key in (
            "file_name",
            "duration",
            "w",
            "h",
            "alt",
            "voice",
            "round_message",
            "title",
            "performer",
            "animated",
        ):
            value = getattr(attr, key, None)
            if value is not None:
                result[key] = value
    sizes = [
        {key: getattr(size, key) for key in ("type", "w", "h", "size") if hasattr(size, key)}
        for size in getattr(raw, "sizes", [])
    ]
    if sizes:
        result["sizes"] = sizes
    return result


def media_evidence(raw: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    media = getattr(raw, "media", None)
    if media is None:
        return None, None
    result: dict[str, Any] = {"type": type(media).__name__}
    preview = getattr(raw, "web_preview", None)
    if preview is not None:
        return {**result, "kind": "web_preview"}, {
            key: value
            for key in (
                "url",
                "display_url",
                "site_name",
                "title",
                "description",
                "author",
                "duration",
            )
            if (value := getattr(preview, key, None)) is not None
        }
    result["kind"] = next(
        (
            kind
            for kind in (
                "sticker",
                "voice",
                "video_note",
                "video",
                "audio",
                "gif",
                "photo",
                "document",
                "poll",
                "dice",
                "game",
                "contact",
                "geo",
            )
            if getattr(raw, kind, None) is not None
        ),
        "media",
    )
    file = getattr(raw, "file", None)
    if file:
        result.update(
            {
                key: getattr(file, key, None)
                for key in (
                    "name",
                    "size",
                    "mime_type",
                    "duration",
                    "width",
                    "height",
                    "emoji",
                    "title",
                    "performer",
                )
            }
        )
    for key in ("spoiler", "ttl_seconds"):
        if (value := getattr(media, key, None)) is not None:
            result[key] = value
    poll = getattr(media, "poll", None)
    if poll:
        result["poll"] = {
            "id": str(poll.id),
            "question": poll.question.text,
            "question_entities": entities(poll.question.entities),
            "answers": [
                {
                    "text": answer.text.text,
                    "entities": entities(answer.text.entities),
                    "option": answer.option.hex(),
                }
                for answer in poll.answers
            ],
            **{
                key: getattr(poll, key, None)
                for key in ("closed", "public_voters", "multiple_choice", "quiz", "close_period")
            },
        }
    return result, None


def reply_quote(raw: Any, *, bot: bool = False) -> dict[str, Any] | None:
    if raw is None:
        return None
    text = getattr(raw, "text" if bot else "quote_text", None)
    if text is None:
        return None
    return {
        "text": text,
        "offset": getattr(raw, "position" if bot else "quote_offset", None),
        "offset_unit": "utf16",
        "manual": getattr(raw, "is_manual", None) if bot else None,
        "selected": bool(getattr(raw, "is_manual" if bot else "quote", False)),
        "entities": entities(getattr(raw, "entities" if bot else "quote_entities", None)),
    }


def buttons(markup: Any, *, bot: bool = False) -> list[list[dict[str, Any]]] | None:
    rows = getattr(markup, "inline_keyboard" if bot else "rows", None)
    if rows is None:
        return None
    result = []
    for row in rows:
        normalized = []
        for item in row if bot else row.buttons:
            detail = item if bot else getattr(item, "type", item)
            url = getattr(detail, "url", None)
            callback = getattr(detail, "callback_data" if bot else "data", None)
            normalized.append(
                {
                    "text": item.text,
                    "type": "callback"
                    if callback is not None
                    else "url"
                    if url
                    else type(detail).__name__,
                    **({"url": url} if url else {}),
                }
            )
        result.append(normalized)
    return result
