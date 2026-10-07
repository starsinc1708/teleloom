"""Typed message operations; only the confirmed job worker may call the SDK writers."""

import base64
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from telethon import functions, types, utils
from telethon.extensions import html, markdown

from .models import TeleloomError, iso, utcnow

ChatID = Annotated[str, Field(pattern=r"^-?[1-9][0-9]*$")]
MessageID = Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
MessageIDs = Annotated[list[MessageID], Field(min_length=1, max_length=100)]
Format = Literal["plain", "html", "markdown", "rich_html", "rich_markdown"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(Strict):
    type: Literal[
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "spoiler",
        "code",
        "pre",
        "text_link",
        "custom_emoji",
        "blockquote",
        "date_time",
    ]
    offset: int = Field(ge=0)
    length: int = Field(ge=1)
    url: str | None = Field(default=None, max_length=2048)
    language: str = Field(default="", max_length=64)
    custom_emoji_id: MessageID | None = None
    unix_time: int | None = Field(default=None, ge=0)
    date_time_format: str = Field(default="", pattern=r"^(r|w?[dD]?[tT]?)$")

    @model_validator(mode="after")
    def required_metadata(self) -> "Entity":
        if self.type == "text_link" and (
            not self.url or not self.url.startswith(("https://", "http://", "tg://"))
        ):
            raise ValueError("text_link requires an http, https or tg URL")
        if self.type == "custom_emoji" and not self.custom_emoji_id:
            raise ValueError("custom_emoji requires custom_emoji_id")
        if self.type == "date_time" and (
            self.unix_time is None or self.unix_time > int(utcnow().timestamp()) + 1098 * 86400
        ):
            raise ValueError("date_time requires unix_time between zero and now plus 1098 days")
        return self


class ChatOperation(Strict):
    chat_id: ChatID


class Content(ChatOperation):
    text: str = Field(min_length=1, max_length=32768)
    format: Format = "plain"
    entities: list[Entity] = Field(default_factory=list, max_length=100)
    no_webpage: bool = True

    @model_validator(mode="after")
    def content_limits(self) -> "Content":
        if not self.text.strip():
            raise ValueError("text cannot be blank")
        if self.entities and self.format != "plain":
            raise ValueError("explicit entities require plain format")
        if self.format == "plain":
            validate_entities(self.text, self.entities)
        return self


class Send(Content):
    kind: Literal["send"]
    reply_to_message_id: MessageID | None = None
    quote_text: str | None = Field(default=None, max_length=1024)
    quote_offset: int | None = Field(default=None, ge=0)
    top_message_id: MessageID | None = None
    send_as: ChatID | None = None
    schedule_at: datetime | None = None
    silent: bool = False

    @model_validator(mode="after")
    def reply_and_schedule(self) -> "Send":
        if self.quote_text is not None and not self.reply_to_message_id:
            raise ValueError("quote_text requires a reply target")
        if self.quote_offset is not None and self.quote_text is None:
            raise ValueError("quote_offset requires quote_text")
        if self.schedule_at:
            self.schedule_at = datetime.fromisoformat(iso(self.schedule_at))
        return self


class Edit(Content):
    kind: Literal["edit"]
    message_id: MessageID


class Delete(ChatOperation):
    kind: Literal["delete"]
    message_ids: MessageIDs
    revoke: bool = True


class Forward(ChatOperation):
    kind: Literal["forward"]
    source_chat_id: ChatID
    message_ids: MessageIDs
    expand_album: bool = True
    drop_author: bool = False
    drop_media_captions: bool = False
    top_message_id: MessageID | None = None
    reply_to_message_id: MessageID | None = None
    send_as: ChatID | None = None
    schedule_at: datetime | None = None
    silent: bool = False

    @field_validator("schedule_at")
    @classmethod
    def aware_schedule(cls, value: datetime | None) -> datetime | None:
        return datetime.fromisoformat(iso(value)) if value else None


class Reaction(ChatOperation):
    kind: Literal["reaction"]
    message_id: MessageID
    reactions: list[str] = Field(default_factory=list, max_length=3)
    big: bool = False


class Poll(ChatOperation):
    kind: Literal["poll"]
    question: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=10)
    multiple_choice: bool = False
    public_votes: bool = False
    quiz: bool = False
    correct_option: int | None = Field(default=None, ge=0, le=9)
    close_at: datetime | None = None
    reply_to_message_id: MessageID | None = None

    @model_validator(mode="after")
    def valid_poll(self) -> "Poll":
        if (
            not self.question.strip()
            or any(not x.strip() or len(x) > 100 for x in self.options)
            or len(set(self.options)) != len(self.options)
        ):
            raise ValueError(
                "provide a question and unique nonempty options of at most 100 characters"
            )
        if self.quiz != (self.correct_option is not None) or (
            self.correct_option is not None and self.correct_option >= len(self.options)
        ):
            raise ValueError("quiz requires one valid correct_option; regular poll has none")
        if self.quiz and self.multiple_choice:
            raise ValueError("quiz cannot be multiple choice")
        if self.close_at:
            self.close_at = datetime.fromisoformat(iso(self.close_at))
        return self


class ContactSend(ChatOperation):
    kind: Literal["contact_send"]
    phone_number: str = Field(min_length=3, max_length=32, pattern=r"^\+?[0-9 ()-]+$")
    first_name: str = Field(min_length=1, max_length=128)
    last_name: str = Field(default="", max_length=128)
    vcard: str = Field(default="", max_length=2048)
    reply_to_message_id: MessageID | None = None


class Pin(ChatOperation):
    kind: Literal["pin"]
    message_id: MessageID
    unpin: bool = False
    silent: bool = True
    for_self_only: bool = False


class ClearPins(ChatOperation):
    kind: Literal["unpin_all"]
    max_requests: int = Field(default=100, ge=1, le=1000)


class ReadAck(ChatOperation):
    kind: Literal["read_ack"]
    through_message_id: MessageID


class DeleteHistory(ChatOperation):
    kind: Literal["delete_history"]
    through_message_id: MessageID
    revoke: bool = False
    max_requests: int = Field(default=100, ge=1, le=1000)


class Mute(ChatOperation):
    kind: Literal["mute"]
    muted: bool


class Archive(ChatOperation):
    kind: Literal["archive"]
    archived: bool


class Draft(Content):
    kind: Literal["draft_save"]
    reply_to_message_id: MessageID | None = None


class ClearDraft(ChatOperation):
    kind: Literal["draft_clear"]


class ScheduledDelete(ChatOperation):
    kind: Literal["scheduled_delete"]
    message_ids: MessageIDs


class InlineCallback(ChatOperation):
    kind: Literal["inline_callback"]
    message_id: MessageID
    button_index: int = Field(ge=0, le=99)


class InlineSend(ChatOperation):
    kind: Literal["inline_send"]
    bot_id: ChatID
    query: str = Field(default="", max_length=256)
    result_id: str = Field(min_length=1, max_length=256)
    reply_to_message_id: MessageID | None = None


MessageOperation = Annotated[
    Send
    | Edit
    | Delete
    | Forward
    | Reaction
    | Poll
    | ContactSend
    | Pin
    | ClearPins
    | ReadAck
    | DeleteHistory
    | Mute
    | Archive
    | Draft
    | ClearDraft
    | ScheduledDelete
    | InlineCallback
    | InlineSend,
    Field(discriminator="kind"),
]
MessageState = Literal[
    "drafts", "scheduled", "buttons", "read_receipts", "send_as", "reactions", "inline_results"
]
SEND_OPERATIONS = {"send", "forward", "poll", "contact_send", "inline_send"}
BOT_OPERATIONS = {
    "send",
    "edit",
    "delete",
    "forward",
    "reaction",
    "poll",
    "contact_send",
    "pin",
    "unpin_all",
}


def validate_entities(text: str, entities: list[Entity]) -> None:
    units = len(text.encode("utf-16-le")) // 2
    if units > 4096:
        raise ValueError("text exceeds 4096 UTF-16 units")
    for entity in entities:
        end = entity.offset + entity.length
        if end > units:
            raise ValueError("entity exceeds text")
        try:
            # Slicing a surrogate pair is invalid even if its numeric offset fits.
            text.encode("utf-16-le")[entity.offset * 2 : end * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            raise ValueError("entity splits a UTF-16 surrogate pair") from None
        if entity.type == "date_time" and any(
            other is not entity
            and other.offset < end
            and other.offset + other.length > entity.offset
            for other in entities
        ):
            raise ValueError("date_time entities cannot overlap or nest inside another entity")


def telegram_content(operation: dict[str, Any]) -> dict[str, Any]:
    text, mode = operation["text"], operation["format"]
    if mode.startswith("rich_"):
        rich = (
            types.InputRichMessageHTML(text)
            if mode == "rich_html"
            else types.InputRichMessageMarkdown(text)
        )
        return {"message": "", "rich_message": rich}
    if operation.get("rendered"):
        rendered = operation["rendered"]
        allowed = {
            cls.__name__: cls
            for cls in (
                types.MessageEntityBold,
                types.MessageEntityItalic,
                types.MessageEntityUnderline,
                types.MessageEntityStrike,
                types.MessageEntitySpoiler,
                types.MessageEntityCode,
                types.MessageEntityPre,
                types.MessageEntityTextUrl,
                types.MessageEntityCustomEmoji,
                types.MessageEntityBlockquote,
                types.MessageEntityUrl,
                types.MessageEntityEmail,
                types.MessageEntityFormattedDate,
            )
        }
        restored = []
        for entity in rendered["entities"]:
            frozen_metadata = {key: value for key, value in entity.items() if key != "_"}
            if entity["_"] == "MessageEntityFormattedDate":
                frozen_metadata["date"] = datetime.fromisoformat(frozen_metadata["date"])
            restored.append(allowed[entity["_"]](**frozen_metadata))
        return {"message": rendered["text"], "entities": restored}
    if mode == "html":
        text, entities = html.parse(text)
    elif mode == "markdown":
        text, entities = markdown.parse(text)
    else:
        entities = []
        classes = {
            "bold": types.MessageEntityBold,
            "italic": types.MessageEntityItalic,
            "underline": types.MessageEntityUnderline,
            "strikethrough": types.MessageEntityStrike,
            "spoiler": types.MessageEntitySpoiler,
            "code": types.MessageEntityCode,
            "blockquote": types.MessageEntityBlockquote,
        }
        for raw in operation["entities"]:
            metadata: dict[str, Any] = {}
            if raw["type"] == "pre":
                entity_class = types.MessageEntityPre
                metadata["language"] = raw["language"]
            elif raw["type"] == "text_link":
                entity_class = types.MessageEntityTextUrl
                metadata["url"] = raw["url"]
            elif raw["type"] == "custom_emoji":
                entity_class = types.MessageEntityCustomEmoji
                metadata["document_id"] = int(raw["custom_emoji_id"])
            elif raw["type"] == "date_time":
                entity_class = types.MessageEntityFormattedDate
                metadata["date"] = datetime.fromtimestamp(raw["unix_time"], UTC)
                for flag, control in (
                    ("relative", "r"),
                    ("day_of_week", "w"),
                    ("short_date", "d"),
                    ("long_date", "D"),
                    ("short_time", "t"),
                    ("long_time", "T"),
                ):
                    metadata[flag] = control in raw["date_time_format"]
            else:
                entity_class = classes[raw["type"]]
            entities.append(entity_class(offset=raw["offset"], length=raw["length"], **metadata))
    if not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
        raise TeleloomError(
            "invalid_text", "Parsed text must be nonempty and at most 4096 UTF-16 units."
        )
    return {"message": text, "entities": entities}


def source_targets(operation: dict[str, Any]) -> list[tuple[str, list[str]]]:
    kind, chat = operation["kind"], operation["chat_id"]
    targets = []
    if kind == "forward":
        targets.append((operation["source_chat_id"], operation["message_ids"]))
    elif kind == "delete":
        targets.append((chat, operation["message_ids"]))
    elif operation.get("message_id"):
        targets.append((chat, [operation["message_id"]]))
    elif operation.get("through_message_id"):
        targets.append((chat, [operation["through_message_id"]]))
    if operation.get("reply_to_message_id"):
        targets.append((chat, [operation["reply_to_message_id"]]))
    if operation.get("top_message_id"):
        targets.append((chat, [operation["top_message_id"]]))
    return targets


def operation_capability(
    kind: str, operation: dict[str, Any], bot_backend: str = "bot_api"
) -> None:
    if operation["kind"] == "forward" and operation.get("reply_to_message_id"):
        raise TeleloomError(
            "platform_restriction",
            "Telegram forwarding does not accept an ordinary message reply. Use top_message_id for a forum topic; MonoForum routing requires its own typed contract.",
        )
    if kind == "bot":
        if operation["kind"] not in BOT_OPERATIONS:
            raise TeleloomError(
                "unsupported_capability", "This operation requires a Telegram user profile."
            )
        if any(
            operation.get(key)
            for key in (
                "schedule_at",
                "send_as",
                "for_self_only",
            )
        ):
            raise TeleloomError(
                "unsupported_capability",
                "The bot account does not provide the requested schedule/send-as/personal-pin option.",
            )
        if bot_backend == "bot_api" and (
            operation.get("drop_author") or operation.get("drop_media_captions")
        ):
            raise TeleloomError(
                "unsupported_capability",
                "The aiogram Bot API forward endpoint lacks attribution options; an explicitly provisioned MTProto bot can use them.",
            )
        if operation["kind"] == "delete" and not operation["revoke"]:
            raise TeleloomError(
                "unsupported_capability",
                "Bot API deletions apply to the chat, not only the bot's view.",
            )
    if (
        operation.get("schedule_at")
        and datetime.fromisoformat(operation["schedule_at"]) <= utcnow()
    ):
        raise TeleloomError("invalid_time", "schedule_at must be in the future.")
    if operation.get("close_at") and datetime.fromisoformat(operation["close_at"]) <= utcnow():
        raise TeleloomError("invalid_time", "close_at must be in the future.")
    if operation.get("message_ids") and len(set(operation["message_ids"])) != len(
        operation["message_ids"]
    ):
        raise TeleloomError("invalid_message_ids", "Message IDs must be unique.")
    for value in operation.get("reactions", []):
        if (
            not value
            or len(value) > 128
            or (
                value.startswith("custom:")
                and (not value[7:].isascii() or not value[7:].isdigit() or int(value[7:]) <= 0)
            )
        ):
            raise TeleloomError(
                "invalid_reaction", "Use an emoji or custom:<positive document ID>."
            )
    if "text" in operation:
        telegram_content(operation)


def receipt(updates: Any, random_ids: list[int]) -> dict[str, Any]:
    ids = {}
    for update in getattr(updates, "updates", []):
        if getattr(update, "random_id", None) in random_ids and hasattr(update, "id"):
            ids[update.random_id] = str(update.id)
    if hasattr(updates, "id") and len(random_ids) == 1:
        ids[random_ids[0]] = str(updates.id)
    # Telegram can omit UpdateMessageID in an accepted response; do not invent IDs.
    if len(ids) != len(random_ids):
        outgoing = [
            str(update.message.id)
            for update in getattr(updates, "updates", [])
            if hasattr(update, "message") and getattr(update.message, "out", False)
        ]
        if len(outgoing) == len(random_ids):
            return {"accepted": True, "message_ids": outgoing}
        raise TeleloomError(
            "delivery_unknown",
            "Telegram accepted the operation without complete identifiable message receipts.",
        )
    return {"accepted": True, "message_ids": [ids[id_] for id_ in random_ids]}


async def user_mutate(adapter: Any, operation: dict[str, Any], random_id: int) -> dict[str, Any]:
    p, kind = operation, operation["kind"]
    peer = await adapter._input_peer(p["chat_id"])
    reply = (
        types.InputReplyToMessage(
            reply_to_msg_id=int(p["reply_to_message_id"]),
            top_msg_id=int(p["top_message_id"]) if p.get("top_message_id") else None,
            quote_text=p.get("quote_text"),
            quote_offset=p.get("quote_offset"),
        )
        if p.get("reply_to_message_id")
        else (
            types.InputReplyToMessage(
                reply_to_msg_id=int(p["top_message_id"]), top_msg_id=int(p["top_message_id"])
            )
            if p.get("top_message_id")
            else None
        )
    )
    schedule = datetime.fromisoformat(p["schedule_at"]) if p.get("schedule_at") else None
    send_as = await adapter._input_peer(p["send_as"]) if p.get("send_as") else None
    if kind == "send":
        updates = await adapter.client(
            functions.messages.SendMessageRequest(
                peer=peer,
                random_id=random_id,
                reply_to=reply,
                schedule_date=schedule,
                send_as=send_as,
                silent=p["silent"],
                no_webpage=p["no_webpage"],
                **telegram_content(p),
            )
        )
        result = receipt(updates, [random_id])
        if schedule:
            result.update(
                schedule_accepted=True, scheduled_for=p["schedule_at"], delivery_confirmed=False
            )
        return result
    if kind == "edit":
        await adapter.client(
            functions.messages.EditMessageRequest(
                peer=peer,
                id=int(p["message_id"]),
                no_webpage=p["no_webpage"],
                **telegram_content(p),
            )
        )
    elif kind == "delete":
        ids = [int(x) for x in p["message_ids"]]
        if isinstance(peer, types.InputPeerChannel):
            await adapter.client(functions.channels.DeleteMessagesRequest(channel=peer, id=ids))
        else:
            await adapter.client(
                functions.messages.DeleteMessagesRequest(id=ids, revoke=p["revoke"])
            )
    elif kind == "forward":
        random_ids = [(random_id + i) % (2**63) for i in range(len(p["message_ids"]))]
        updates = await adapter.client(
            functions.messages.ForwardMessagesRequest(
                from_peer=await adapter._input_peer(p["source_chat_id"]),
                id=[int(x) for x in p["message_ids"]],
                to_peer=peer,
                random_id=random_ids,
                drop_author=p["drop_author"],
                drop_media_captions=p["drop_media_captions"],
                top_msg_id=int(p["top_message_id"]) if p.get("top_message_id") else None,
                reply_to=None,
                schedule_date=schedule,
                send_as=send_as,
                silent=p["silent"],
            )
        )
        result = receipt(updates, random_ids)
        if schedule:
            result.update(
                schedule_accepted=True, scheduled_for=p["schedule_at"], delivery_confirmed=False
            )
        return result
    elif kind == "reaction":
        reactions = [
            types.ReactionCustomEmoji(document_id=int(value[7:]))
            if value.startswith("custom:")
            else types.ReactionEmoji(emoticon=value)
            for value in p["reactions"]
        ]
        await adapter.client(
            functions.messages.SendReactionRequest(
                peer=peer, msg_id=int(p["message_id"]), reaction=reactions, big=p["big"]
            )
        )
    elif kind == "poll":
        poll = types.Poll(
            id=random_id,
            hash=0,
            question=types.TextWithEntities(p["question"], []),
            answers=[
                types.PollAnswer(types.TextWithEntities(text, []), bytes([i]))
                for i, text in enumerate(p["options"])
            ],
            multiple_choice=p["multiple_choice"],
            public_voters=p["public_votes"],
            quiz=p["quiz"],
            close_date=datetime.fromisoformat(p["close_at"]) if p.get("close_at") else None,
        )
        media = types.InputMediaPoll(
            poll=poll, correct_answers=[p["correct_option"]] if p["quiz"] else None
        )
        return receipt(
            await adapter.client(
                functions.messages.SendMediaRequest(
                    peer=peer, media=media, message="", random_id=random_id, reply_to=reply
                )
            ),
            [random_id],
        )
    elif kind == "contact_send":
        media = types.InputMediaContact(
            phone_number=p["phone_number"],
            first_name=p["first_name"],
            last_name=p["last_name"],
            vcard=p["vcard"],
        )
        return receipt(
            await adapter.client(
                functions.messages.SendMediaRequest(
                    peer=peer, media=media, message="", random_id=random_id, reply_to=reply
                )
            ),
            [random_id],
        )
    elif kind == "pin":
        await adapter.client(
            functions.messages.UpdatePinnedMessageRequest(
                peer=peer,
                id=int(p["message_id"]),
                silent=p["silent"],
                unpin=p["unpin"],
                pm_oneside=p["for_self_only"],
            )
        )
    elif kind == "unpin_all":
        result = await adapter.client(functions.messages.UnpinAllMessagesRequest(peer=peer))
        if getattr(result, "offset", 0):
            return {
                "accepted": True,
                "complete": False,
                "remaining_offset": result.offset,
                "continue": True,
            }
    elif kind == "read_ack":
        await adapter.acknowledge(p["chat_id"], int(p["through_message_id"]))
    elif kind == "delete_history":
        if isinstance(peer, types.InputPeerChannel):
            result = await adapter.client(
                functions.channels.DeleteHistoryRequest(
                    channel=peer, max_id=int(p["through_message_id"]), for_everyone=p["revoke"]
                )
            )
        else:
            result = await adapter.client(
                functions.messages.DeleteHistoryRequest(
                    peer=peer, max_id=int(p["through_message_id"]), revoke=p["revoke"]
                )
            )
        if getattr(result, "offset", 0):
            return {
                "accepted": True,
                "complete": False,
                "remaining_offset": result.offset,
                "continue": True,
            }
    elif kind == "mute":
        await adapter.client(
            functions.account.UpdateNotifySettingsRequest(
                peer=types.InputNotifyPeer(peer),
                settings=types.InputPeerNotifySettings(
                    mute_until=datetime(2038, 1, 19, 3, 14, 7, tzinfo=UTC)
                    if p["muted"]
                    else datetime(1970, 1, 1, tzinfo=UTC)
                ),
            )
        )
    elif kind == "archive":
        await adapter.client(
            functions.folders.EditPeerFoldersRequest(
                folder_peers=[types.InputFolderPeer(peer=peer, folder_id=int(p["archived"]))]
            )
        )
    elif kind in {"draft_save", "draft_clear"}:
        content = telegram_content(p) if kind == "draft_save" else {"message": ""}
        await adapter.client(
            functions.messages.SaveDraftRequest(
                peer=peer, reply_to=reply, no_webpage=p.get("no_webpage", True), **content
            )
        )
    elif kind == "scheduled_delete":
        await adapter.client(
            functions.messages.DeleteScheduledMessagesRequest(
                peer=peer, id=[int(x) for x in p["message_ids"]]
            )
        )
    elif kind == "inline_callback":
        result = await adapter.client(
            functions.messages.GetBotCallbackAnswerRequest(
                peer=peer,
                msg_id=int(p["message_id"]),
                data=base64.b64decode(p["callback"]["data_base64"], validate=True),
            )
        )
        return {
            "accepted": True,
            "response": getattr(result, "message", None),
            "url": getattr(result, "url", None),
            "alert": bool(getattr(result, "alert", False)),
        }
    elif kind == "inline_send":
        updates = await adapter.client(
            functions.messages.SendInlineBotResultRequest(
                peer=peer,
                query_id=int(p["inline_result"]["query_id"]),
                id=p["result_id"],
                random_id=random_id,
                reply_to=reply,
            )
        )
        return receipt(updates, [random_id])
    else:
        raise TeleloomError("unsupported_operation", "Unknown typed message operation.")
    return {"accepted": True, "complete": True}


async def bot_mutate(adapter: Any, operation: dict[str, Any], random_id: int) -> dict[str, Any]:
    from aiogram.types import (
        InputPollOption,
        InputRichMessage,
        MessageEntity,
        ReactionTypeCustomEmoji,
        ReactionTypeEmoji,
        ReplyParameters,
    )

    p, kind, bot = operation, operation["kind"], adapter.bot
    chat = int(p["chat_id"])
    reply = (
        ReplyParameters(
            message_id=int(p["reply_to_message_id"]),
            quote=p.get("quote_text"),
            quote_position=p.get("quote_offset"),
        )
        if p.get("reply_to_message_id")
        else None
    )
    if kind in {"send", "edit"}:
        content = telegram_content(p)
        kwargs: dict[str, Any] = {"chat_id": chat}
        if p["format"].startswith("rich_"):
            kwargs["rich_message"] = InputRichMessage(
                **{"html" if p["format"] == "rich_html" else "markdown": p["text"]}
            )
        else:
            # Parse once with the same SDK formatter as preview; Bot API receives exact entities.
            kwargs.update(
                text=content["message"],
                entities=[
                    MessageEntity.model_validate(_bot_out_entity(entity))
                    for entity in content["entities"]
                ],
                parse_mode=None,
            )
        if kind == "send":
            kwargs.update(
                reply_parameters=reply,
                message_thread_id=int(p["top_message_id"]) if p.get("top_message_id") else None,
                disable_notification=p["silent"],
            )
            if "rich_message" in kwargs:
                raw = await bot.send_rich_message(**kwargs)
            else:
                from aiogram.types import LinkPreviewOptions

                kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=p["no_webpage"])
                raw = await bot.send_message(**kwargs)
        else:
            kwargs["message_id"] = int(p["message_id"])
            raw = await bot.edit_message_text(**kwargs)
        _save_bot_response(adapter, raw)
        return {
            "accepted": True,
            "message_ids": [str(raw.message_id)]
            if hasattr(raw, "message_id")
            else [p["message_id"]],
        }
    if kind == "delete":
        await bot.delete_messages(chat_id=chat, message_ids=[int(x) for x in p["message_ids"]])
    elif kind == "forward":
        raw_ids = await bot.forward_messages(
            chat_id=chat,
            from_chat_id=int(p["source_chat_id"]),
            message_ids=sorted(int(x) for x in p["message_ids"]),
            disable_notification=p["silent"],
            message_thread_id=int(p["top_message_id"]) if p.get("top_message_id") else None,
        )
        if len(raw_ids) != len(p["message_ids"]):
            return {
                "accepted": True,
                "complete": False,
                "message_ids": [str(raw.message_id) for raw in raw_ids],
                "warning": "Telegram skipped messages; inspect the returned receipts.",
            }
        return {"accepted": True, "message_ids": [str(raw.message_id) for raw in raw_ids]}
    elif kind == "reaction":
        values = [
            ReactionTypeCustomEmoji(custom_emoji_id=x[7:])
            if x.startswith("custom:")
            else ReactionTypeEmoji(emoji=x)
            for x in p["reactions"]
        ]
        await bot.set_message_reaction(
            chat_id=chat, message_id=int(p["message_id"]), reaction=values, is_big=p["big"]
        )
    elif kind == "poll":
        raw = await bot.send_poll(
            chat_id=chat,
            question=p["question"],
            options=[InputPollOption(text=text) for text in p["options"]],
            allows_multiple_answers=p["multiple_choice"],
            is_anonymous=not p["public_votes"],
            type="quiz" if p["quiz"] else "regular",
            correct_option_ids=[p["correct_option"]] if p["quiz"] else None,
            close_date=datetime.fromisoformat(p["close_at"]) if p.get("close_at") else None,
            reply_parameters=reply,
        )
        _save_bot_response(adapter, raw)
        return {
            "accepted": True,
            "message_ids": [str(raw.message_id)],
            "poll_id": raw.poll.id if raw.poll else None,
        }
    elif kind == "contact_send":
        raw = await bot.send_contact(
            chat_id=chat,
            phone_number=p["phone_number"],
            first_name=p["first_name"],
            last_name=p["last_name"],
            vcard=p["vcard"],
            reply_parameters=reply,
        )
        _save_bot_response(adapter, raw)
        return {"accepted": True, "message_ids": [str(raw.message_id)]}
    elif kind == "pin":
        if p["unpin"]:
            await bot.unpin_chat_message(chat_id=chat, message_id=int(p["message_id"]))
        else:
            await bot.pin_chat_message(
                chat_id=chat, message_id=int(p["message_id"]), disable_notification=p["silent"]
            )
    elif kind == "unpin_all":
        await bot.unpin_all_chat_messages(chat_id=chat)
    else:
        raise TeleloomError(
            "unsupported_capability", "This operation requires a Telegram user profile."
        )
    return {"accepted": True, "complete": True}


def _save_bot_response(adapter: Any, raw: Any) -> None:
    from .telegram.evidence import bot_message

    if hasattr(raw, "message_id"):
        message = bot_message(adapter.profile_id, raw)
        message.outgoing = True
        adapter.store.save_messages([message])


def _bot_out_entity(entity: Any) -> dict[str, Any]:
    mapping = {
        types.MessageEntityBold: "bold",
        types.MessageEntityItalic: "italic",
        types.MessageEntityUnderline: "underline",
        types.MessageEntityStrike: "strikethrough",
        types.MessageEntitySpoiler: "spoiler",
        types.MessageEntityCode: "code",
        types.MessageEntityPre: "pre",
        types.MessageEntityTextUrl: "text_link",
        types.MessageEntityCustomEmoji: "custom_emoji",
        types.MessageEntityBlockquote: "blockquote",
        types.MessageEntityUrl: "url",
        types.MessageEntityEmail: "email",
        types.MessageEntityFormattedDate: "date_time",
    }
    result = {"type": mapping[type(entity)], "offset": entity.offset, "length": entity.length}
    for key in ("url", "language"):
        if hasattr(entity, key):
            result[key] = getattr(entity, key)
    if hasattr(entity, "document_id"):
        result["custom_emoji_id"] = str(entity.document_id)
    if isinstance(entity, types.MessageEntityFormattedDate):
        result["unix_time"] = int(entity.date.timestamp())
        result["date_time_format"] = "".join(
            control
            for flag, control in (
                ("relative", "r"),
                ("day_of_week", "w"),
                ("short_date", "d"),
                ("long_date", "D"),
                ("short_time", "t"),
                ("long_time", "T"),
            )
            if getattr(entity, flag)
        )
    return result


def draft_record(profile: str, chat: str, draft: Any) -> dict[str, Any]:
    from .rich_reads import entities, reply_quote, text_evidence

    reply = getattr(draft, "reply_to", None)
    reply_id = getattr(reply, "reply_to_msg_id", None)
    reply_peer = getattr(reply, "reply_to_peer_id", None)
    return {
        "profile_id": profile,
        "chat_id": chat,
        "kind": "draft",
        **text_evidence(
            draft.message, entities(draft.entities), getattr(draft, "rich_message", None)
        ),
        "entities": entities(draft.entities),
        "date": iso(draft.date),
        "reply_to_message_id": str(reply_id) if reply_id else None,
        "reply_to_chat_id": str(utils.get_peer_id(reply_peer)) if reply_peer else None,
        "reply_external": bool(reply_peer),
        "reply_quote": reply_quote(reply),
        "no_webpage": bool(getattr(draft, "no_webpage", False)),
    }


async def user_drafts(adapter: Any) -> list[dict[str, Any]]:
    """The Telegram aggregate has no pagination; return only permitted draft records."""
    result = await adapter.client(functions.messages.GetAllDraftsRequest())
    records = []
    for update in result.updates:
        if not isinstance(update, types.UpdateDraftMessage) or not isinstance(
            update.draft, types.DraftMessage
        ):
            continue
        chat = str(utils.get_peer_id(update.peer))
        if adapter.profile.allows_read(chat):
            records.append(
                {
                    **draft_record(adapter.profile_id, chat, update.draft),
                    "top_message_id": str(update.top_msg_id) if update.top_msg_id else None,
                }
            )
    return records


async def user_message_state(
    adapter: Any,
    chat: str,
    kind: str,
    message_id: str | None,
    limit: int,
    query: str,
    bot_id: str | None,
) -> dict[str, Any]:
    from .telegram.evidence import telethon_message

    peer = await adapter._input_peer(chat)
    if kind == "scheduled":
        result = await adapter.client(
            functions.messages.GetScheduledHistoryRequest(peer=peer, hash=0)
        )
        rows = list(result.messages)
        return {
            "items": [
                telethon_message(adapter.profile_id, row).model_dump(mode="json")
                for row in rows[:limit]
            ],
            "incomplete": len(rows) > limit,
            "source": "telegram_scheduled",
            "delivery_confirmed": False,
        }
    if kind == "drafts":
        result = await adapter.client(
            functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)])
        )
        draft = next(
            (
                getattr(dialog, "draft", None)
                for dialog in result.dialogs
                if str(utils.get_peer_id(dialog.peer)) == chat
            ),
            None,
        )
        return {
            "items": [draft_record(adapter.profile_id, chat, draft)]
            if draft and isinstance(draft, types.DraftMessage)
            else [],
            "source": "telegram_draft",
        }
    if kind == "send_as":
        result = await adapter.client(functions.channels.GetSendAsRequest(peer=peer))
        return {
            "items": [
                {
                    "id": str(utils.get_peer_id(item.peer)),
                    "premium_required": bool(item.premium_required),
                }
                for item in result.peers[:limit]
            ],
            "incomplete": len(result.peers) > limit,
        }
    if kind == "inline_results":
        if not bot_id:
            raise TeleloomError("invalid_bot", "inline_results requires an exact bot_id.")
        result = await adapter.client(
            functions.messages.GetInlineBotResultsRequest(
                bot=await adapter._input_peer(bot_id), peer=peer, query=query, offset=""
            )
        )
        return {
            "query_id": str(result.query_id),
            "bot_id": bot_id,
            "query": query,
            "items": [
                {
                    "id": item.id,
                    "type": item.type,
                    "title": getattr(item, "title", None),
                    "description": getattr(item, "description", None),
                    "send_message": _inline_content(item.send_message),
                    "media": _inline_media(item),
                }
                for item in result.results[:limit]
            ],
            "incomplete": bool(result.next_offset) or len(result.results) > limit,
        }
    if not message_id:
        raise TeleloomError("message_required", "This query requires an exact message_id.")
    if kind == "read_receipts":
        result = await adapter.client(
            functions.messages.GetMessageReadParticipantsRequest(peer=peer, msg_id=int(message_id))
        )
        return {
            "items": [
                {
                    "user_id": str(getattr(item, "user_id", item)),
                    "read_at": iso(item.date) if getattr(item, "date", None) else None,
                }
                for item in result[:limit]
            ],
            "incomplete": len(result) > limit,
            "limitations": "Telegram restricts group size, age and read privacy; rejection is not an empty result.",
        }
    if kind == "reactions":
        result = await adapter.client(
            functions.messages.GetMessageReactionsListRequest(
                peer=peer, id=int(message_id), limit=limit
            )
        )
        return {
            "items": [
                {
                    "peer_id": str(utils.get_peer_id(item.peer_id)),
                    "reaction": "custom:" + str(item.reaction.document_id)
                    if isinstance(item.reaction, types.ReactionCustomEmoji)
                    else getattr(item.reaction, "emoticon", None),
                    "date": iso(item.date),
                }
                for item in result.reactions
            ],
            "next_offset": result.next_offset,
            "incomplete": bool(result.next_offset),
        }
    raw = await adapter.client.get_messages(peer, ids=int(message_id))
    if not raw:
        raise TeleloomError("message_not_found", "Message was not found in the selected chat.")
    buttons = [
        button
        for row in getattr(getattr(raw, "reply_markup", None), "rows", [])
        for button in row.buttons
    ]
    return {
        "items": [
            {
                "index": i,
                "text": button.text,
                "url": getattr(getattr(button, "type", button), "url", None),
                "data_base64": base64.b64encode(getattr(button, "type", button).data).decode(
                    "ascii"
                )
                if getattr(getattr(button, "type", button), "data", None) is not None
                else None,
                "requires_password": bool(
                    getattr(getattr(button, "type", button), "requires_password", False)
                ),
                "switch_inline_query": getattr(getattr(button, "type", button), "query", None),
                "same_peer": getattr(getattr(button, "type", button), "same_peer", None),
            }
            for i, button in enumerate(buttons[:limit])
        ],
        "incomplete": len(buttons) > limit,
        "inline_bot_id": str(raw.via_bot_id) if getattr(raw, "via_bot_id", None) else None,
    }


def _inline_content(value: Any) -> dict[str, Any]:
    # Inline query output is untrusted evidence, not arbitrary SDK objects/access hashes.
    result = {"type": type(value).__name__}
    for key in ("message", "phone_number", "first_name", "last_name", "title", "address"):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


def _inline_media(item: Any) -> dict[str, Any] | None:
    document = getattr(item, "document", None)
    if document:
        return {
            "type": "document",
            "id": str(document.id),
            "mime_type": getattr(document, "mime_type", None),
            "size": getattr(document, "size", None),
            "name": next(
                (
                    attribute.file_name
                    for attribute in getattr(document, "attributes", [])
                    if isinstance(attribute, types.DocumentAttributeFilename)
                ),
                None,
            ),
        }
    photo = getattr(item, "photo", None)
    if photo:
        return {"type": "photo", "id": str(photo.id)}
    web = getattr(item, "content", None)
    if web:
        return {
            "type": "web",
            "url": getattr(web, "url", None),
            "mime_type": getattr(web, "mime_type", None),
        }
    return None
