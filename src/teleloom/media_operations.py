"""Typed media confirmations and bounded external SDK operations."""

import io
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from telethon import functions, types, utils

from .models import TeleloomError, iso
from .mutations import receipt as message_receipt
from .rich_reads import media_file

ID = Annotated[str, Field(pattern=r"^-?[1-9][0-9]{0,19}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Send(Strict):
    chat_id: ID
    caption: str = ""
    reply_to_message_id: ID | None = None
    topic_id: ID | None = None
    schedule_date: datetime | None = None


class FileSend(Send):
    kind: Literal["send_file"]
    source_path: str | None = None
    upload_handle: str | None = None
    as_document: bool = True


class AlbumSend(Send):
    kind: Literal["send_album"]
    source_paths: Annotated[list[str], Field(min_length=2, max_length=10)]


class VoiceSend(Send):
    kind: Literal["send_voice"]
    source_path: str


class StickerSend(Send):
    kind: Literal["send_sticker"]
    source_path: str


class GifSend(Send):
    kind: Literal["send_gif"]
    source_path: str | None = None
    gif_handle: str | None = None


class FileUpload(Strict):
    kind: Literal["upload_file"]
    source_path: str


MediaOperation = Annotated[
    FileSend | AlbumSend | VoiceSend | StickerSend | GifSend | FileUpload,
    Field(discriminator="kind"),
]


def uploaded_file(value: dict[str, Any]) -> Any:
    if value["_"] == "InputFileBig":
        return types.InputFileBig(value["id"], value["parts"], value["name"])
    return types.InputFile(value["id"], value["parts"], value["name"], value["md5_checksum"])


def reply(p: dict[str, Any]) -> Any:
    target = p.get("reply_to_message_id") or p.get("topic_id")
    return (
        types.InputReplyToMessage(
            int(target), top_msg_id=int(p["topic_id"]) if p.get("topic_id") else None
        )
        if target
        else None
    )


def receipt(response: Any, random_ids: list[int], schedule: str | None) -> dict[str, Any]:
    result = message_receipt(response, random_ids)
    if schedule:
        result.update(schedule_accepted=True, scheduled_for=schedule, delivery_confirmed=False)
    return result


async def user_media_write(
    adapter: Any,
    p: dict[str, Any],
    random_id: int,
    content: list[bytes],
    handles: list[Any],
) -> dict[str, Any]:
    kind = p["kind"].removeprefix("media_")
    if kind == "upload_file":
        file = p["files"][0]
        uploaded = await adapter.client.upload_file(
            io.BytesIO(content[0]), file_size=len(content[0]), file_name=file["name"]
        )
        return {"accepted": True, "uploaded": uploaded.to_dict()}
    peer = await adapter._input_peer(p["chat_id"])
    schedule = datetime.fromisoformat(p["schedule_date"]) if p.get("schedule_date") else None
    if p.get("gif_handle"):
        handle = handles[0]
        result = await adapter.client(
            functions.messages.SendInlineBotResultRequest(
                peer=peer,
                query_id=handle["query_id"],
                id=handle["result_id"],
                random_id=random_id,
                reply_to=reply(p),
                schedule_date=schedule,
            )
        )
        return receipt(result, [random_id], p.get("schedule_date"))
    medias = []
    for index, file in enumerate(p["files"]):
        uploaded = (
            handles[index]
            if handles
            else await adapter.client.upload_file(
                io.BytesIO(content[index]), file_size=len(content[index]), file_name=file["name"]
            )
        )
        mime = file["mime_type"]
        is_photo = (kind == "send_album" or not p.get("as_document", True)) and mime in {
            "image/jpeg",
            "image/png",
        }
        if is_photo:
            media = types.InputMediaUploadedPhoto(uploaded)
        else:
            attributes: list[Any] = [types.DocumentAttributeFilename(file["name"])]
            if kind == "send_voice":
                attributes.append(types.DocumentAttributeAudio(file["duration"], voice=True))
            elif kind == "send_sticker":
                attributes.extend(
                    [
                        types.DocumentAttributeSticker("", types.InputStickerSetEmpty()),
                        types.DocumentAttributeImageSize(file["width"], file["height"]),
                    ]
                )
            elif kind == "send_gif":
                attributes.append(types.DocumentAttributeAnimated())
            elif mime.startswith("video/"):
                attributes.append(
                    types.DocumentAttributeVideo(
                        file.get("duration", 0),
                        file.get("width", 0),
                        file.get("height", 0),
                        supports_streaming=True,
                    )
                )
            media = types.InputMediaUploadedDocument(
                file=uploaded,
                mime_type=mime,
                attributes=attributes,
                nosound_video=kind == "send_gif" or None,
                force_file=kind == "send_file" and p.get("as_document", True) or None,
            )
        if kind == "send_album":
            cached = await adapter.client(functions.messages.UploadMediaRequest(peer, media))
            media = utils.get_input_media(cached.photo if is_photo else cached.document)
        medias.append(media)
    if kind == "send_album":
        request = functions.messages.SendMultiMediaRequest(
            peer=peer,
            multi_media=[
                types.InputSingleMedia(
                    media,
                    random_id=(random_id + index) % (2**63),
                    message=p["caption"] if index == 0 else "",
                    entities=[],
                )
                for index, media in enumerate(medias)
            ],
            reply_to=reply(p),
            schedule_date=schedule,
        )
    else:
        request = functions.messages.SendMediaRequest(
            peer=peer,
            media=medias[0],
            message=p["caption"],
            entities=[],
            random_id=random_id,
            reply_to=reply(p),
            schedule_date=schedule,
        )
    random_ids = (
        [item.random_id for item in request.multi_media] if kind == "send_album" else [random_id]
    )
    return receipt(await adapter.client(request), random_ids, p.get("schedule_date"))


async def bot_media_write(adapter: Any, p: dict[str, Any], content: list[bytes]) -> dict[str, Any]:
    from aiogram.types import (
        BufferedInputFile,
        InputMediaDocument,
        InputMediaPhoto,
        InputMediaVideo,
        ReplyParameters,
    )

    kind = p["kind"].removeprefix("media_")
    if (
        kind == "upload_file"
        or p.get("gif_handle")
        or p.get("upload_handle")
        or p.get("schedule_date")
    ):
        raise TeleloomError(
            "unsupported_capability", "This operation requires Telegram MTProto user media APIs."
        )
    kwargs: dict[str, Any] = {"chat_id": int(p["chat_id"])}
    if p.get("reply_to_message_id"):
        kwargs["reply_parameters"] = ReplyParameters(message_id=int(p["reply_to_message_id"]))
    if p.get("topic_id"):
        kwargs["message_thread_id"] = int(p["topic_id"])
    files = [
        BufferedInputFile(value, filename=file["name"])
        for value, file in zip(content, p["files"], strict=True)
    ]
    if kind == "send_album":
        album = []
        for index, (file, metadata) in enumerate(zip(files, p["files"], strict=True)):
            mime = metadata["mime_type"]
            model = (
                InputMediaPhoto
                if mime in {"image/jpeg", "image/png"}
                else InputMediaVideo
                if mime.startswith("video/")
                else InputMediaDocument
            )
            album.append(
                model(media=file, caption=p["caption"] if index == 0 else None, parse_mode=None)
            )
        result = await adapter.bot.send_media_group(media=album, **kwargs)
        ids = [str(message.message_id) for message in result]
    else:
        if kind == "send_voice":
            result = await adapter.bot.send_voice(
                voice=files[0], caption=p["caption"], parse_mode=None, **kwargs
            )
        elif kind == "send_sticker":
            result = await adapter.bot.send_sticker(sticker=files[0], **kwargs)
        elif kind == "send_gif":
            result = await adapter.bot.send_animation(
                animation=files[0], caption=p["caption"], parse_mode=None, **kwargs
            )
        elif not p["as_document"] and p["files"][0]["mime_type"] in {"image/jpeg", "image/png"}:
            result = await adapter.bot.send_photo(
                photo=files[0], caption=p["caption"], parse_mode=None, **kwargs
            )
        else:
            result = await adapter.bot.send_document(
                document=files[0], caption=p["caption"], parse_mode=None, **kwargs
            )
        ids = [str(result.message_id)]
    return {"accepted": True, "message_ids": ids}


async def user_gifs(adapter: Any, query: str, offset: str) -> dict[str, Any]:
    config = await adapter.client(functions.help.GetConfigRequest())
    username = getattr(config, "gif_search_username", None)
    if not username:
        raise TeleloomError(
            "unsupported_capability", "Telegram provided no configured GIF search bot."
        )
    bot = utils.get_input_user(await adapter.client.get_input_entity(username))
    response = await adapter.client(
        functions.messages.GetInlineBotResultsRequest(
            bot=bot, peer=types.InputPeerEmpty(), query=query, offset=offset
        )
    )
    return {
        "items": [
            {
                "id": result.id,
                "title": getattr(result, "title", None),
                "description": getattr(result, "description", None),
                "media": media_file(result.document) if getattr(result, "document", None) else None,
                "query_id": response.query_id,
                "result_id": result.id,
            }
            for result in response.results
            if result.type in {"gif", "mpeg4_gif"}
        ],
        "next_offset": response.next_offset,
        "cache_time": response.cache_time,
        "provider": username,
    }


async def user_message_photos(adapter: Any, chat: str, limit: int) -> list[dict[str, Any]]:
    result = []
    peer = await adapter._input_peer(chat)
    async for raw in adapter.client.iter_messages(
        peer, filter=types.InputMessagesFilterPhotos(), limit=limit
    ):
        if getattr(raw, "photo", None):
            result.append(
                {
                    "id": str(raw.id),
                    "message_id": str(raw.id),
                    "photo_id": str(raw.photo.id),
                    "date": iso(raw.date),
                    "caption": raw.message or "",
                    "is_current": False,
                }
            )
    return result
