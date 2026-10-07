from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise TeleloomError("invalid_time", "Dates must include a timezone.")
    return value.astimezone(UTC).isoformat()


class TeleloomError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.details = details or {}


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False
    retry_after: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Result(BaseModel):
    ok: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None


class EvidenceKey(BaseModel):
    """Exact frozen message key; message IDs are only unique inside one chat."""

    chat_id: str = Field(description="Canonical numeric Telegram chat ID.")
    message_id: str = Field(description="Canonical numeric message ID inside that chat.")


class EvidenceAggregate(BaseModel):
    """Allowlisted statistics over collected originals, never agent SQL."""

    model_config = ConfigDict(extra="forbid", strict=True)

    group_by: list[Literal["chat", "day"]] = Field(default_factory=list, max_length=2)
    metrics: list[Literal["count", "incoming_count"]] = Field(
        default=["count", "incoming_count"], min_length=1, max_length=2
    )
    timezone: str = Field(
        default="UTC",
        min_length=1,
        max_length=64,
        description="Publication-day timezone: UTC, a fixed offset (+04:00), or an IANA name (Indian/Mauritius, America/New_York). IANA zones apply date-specific daylight-saving rules; a fixed offset does not.",
    )
    top_k: int | None = Field(default=None, ge=1, le=1000)
    include_outgoing: bool = True
    include_service: bool = True

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        if len(set(self.group_by)) != len(self.group_by) or len(set(self.metrics)) != len(
            self.metrics
        ):
            raise ValueError("Aggregate dimensions and metrics must be unique.")
        return self


class FrozenEvidenceSource(BaseModel):
    """An owned frozen selection, exclusive with legacy index-export filters."""

    model_config = {"extra": "forbid"}
    kind: Literal["frozen_evidence"]
    job_id: str = Field(min_length=1, max_length=128)
    evidence_ref: str = Field(min_length=1, max_length=128)


class Chat(BaseModel):
    id: str
    title: str
    kind: str = "chat"
    username: str | None = None
    unread_count: int = 0
    read_inbox_max_id: str = "0"
    top_message_id: str = "0"
    is_contact: bool | None = None
    is_bot: bool | None = None
    muted: bool | None = None
    archived: bool | None = None
    unread_mark: bool = False
    unread_mentions_count: int = 0
    forum: bool | None = None


class Message(BaseModel):
    profile_id: str
    chat_id: str
    id: str
    date: datetime
    text: str = ""
    text_source: Literal["original", "reconstructed"] = "original"
    original_text: str | None = None
    rich_text: dict[str, Any] | None = None
    custom_emojis: list[dict[str, str]] = Field(default_factory=list)
    reply_quote: dict[str, Any] | None = None
    reply_to_chat_id: str | None = None
    reply_external: bool = False
    web_preview: dict[str, Any] | None = None
    buttons: list[list[dict[str, Any]]] | None = None
    sender_id: str | None = None
    reply_to_message_id: str | None = None
    topic_id: str | None = None
    link: str | None = None
    entities: list[dict[str, Any]] = Field(default_factory=list)
    media: dict[str, Any] | None = None
    edited_at: datetime | None = None
    deleted: bool = False
    outgoing: bool = False
    sender_name: str | None = None
    sender_username: str | None = None
    author_signature: str | None = None
    forwarded_from: dict[str, Any] | None = None
    grouped_id: str | None = None
    kind: Literal["message", "service"] = "message"
    views: int | None = None
    reactions: list[dict[str, Any]] | None = None
    reply_count: int | None = None
    pinned: bool | None = None
    thread_root_id: str | None = None
    transcript: dict[str, Any] | None = None


class Page(BaseModel):
    items: list[Message] = Field(default_factory=list)
    next_cursor: str | None = None
    source: Literal["telegram", "local_index", "bot_updates"]
    coverage: dict[str, Any] = Field(default_factory=dict)
    incomplete: bool = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def rich_coverage(self) -> Self:
        partial = sum(
            bool(item.rich_text.get("partial") or item.rich_text.get("warnings"))
            for item in self.items
            if item.rich_text
        )
        if partial:
            self.coverage["partial_rich_messages"] = partial
            self.incomplete = True
            warning = "Some rich content is partial or could not be represented fully."
            if warning not in self.warnings:
                self.warnings.append(warning)
        return self
