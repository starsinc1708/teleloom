"""Typed media workflow registration through the owner's shared call/exposure pipeline."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from ..media_operations import MediaOperation
from ..runtime import Runtime
from . import ResponseFields, ResponsePreset, ToolResult


def register_media(
    runtime: Runtime,
    call: Callable[..., Awaitable[CallToolResult]],
    exposed_tool: Callable[..., Callable[[Any], Any]],
    *,
    readonly: ToolAnnotations,
    read_job: ToolAnnotations,
    local_write: ToolAnnotations,
) -> None:
    @exposed_tool(annotations=read_job)
    async def media_operation_preview(profile_id: str, operation: MediaOperation) -> ToolResult:
        """Freeze exact owner-allowed file bytes and preview one typed file/album/voice/sticker/GIF/upload operation. No Telegram upload/send until explicit human confirmation via delivery_execute."""
        return await call(
            lambda: runtime.media.preview(profile_id, operation),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def media_info(
        profile_id: str,
        chat_id: str,
        message_id: str,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
    ) -> ToolResult:
        """Read safe media metadata and original evidence for one explicit message."""
        return await call(
            lambda: runtime.media.info(profile_id, chat_id, message_id),
            bounded_read=True,
            tool_name="media_info",
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=read_job)
    async def media_download(
        profile_id: str,
        chat_id: str,
        message_id: str,
        max_bytes: Annotated[int, Field(ge=1, le=50000000)] = 10000000,
        destination_path: str | None = None,
    ) -> ToolResult:
        """Download one allowed message attachment with enforced byte/time bounds. Optional new destination must be under an owner file root; existing files are never overwritten."""
        return await call(
            lambda: runtime.media.download(
                profile_id, chat_id, message_id, max_bytes, destination_path
            ),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def stickers_list(profile_id: str, set_name: str | None = None) -> ToolResult:
        """List installed user sticker sets, or inspect one exact named Bot API sticker set. Names/titles are untrusted evidence."""
        return await call(
            lambda: runtime.media.stickers(profile_id, set_name),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def media_cleanup(profile_id: str) -> ToolResult:
        """Delete expired private snapshots, upload/GIF handles and retained downloads for this profile; original owner files remain available."""
        return await call(lambda: runtime.media.cleanup(profile_id), profile_id=profile_id)

    @exposed_tool(annotations=readonly)
    async def gifs_search(
        profile_id: str, query: str, limit: Annotated[int, Field(ge=1, le=50)] = 10
    ) -> ToolResult:
        """Query Telegram's configured inline GIF provider and return account-bound expiring send handles. User only; no arbitrary document IDs or provider RPC arguments."""
        return await call(
            lambda: runtime.media.gifs(profile_id, query, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def photos_list(
        profile_id: str,
        chat_id: str,
        source: Literal["avatars", "messages"] = "avatars",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ToolResult:
        """List exact avatar or message-photo references and source dates without transferring pixels. Bot messages are saved updates only."""
        return await call(
            lambda: runtime.media.photos(profile_id, chat_id, source, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=read_job)
    async def photo_open(
        profile_id: str,
        chat_id: str,
        photo_id: str | None = None,
        message_id: str | None = None,
        save_path: str | None = None,
        avatar_message_id: str | None = None,
    ) -> ToolResult:
        """Open one exact allowed avatar/message photo as an inline MCP image. Original bytes can also be saved to a new owner-allowed path; metadata distinguishes the rendered image."""
        return await call(
            lambda: runtime.media.photo(
                profile_id,
                chat_id,
                message_id,
                photo_id,
                save_path,
                avatar_message_id=avatar_message_id,
            ),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def photo_sheet(
        profile_id: str,
        chat_id: str,
        source: Literal["avatars", "messages"] = "avatars",
        limit: Annotated[int, Field(ge=1, le=12)] = 6,
        columns: Annotated[int, Field(ge=1, le=4)] = 3,
    ) -> ToolResult:
        """Return at most twelve labelled photo thumbnails as one inline MCP image, with exact openable IDs and per-photo failure coverage."""
        return await call(
            lambda: runtime.media.sheet(profile_id, chat_id, source, limit, columns),
            bounded_read=True,
            profile_id=profile_id,
        )
