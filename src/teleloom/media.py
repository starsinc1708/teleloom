"""Confirmed media transfers, opaque provider handles and explicit photo inspection."""

import asyncio
import base64
import hashlib
import io
import math
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from telethon import functions, types

from .adapters import BotAdapter, UserAdapter
from .config import private_dir
from .file_snapshots import FileSnapshots, plain_path, read_verified, write_new_verified
from .media_operations import (
    MediaOperation,
    bot_media_write,
    uploaded_file,
    user_gifs,
    user_media_write,
    user_message_photos,
)
from .models import TeleloomError, iso, utcnow

if TYPE_CHECKING:
    from .runtime import Runtime


def image(content: bytes, *, edge: int = 2048) -> tuple[bytes, dict[str, Any]]:
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(content)) as source:
            if source.width * source.height > 16_000_000:
                raise TeleloomError(
                    "image_too_large", "Photo inspection permits at most 16 million pixels."
                )
            metadata: dict[str, Any] = {
                "width": source.width,
                "height": source.height,
                "format": source.format,
                "animated": bool(getattr(source, "is_animated", False)),
            }
            rendered = ImageOps.exif_transpose(source).convert("RGB")
            rendered.thumbnail((edge, edge))
            metadata["rendered"] = {
                "width": rendered.width,
                "height": rendered.height,
                "mime_type": "image/jpeg",
                "frame": 0,
            }
            output = io.BytesIO()
            rendered.save(output, "JPEG", quality=85)
        return output.getvalue(), metadata
    except (OSError, ValueError, Image.DecompressionBombError):
        raise TeleloomError(
            "invalid_image", "The selected bytes are not a supported image."
        ) from None


def voice_duration(content: bytes) -> int:
    position, granule = 0, 0
    head = content.find(b"OpusHead")
    if head < 0 or head + 19 > len(content):
        raise TeleloomError("invalid_voice", "Select an Ogg container with an Opus voice stream.")
    while position < len(content):
        if content[position : position + 4] != b"OggS" or position + 27 > len(content):
            raise TeleloomError("invalid_voice", "The selected Ogg page is truncated or invalid.")
        segments = content[position + 26]
        header_end = position + 27 + segments
        end = header_end + sum(content[position + 27 : header_end])
        if header_end > len(content) or end > len(content):
            raise TeleloomError("invalid_voice", "The selected Ogg page is incomplete.")
        timestamp = int.from_bytes(content[position + 6 : position + 14], "little")
        if timestamp != 2**64 - 1:
            granule = max(granule, timestamp)
        position = end
    skip = int.from_bytes(content[head + 10 : head + 12], "little")
    if granule <= skip:
        raise TeleloomError(
            "invalid_voice", "Select a nonempty Opus voice stream with an observed duration."
        )
    return math.ceil(max(0, granule - skip) / 48000)


class MediaManager:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        self.settings, self.store = runtime.settings, runtime.store
        self.files = FileSnapshots(self.settings, self.store)
        self.download_lock = asyncio.Lock()

    @asynccontextmanager
    async def _download_path(self, max_bytes: int) -> AsyncIterator[Path]:
        # ponytail: serialize private downloads; reserve bytes if parallel transfer throughput matters.
        async with self.download_lock:
            root = self.settings.data_dir / "media-downloads"
            private_dir(root)
            plain_path(root, directory=True)
            if (
                sum(plain_path(child).stat().st_size for child in root.iterdir()) + max_bytes
                > 500_000_000
            ):
                raise TeleloomError(
                    "file_disk_limit", "The private download disk budget is exhausted."
                )
            yield root / (uuid.uuid4().hex + ".bin")

    def allowed(
        self, profile: str, operation: dict[str, Any], targets: list[dict[str, Any]] | None = None
    ) -> None:
        config = self.settings.profile(profile)
        kind = operation["kind"].removeprefix("media_")
        if operation.get("owner_authorized") and not kind.startswith("send_"):
            raise TeleloomError(
                "unsupported_authorization",
                "Owner instruction authorization supports media sending only.",
            )
        if config.kind == "bot" and (operation.get("schedule_date") or operation.get("gif_handle")):
            raise TeleloomError(
                "unsupported_capability",
                "Telegram scheduled media and inline GIF results require a user account.",
            )
        if (
            config.kind == "bot"
            and getattr(config, "bot_backend", "bot_api") == "bot_api"
            and (kind == "upload_file" or operation.get("upload_handle"))
        ):
            raise TeleloomError(
                "backend_required",
                "Bare uploads and reusable upload handles require the owner-configured MTProto backend.",
            )
        if (
            kind != "upload_file"
            and not operation.get("owner_authorized")
            and operation["chat_id"] not in config.send_chats
        ):
            raise TeleloomError(
                "recipient_not_allowed",
                "The owner has not allowed media delivery to this exact chat.",
            )
        if kind != "upload_file":
            config.require_read(operation["chat_id"])

    def _handle(self, profile: str, handle: str, kind: str) -> dict[str, Any]:
        record = self.store.state("media_handle:" + handle)
        if not record or record["profile_id"] != profile or record["kind"] != kind:
            raise TeleloomError(
                "media_handle_not_found",
                "The selected media handle is unavailable to this profile.",
            )
        if record["generation"] != self.settings.profile(profile).generation:
            raise TeleloomError(
                "account_changed", "The media handle belongs to a previous account."
            )
        if utcnow() >= datetime.fromisoformat(record["expires_at"]):
            raise TeleloomError(
                "media_handle_expired",
                "Search or upload the media again and confirm a new preview.",
            )
        if kind == "upload":
            parent = plain_path(Path(record["file"]["source_path"]).parent, directory=True)
            if not any(
                parent.is_relative_to(plain_path(root, directory=True))
                for root in self.settings.profile(profile).file_roots
            ):
                raise TeleloomError(
                    "file_not_allowed", "The owner revoked the upload handle's file root."
                )
        return cast(dict[str, Any], record)

    def _new_handle(
        self, profile: str, kind: str, value: dict[str, Any], seconds: int = 900
    ) -> dict[str, Any]:
        handle = uuid.uuid4().hex
        expiry = iso(utcnow() + timedelta(seconds=min(900, max(1, seconds))))
        self.store.set_state(
            "media_handle:" + handle,
            {
                "profile_id": profile,
                "generation": self.settings.profile(profile).generation,
                "kind": kind,
                "expires_at": expiry,
                **value,
            },
        )
        return {"handle": handle, "expires_at": expiry}

    def validate_sources(
        self, profile: str, operation: dict[str, Any], sources: list[dict[str, Any]]
    ) -> list[bytes]:
        self.allowed(profile, operation)
        if sources != operation["files"]:
            raise TeleloomError(
                "source_changed", "The media source descriptors differ from the confirmed preview."
            )
        if operation.get("gif_handle"):
            self._handle(profile, operation["gif_handle"], "gif")
            return []
        if operation.get("upload_handle"):
            self._handle(profile, operation["upload_handle"], "upload")
            return []
        return [
            self.files.validate(
                profile, file, owner_authorized=operation.get("owner_authorized", False)
            )
            for file in sources
        ]

    async def preview(
        self, profile: str, operation: MediaOperation, owner_authorized: bool = False
    ) -> dict[str, Any]:
        from .runtime import number

        p = operation.model_dump(mode="json")
        p["kind"] = "media_" + p["kind"]
        if owner_authorized:
            p["owner_authorized"] = True
        self.allowed(profile, p)
        kind = p["kind"].removeprefix("media_")
        caption = p.get("caption", "")
        if len(caption.encode("utf-16-le")) // 2 > 1024:
            raise TeleloomError("invalid_caption", "Captions must fit 1024 UTF-16 units.")
        if kind in {"send_voice", "send_sticker"} and caption:
            raise TeleloomError("invalid_caption", "Voice/sticker previews use no caption.")
        for field in ("chat_id", "reply_to_message_id", "topic_id"):
            if p.get(field):
                number(p[field], positive=field != "chat_id")
        if p.get("schedule_date"):
            schedule = datetime.fromisoformat(p["schedule_date"])
            iso(schedule)
            if not utcnow() < schedule <= utcnow() + timedelta(days=365):
                raise TeleloomError(
                    "invalid_schedule", "Choose a timezone-aware future time within 365 days."
                )
        paths = p.pop("source_paths", None)
        source = p.pop("source_path", None)
        if paths is None:
            paths = [source] if source else []
        alternatives = sum(
            bool(value) for value in (paths, p.get("upload_handle"), p.get("gif_handle"))
        )
        if alternatives != 1:
            raise TeleloomError(
                "invalid_media_source",
                "Select exactly one local source, upload handle or GIF handle.",
            )
        files: list[dict[str, Any]] = []
        captured: list[str] = []
        try:
            for path in paths:
                descriptor = self.files.capture(profile, path, owner_authorized=owner_authorized)
                captured.append(descriptor["snapshot_id"])
                files.append(descriptor)
                content = self.files.validate(
                    profile, descriptor, owner_authorized=owner_authorized
                )
                if kind == "send_sticker":
                    _, metadata = image(content)
                    if (
                        metadata["format"] != "WEBP"
                        or metadata["animated"]
                        or len(content) > 512_000
                        or max(metadata["width"], metadata["height"]) != 512
                    ):
                        raise TeleloomError(
                            "invalid_sticker",
                            "Select a static WebP sticker with one 512-pixel edge, at most 512000 bytes.",
                        )
                    descriptor.update(
                        width=metadata["width"], height=metadata["height"], mime_type="image/webp"
                    )
                elif kind == "send_voice":
                    descriptor.update(duration=voice_duration(content), mime_type="audio/ogg")
                elif kind == "send_gif":
                    if not content.startswith((b"GIF87a", b"GIF89a")) and content[4:8] != b"ftyp":
                        raise TeleloomError(
                            "invalid_gif", "Select an actual GIF or MPEG4 animation file."
                        )
                    if content.startswith((b"GIF87a", b"GIF89a")):
                        _, metadata = image(content)
                        descriptor.update(width=metadata["width"], height=metadata["height"])
                elif kind == "send_album" or not p.get("as_document", True):
                    if descriptor["mime_type"] in {"image/jpeg", "image/png"}:
                        _, metadata = image(content)
                        descriptor.update(width=metadata["width"], height=metadata["height"])
            if sum(file["size"] for file in files) > 100_000_000:
                raise TeleloomError(
                    "file_too_large", "Albums permit at most 100000000 bytes in total."
                )
            if kind == "send_album":
                categories = {
                    "visual"
                    if file["mime_type"] in {"image/png", "image/jpeg"}
                    or file["mime_type"].startswith("video/")
                    else "document"
                    for file in files
                }
                if len(categories) != 1:
                    raise TeleloomError(
                        "invalid_album",
                        "An album must contain only photos/videos, or only documents.",
                    )
            if p.get("upload_handle"):
                record = self._handle(profile, p["upload_handle"], "upload")
                files = [record["file"]]
            if p.get("gif_handle"):
                record = self._handle(profile, p["gif_handle"], "gif")
                if caption:
                    raise TeleloomError(
                        "invalid_caption",
                        "A provider GIF handle retains its original inline result without a custom caption.",
                    )
            p["files"] = files
            resolved = []
            targets = [{"kind": "account"}]
            if kind != "upload_file":
                adapter = await self.runtime.adapter(profile)
                chat = await adapter.resolve(p["chat_id"])
                if chat.id != p["chat_id"]:
                    raise TeleloomError(
                        "recipient_changed", "The resolved media target changed identity."
                    )
                resolved = [chat.model_dump(mode="json")]
                targets = [{"kind": "chat", "chat_id": p["chat_id"]}]
                if p.get("reply_to_message_id") or p.get("topic_id"):
                    self.settings.profile(profile).require_read(p["chat_id"])
                    ids = list(
                        {int(p[key]) for key in ("reply_to_message_id", "topic_id") if p.get(key)}
                    )
                    rows = await adapter.history(
                        p["chat_id"], before=None, since=None, until=None, limit=len(ids), ids=ids
                    )
                    if set(ids) != {int(row.id) for row in rows}:
                        raise TeleloomError(
                            "message_not_found",
                            "The selected media reply/topic target is unavailable.",
                        )
                    if p.get("topic_id") and not chat.forum:
                        raise TeleloomError(
                            "invalid_topic", "Select a forum chat for a media topic target."
                        )
            return self.runtime.jobs.confirmed_preview(
                profile, p, targets=targets, sources=files, resolved=resolved
            )
        except BaseException:
            for snapshot in captured:
                self.files.discard(snapshot)
            raise

    async def perform(
        self,
        profile: str,
        operation: dict[str, Any],
        delivery: dict[str, Any],
        validated_bytes: list[bytes],
    ) -> dict[str, Any]:
        adapter = await self.runtime.adapter(profile)
        if hasattr(adapter, "client"):
            handles = []
            if operation.get("gif_handle"):
                handles = [self._handle(profile, operation["gif_handle"], "gif")]
            elif operation.get("upload_handle"):
                handles = [
                    uploaded_file(
                        self._handle(profile, operation["upload_handle"], "upload")["uploaded"]
                    )
                ]
            result = await user_media_write(
                cast(UserAdapter, adapter),
                operation,
                delivery["random_id"],
                validated_bytes,
                handles,
            )
        else:
            result = await bot_media_write(cast(BotAdapter, adapter), operation, validated_bytes)
        if uploaded := result.pop("uploaded", None):
            result["upload"] = {
                **self._new_handle(
                    profile, "upload", {"uploaded": uploaded, "file": operation["files"][0]}
                ),
                "file": operation["files"][0],
            }
        return result

    async def info(self, profile: str, chat: str, message_id: str) -> dict[str, Any]:
        from .runtime import number

        self.settings.profile(profile).require_read(chat)
        number(chat)
        number(message_id, positive=True)
        adapter = await self.runtime.adapter(profile)
        rows = await adapter.history(
            chat, before=None, since=None, until=None, limit=1, ids=[int(message_id)]
        )
        if not rows or not rows[0].media:
            raise TeleloomError(
                "media_unavailable", "The selected message has no observed attachment."
            )
        return {
            "item": rows[0].model_dump(mode="json"),
            "source": "telegram"
            if self.settings.profile(profile).kind == "user"
            or self.settings.profile(profile).bot_backend == "mtproto"
            else "bot_updates",
        }

    async def download(
        self,
        profile: str,
        chat: str,
        message_id: str,
        max_bytes: int,
        destination: str | None = None,
    ) -> dict[str, Any]:
        if destination:
            self._destination(profile, destination)
        await self.info(profile, chat, message_id)
        adapter = await self.runtime.adapter(profile)
        async with self._download_path(max_bytes) as temporary:
            retained = False
            try:
                metadata = await adapter.download_attachment(
                    chat, int(message_id), temporary, max_bytes=max_bytes
                )
                content = read_verified(temporary, max_bytes)
                path = temporary
                if destination:
                    path = self._save(profile, destination, content)
                else:
                    self.store.set_state(
                        "media_download:" + temporary.stem,
                        {
                            "profile_id": profile,
                            "generation": self.settings.profile(profile).generation,
                            "chat_id": chat,
                            "expires_at": iso(utcnow() + timedelta(hours=24)),
                        },
                    )
                    retained = True
                return {
                    "profile_id": profile,
                    "chat_id": chat,
                    "message_id": message_id,
                    "path": str(path),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    **metadata,
                }
            finally:
                if not retained:
                    temporary.unlink(missing_ok=True)

    def _destination(self, profile: str, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or path.exists():
            raise TeleloomError(
                "unsafe_file_path", "Choose a new absolute destination without traversal."
            )
        parent = plain_path(path.parent, directory=True)
        roots = self.settings.profile(profile).file_roots
        if not any(parent.is_relative_to(plain_path(root, directory=True)) for root in roots):
            raise TeleloomError(
                "file_not_allowed", "The owner has not allowed this destination root."
            )
        return path

    def _save(self, profile: str, value: str, content: bytes) -> Path:
        path = self._destination(profile, value)
        write_new_verified(path, content)
        return path

    def cleanup_expired(self, profile: str | None = None) -> dict[str, int]:
        import json

        if profile is not None:
            self.settings.profile(profile)
        removed = {"snapshots": self.files.cleanup_expired(profile), "handles": 0, "downloads": 0}
        rows = self.store.db.execute(
            "SELECT key,data FROM state WHERE key GLOB 'media_handle:*' OR key GLOB 'media_download:*'"
        ).fetchall()
        for row in rows:
            record = json.loads(row["data"])
            if profile is not None and record["profile_id"] != profile:
                continue
            if utcnow() < datetime.fromisoformat(record["expires_at"]):
                continue
            if row["key"].startswith("media_download:"):
                id_ = row["key"].split(":", 1)[1]
                if not re.fullmatch(r"[0-9a-f]{32}", id_):
                    raise TeleloomError(
                        "unsafe_file_path", "The private download identity is invalid."
                    )
                root = plain_path(self.settings.data_dir / "media-downloads", directory=True)
                path = root / (id_ + ".bin")
                if path.exists():
                    plain_path(path).unlink()
                removed["downloads"] += 1
            else:
                removed["handles"] += 1
            with self.store.db:
                self.store.db.execute("DELETE FROM state WHERE key=?", (row["key"],))
        return removed

    async def cleanup(self, profile: str) -> dict[str, Any]:
        return {"profile_id": profile, "removed": self.cleanup_expired(profile)}

    async def stickers(self, profile: str, set_name: str | None) -> dict[str, Any]:
        if (
            self.settings.profile(profile).kind == "bot"
            and getattr(self.settings.profile(profile), "bot_backend", "bot_api") == "mtproto"
        ):
            raise TeleloomError(
                "unsupported_capability",
                "Telegram's installed sticker-set list is a user-only API; use a Bot API profile to inspect one named set.",
            )
        adapter = await self.runtime.adapter(profile)
        if not hasattr(adapter, "client"):
            if not set_name:
                raise TeleloomError(
                    "unsupported_capability",
                    "Bot API can inspect one named sticker set; installed user sets require a user profile.",
                )
            result = await cast(BotAdapter, adapter).bot.get_sticker_set(set_name)
            return {
                "items": [
                    {
                        "name": result.name,
                        "title": result.title,
                        "stickers": [
                            {
                                "id": item.file_unique_id,
                                "emoji": item.emoji,
                                "width": item.width,
                                "height": item.height,
                                "animated": item.is_animated,
                                "video": item.is_video,
                            }
                            for item in result.stickers
                        ],
                    }
                ],
                "source": "telegram",
            }
        response = await cast(UserAdapter, adapter).client(
            functions.messages.GetAllStickersRequest(hash=0)
        )
        return {
            "items": [
                {
                    "id": str(item.id),
                    "name": item.short_name,
                    "title": item.title,
                    "count": item.count,
                    "animated": getattr(item, "animated", None),
                    "video": getattr(item, "videos", None),
                }
                for item in response.sets
            ],
            "source": "telegram",
        }

    async def gifs(self, profile: str, query: str, limit: int) -> dict[str, Any]:
        if self.settings.profile(profile).kind != "user":
            raise TeleloomError(
                "unsupported_capability",
                "Telegram's configured inline GIF provider requires a user account.",
            )
        if not query.strip() or len(query) > 256:
            raise TeleloomError("invalid_query", "Provide 1..256 nonblank query characters.")
        adapter = cast(UserAdapter, await self.runtime.adapter(profile))
        response = await user_gifs(adapter, query, "")
        items = []
        for item in response["items"][:limit]:
            handle = self._new_handle(
                profile,
                "gif",
                {"query_id": item["query_id"], "result_id": item["result_id"]},
                response["cache_time"],
            )
            items.append(
                {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"query_id", "result_id"}
                    },
                    **handle,
                }
            )
        return {
            "items": items,
            "provider": response["provider"],
            "source": "telegram",
            "incomplete": bool(response["next_offset"]) or len(response["items"]) > limit,
            "warnings": [
                "Results are capped to the selected limit; narrow the query for additional provider candidates."
            ],
        }

    async def photos(self, profile: str, chat: str, source: str, limit: int) -> dict[str, Any]:
        from .runtime import number

        config = self.settings.profile(profile)
        number(chat)
        config.require_read(chat)
        adapter = await self.runtime.adapter(profile)
        if source == "avatars":
            from .administration import photo_info

            if int(chat) > 0:
                result = await adapter.account_read(
                    {"kind": "photos", "user_id": chat, "limit": limit}, 0
                )
            elif hasattr(adapter, "client"):
                sdk = cast(UserAdapter, adapter)
                peer = await sdk._input_peer(chat)
                if config.kind == "bot":
                    response = await sdk.client(
                        functions.channels.GetFullChannelRequest(peer)
                        if isinstance(peer, types.InputPeerChannel)
                        else functions.messages.GetFullChatRequest(peer.chat_id)
                    )
                    current = photo_info(response.full_chat.chat_photo)
                    result = {
                        "items": [{**current, "is_current": True}] if current else [],
                        "history_available": False,
                    }
                else:
                    photos = await sdk.client.get_profile_photos(peer, limit=limit)
                    entity = await sdk.client.get_entity(peer)
                    current_id = str(getattr(getattr(entity, "photo", None), "photo_id", ""))
                    result = {
                        "items": [
                            {
                                **cast(dict[str, Any], photo_info(photo)),
                                "is_current": str(photo.id) == current_id,
                            }
                            for photo in photos
                        ],
                        "next_position": limit
                        if getattr(photos, "total", len(photos)) > limit
                        else None,
                    }
            else:
                entity = await cast(BotAdapter, adapter).bot.get_chat(int(chat))
                result = {
                    "items": [{"id": entity.photo.big_file_unique_id, "is_current": True}]
                    if entity.photo
                    else [],
                    "history_available": False,
                }
            return {
                **result,
                "chat_id": chat,
                "source": source,
                "incomplete": bool(result.get("next_position"))
                or result.get("history_available") is False,
            }
        if config.kind == "user":
            rows = await user_message_photos(cast(UserAdapter, adapter), chat, limit)
        else:
            rows = [
                {
                    "id": row.id,
                    "message_id": row.id,
                    "date": iso(row.date),
                    "caption": row.text,
                    "is_current": False,
                }
                for row in self.store.messages(profile, chat, limit=100)
                if row.media and row.media.get("kind", row.media.get("type")) == "photo"
            ][:limit]
        return {
            "items": rows,
            "chat_id": chat,
            "source": source,
            "incomplete": config.kind == "bot" or len(rows) == limit,
        }

    async def photo(
        self,
        profile: str,
        chat: str,
        message_id: str | None,
        photo_id: str | None,
        destination: str | None,
        *,
        edge: int = 2048,
        avatar_message_id: str | None = None,
    ) -> dict[str, Any]:
        from .runtime import number

        number(chat)
        self.settings.profile(profile).require_read(chat)
        if destination:
            self._destination(profile, destination)
        if avatar_message_id:
            number(avatar_message_id, positive=True)
            if message_id:
                raise TeleloomError(
                    "invalid_selection", "Choose one message photo or one avatar service message."
                )
        if message_id and photo_id:
            raise TeleloomError("invalid_selection", "Choose one message photo or one avatar.")
        if message_id:
            result = await self.info(profile, chat, message_id)
            if result["item"]["media"].get("kind", result["item"]["media"].get("type")) != "photo":
                raise TeleloomError(
                    "photo_unavailable", "The selected message does not contain a photo."
                )
        adapter = await self.runtime.adapter(profile)
        async with self._download_path(10_000_000) as path:
            try:
                if message_id:
                    await adapter.download_attachment(
                        chat, int(message_id), path, max_bytes=10_000_000
                    )
                else:
                    await adapter.download_avatar(
                        chat, photo_id, path, max_bytes=10_000_000, message_id=avatar_message_id
                    )
                original = read_verified(path, 10_000_000)
                content, metadata = await asyncio.to_thread(image, original, edge=edge)
                saved = str(self._save(profile, destination, original)) if destination else None
                return {
                    "profile_id": profile,
                    "chat_id": chat,
                    "message_id": message_id,
                    "photo_id": photo_id,
                    "avatar_message_id": avatar_message_id,
                    "path": saved,
                    "original_sha256": hashlib.sha256(original).hexdigest(),
                    **metadata,
                    "_image": base64.b64encode(content).decode(),
                    "_mime_type": "image/jpeg",
                }
            finally:
                path.unlink(missing_ok=True)

    async def sheet(
        self, profile: str, chat: str, source: str, limit: int, columns: int
    ) -> dict[str, Any]:
        from PIL import Image, ImageDraw

        listing = await self.photos(profile, chat, source, limit)
        tiles = []
        errors = []
        for item in listing["items"]:
            try:
                photo = await self.photo(
                    profile,
                    chat,
                    item["id"] if source == "messages" else None,
                    item["id"] if source == "avatars" else None,
                    None,
                    edge=256,
                )
                tiles.append((photo["_image"], item["id"]))
            except TeleloomError as exc:
                errors.append({"id": item["id"], "error": exc.code})
        if not tiles:
            raise TeleloomError(
                "photo_unavailable",
                "None of the selected photos could be inspected.",
                details={"errors": errors},
            )
        sheet = Image.new("RGB", (columns * 256, math.ceil(len(tiles) / columns) * 284), "#eeeeee")
        draw = ImageDraw.Draw(sheet)
        for index, (encoded, id_) in enumerate(tiles):
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as tile:
                sheet.paste(tile, ((index % columns) * 256, (index // columns) * 284))
                draw.text(
                    ((index % columns) * 256 + 4, (index // columns) * 284 + 260),
                    str(id_),
                    fill="black",
                )
        output = io.BytesIO()
        sheet.save(output, "JPEG", quality=85)
        return {
            "profile_id": profile,
            "chat_id": chat,
            "source": source,
            "items": [{"id": id_} for _, id_ in tiles],
            "errors": errors,
            "incomplete": bool(errors) or listing["incomplete"],
            "_image": base64.b64encode(output.getvalue()).decode(),
            "_mime_type": "image/jpeg",
        }
