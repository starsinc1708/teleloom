import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from .account_operations import AccountOperation, AdministrationOperation
from .adapters import make_adapter
from .administration import AccountRead, AdministrationRead
from .build_info import build_info
from .config import Settings
from .contacts import ContactOperation
from .events import EventFilter
from .folder_operations import FolderOperation
from .models import ErrorInfo, EvidenceAggregate, FrozenEvidenceSource, Result, TeleloomError
from .mutations import MessageOperation, MessageState
from .output_budget import bounded_content, measured
from .projection import project, resolve_fields
from .runtime import AdapterFactory, Runtime
from .tools import EvidenceKeys, OutputBudget, PageLimit, ResponseFields, ResponsePreset, ToolResult
from .tools.media import register_media


def output(
    data: dict[str, Any] | None = None, error: TeleloomError | None = None
) -> CallToolResult:
    result = Result(data=data or {})
    if error:
        result.ok = False
        result.error = ErrorInfo(
            code=error.code,
            message=error.message,
            retryable=error.retry_after is not None,
            retry_after=error.retry_after,
            details=error.details,
        )
    return CallToolResult(
        content=[TextContent(type="text", text=result.model_dump_json())],
        structuredContent=result.model_dump(mode="json"),
        isError=bool(error),
    )


def create_server(
    settings: Settings,
    adapter_factory: AdapterFactory = make_adapter,
    runtime: Runtime | None = None,
) -> FastMCP:
    runtime = runtime or Runtime(settings, adapter_factory)
    from .field_selection import FieldSelector

    field_selector = FieldSelector(settings, runtime.store, runtime.credentials)

    server = FastMCP(
        "teleloom",
        host="127.0.0.1",
        port=settings.port,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{settings.port}"],
            allowed_origins=[f"http://127.0.0.1:{settings.port}"],
        ),
    )

    async def read(operation: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(settings.read_timeout_seconds):
                return await runtime.invoke(operation)
        except TimeoutError:
            raise TeleloomError(
                "read_timeout",
                "Telegram read timed out. Retry the read or inspect the connection.",
                retry_after=1,
                details={"timeout_seconds": settings.read_timeout_seconds},
            ) from None

    async def call(
        operation: Callable[[], Awaitable[dict[str, Any]]],
        *,
        bounded_read: bool = False,
        profile_id: str | None = None,
        tool_name: str | None = None,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> CallToolResult:
        try:
            selection = resolve_fields(tool_name, fields, preset) if tool_name else None
            result = await read(operation) if bounded_read else await runtime.invoke(operation)
            with runtime.store.db:
                original = result
                if profile_id is not None:
                    from .rich_reads import scoped_evidence

                    result = scoped_evidence(settings.profile(profile_id), profile_id, result)
                if tool_name and selection:
                    result = project(tool_name, result, selection)
                if max_output_bytes is not None:
                    if (
                        tool_name != "jobs_results"
                        and measured(result, max_output_bytes)["output_budget"][
                            "normalized_data_bytes"
                        ]
                        > max_output_bytes
                    ):
                        assert profile_id is not None and tool_name is not None
                        frozen = runtime.jobs.reading.freeze_direct(profile_id, tool_name, original)
                        result.update(
                            {
                                key: frozen[key]
                                for key in (
                                    "evidence_ref",
                                    "source_version",
                                    "reference_expires_at",
                                    "reference",
                                )
                            }
                        )
                    result = bounded_content(result, max_output_bytes)
            pixels = result.pop("_image", None)
            mime_type = result.pop("_mime_type", None)
            response = output(result)
            if pixels:
                response.content.append(ImageContent(type="image", data=pixels, mimeType=mime_type))
            return response
        except TeleloomError as exc:
            return output(error=exc)

    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
    local_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    read_job = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

    registered: set[str] = set()

    def exposed_tool(*, annotations: ToolAnnotations) -> Callable[[Any], Any]:
        def register(function: Any) -> Any:
            name = function.__name__
            registered.add(name)
            read_workflows = {
                "activity_start",
                "messages_search_many_start",
                "messages_search_local",
                "digest_context_many_start",
                "attachments_read_start",
                "unread_export_start",
                "events_wait_start",
                "transcription_start",
                "media_download",
                "photo_open",
                "media_cleanup",
            }
            enabled = (
                settings.exposure_mode == "all"
                or (settings.exposure_mode == "selected" and name in settings.exposed_tools)
                or (
                    settings.exposure_mode == "read-only"
                    and (
                        annotations.readOnlyHint is True
                        or name in read_workflows
                        or name
                        in {"sync_start", "export_start", "jobs_control", "attachments_cleanup"}
                    )
                )
            )
            return server.tool(annotations=annotations)(function) if enabled else function

        return register

    @exposed_tool(annotations=read_job)
    async def events_wait_start(
        profile_id: str,
        chat_ids: list[str],
        mode: Literal["new", "settled"] = "new",
        timeout_seconds: Annotated[float, Field(ge=0.05, le=3600)] = 30,
        debounce_seconds: Annotated[float, Field(ge=0, le=60)] = 1,
        max_events: Annotated[int, Field(ge=1, le=100)] = 100,
        after_sequence: Annotated[int, Field(ge=0)] | None = None,
        retention_hours: Annotated[int, Field(ge=1, le=168)] = 24,
        filter: EventFilter | None = None,
        cursor: str | None = None,
    ) -> ToolResult:
        """Start a bounded wait on opted-in chats with exact typed observed-event filters. Page jobs_results before continuing with coverage.next_cursor; unknown facts/gaps remain explicit. No callback or acknowledgement."""
        return await call(
            lambda: runtime.events.start(
                profile_id,
                chat_ids,
                mode=mode,
                timeout_seconds=timeout_seconds,
                debounce_seconds=debounce_seconds,
                max_events=max_events,
                after_sequence=after_sequence,
                retention_hours=retention_hours,
                filter=filter,
                cursor=cursor,
            ),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def transcription_capabilities(profile_id: str) -> ToolResult:
        """Inspect per-profile providers, account restrictions, owner consent and upload budgets; makes no transcription calls."""

        async def inspect() -> dict[str, Any]:
            return runtime.transcription.capabilities(profile_id)

        return await call(inspect, profile_id=profile_id)

    @exposed_tool(annotations=read_job)
    async def transcription_start(
        profile_id: str,
        chat_id: str,
        message_id: str,
        provider: Literal["local", "telegram", "openai", "groq"] = "local",
        allow_external_upload: bool = False,
        max_calls: Annotated[int, Field(ge=0, le=1)] = 1,
        max_bytes: Annotated[int, Field(ge=1, le=25_000_000)] = 10_000_000,
        max_characters: Annotated[int, Field(ge=1, le=32000)] = 32000,
        timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 30,
        retention_hours: Annotated[int, Field(ge=1, le=168)] = 24,
    ) -> ToolResult:
        """Transcribe one selected message in a bounded durable job. External AUDIO upload needs owner opt-in and explicit call consent/budget. Cache is untrusted enrichment; no automatic paid retries."""
        return await call(
            lambda: runtime.transcription.start(
                profile_id,
                chat_id,
                message_id,
                provider=provider,
                allow_external_upload=allow_external_upload,
                max_calls=max_calls,
                max_bytes=max_bytes,
                max_characters=max_characters,
                timeout_seconds=timeout_seconds,
                retention_hours=retention_hours,
            ),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def administration_read(
        profile_id: str,
        operation: AdministrationRead,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
    ) -> ToolResult:
        """Read exact group/channel metadata, participants/admins/bans, member rights, audit or common chats. Selected participant reads never grant send permissions."""
        return await call(
            lambda: runtime.administration.read(profile_id, operation),
            bounded_read=True,
            tool_name="administration_read",
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def account_read(
        profile_id: str,
        operation: AccountRead,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
    ) -> ToolResult:
        """Read self/user/bot profile, status, avatar history, owner-enabled privacy or bot commands; never returns phone numbers/access hashes."""
        return await call(
            lambda: runtime.administration.read(profile_id, operation, account=True),
            bounded_read=True,
            tool_name="account_read",
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=read_job)
    async def administration_preview(
        profile_id: str, operation: AdministrationOperation
    ) -> ToolResult:
        """Preview exact confirmed group/channel/forum/admin/invite changes under the owner's groups management scope. Review all targets and before-state, then use delivery_execute; exported invites are writes."""
        return await call(
            lambda: runtime.management.preview(profile_id, operation),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=read_job)
    async def account_preview(profile_id: str, operation: AccountOperation) -> ToolResult:
        """Preview profile/privacy/photo or exact authenticated-bot commands under owner account permission. Review every field/file hash, then use delivery_execute with explicit confirmation."""
        return await call(
            lambda: runtime.management.preview(profile_id, operation),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def server_status(
        profile_id: str | None = None,
        chat_id: str | None = None,
        timeout_seconds: Annotated[float, Field(ge=0.05, le=10)] = 5,
    ) -> ToolResult:
        """Inspect local owner/build and effective scope. Explicit profile_id + chat_id opts into one bounded user history read; no content, login, acknowledgment or delivery. Local/MCP status is not live Telegram health."""
        from .diagnostics import owner_scope, selected_probe

        info: dict[str, Any] = {
            "version": __version__,
            "build": build_info(),
            "owner_id": runtime.owner_id,
            "profiles": len(settings.profiles),
            "health": {"local": "ok", "mcp": "ok", "telegram": "not_checked"},
            "scope": owner_scope(settings, runtime),
        }
        if profile_id is not None or chat_id is not None:
            info["probe"] = await selected_probe(runtime, profile_id, chat_id, timeout_seconds)
            info["health"]["telegram"] = (
                "selected_read_ok" if info["probe"]["status"] == "ok" else "selected_read_failed"
            )
            info["scope"] = owner_scope(settings, runtime)
        return output(info)

    @exposed_tool(annotations=readonly)
    async def profiles_list() -> ToolResult:
        """List identities, connection states, and explicit user/bot capabilities."""
        return output(runtime.profiles())

    @exposed_tool(annotations=readonly)
    async def folders_list(profile_id: str) -> ToolResult:
        """Read user folder definitions: explicit members and dynamic rules, never access hashes."""
        return await call(
            lambda: runtime.folders(profile_id), bounded_read=True, profile_id=profile_id
        )

    @exposed_tool(annotations=readonly)
    async def folders_snapshot(profile_id: str, folder_id: str | None = None) -> ToolResult:
        """Read full ordered folder definitions, title entities and stale guards; system/shared restrictions are explicit."""
        return await call(
            lambda: runtime.folder_operations.snapshot(profile_id, folder_id),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def folder_limits(profile_id: str) -> ToolResult:
        """Read account/Premium and current Telegram folder limits; unavailable limits stay unknown."""
        return await call(
            lambda: runtime.folder_operations.limits(profile_id),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def folder_preview(profile_id: str, operation: FolderOperation) -> ToolResult:
        """Preview an immutable revision-bound folder change; execute only through delivery_execute confirmation."""
        return await call(
            lambda: runtime.folder_operations.preview(profile_id, operation),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def contacts_list(
        profile_id: str,
        view: Literal["records", "ids", "export"] = "records",
        cursor: str | None = None,
        limit: PageLimit = 50,
    ) -> ToolResult:
        """Read saved contacts, IDs or a safe contact export; excludes stored private phones."""
        return await call(
            lambda: runtime.contacts.list(profile_id, view, cursor, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def contacts_preview(profile_id: str, operation: ContactOperation) -> ToolResult:
        """Preview an immutable typed account contact change; execute with delivery_execute after explicit confirmation."""
        return await call(
            lambda: runtime.contacts.preview(profile_id, operation),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def contacts_search(
        profile_id: str, query: str, cursor: str | None = None, limit: PageLimit = 50
    ) -> ToolResult:
        """Search Telegram contacts/usernames; exact aliases and fuzzy suggestions are separate evidence."""
        return await call(
            lambda: runtime.contacts.search(profile_id, query, cursor, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def contacts_blocked(
        profile_id: str, cursor: str | None = None, limit: PageLimit = 50
    ) -> ToolResult:
        """Read a bounded frozen blocked-peer snapshot without stored phone numbers."""
        return await call(
            lambda: runtime.contacts.blocked(profile_id, cursor, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def contacts_direct(
        profile_id: str, query: str, cursor: str | None = None, limit: PageLimit = 50
    ) -> ToolResult:
        """Find matching direct dialogs; multiple matches never select a recipient."""
        return await call(
            lambda: runtime.contacts.direct(profile_id, query, cursor, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def contact_chats(
        profile_id: str, contact_id: str, cursor: str | None = None, limit: PageLimit = 50
    ) -> ToolResult:
        """Read the exact contact's direct and common chats in a fixed bounded snapshot."""
        return await call(
            lambda: runtime.contacts.chats(profile_id, contact_id, cursor, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def contact_interactions(
        profile_id: str, contact_id: str, limit: Annotated[int, Field(ge=1, le=100)] = 5
    ) -> ToolResult:
        """Read latest direct interactions with incoming/outgoing original message attribution."""
        return await call(
            lambda: runtime.contacts.interactions(profile_id, contact_id, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def contact_aliases_list(
        profile_id: str, cursor: str | None = None, limit: PageLimit = 50
    ) -> ToolResult:
        """List the selected profile's exact local aliases; names never imply fuzzy target selection."""
        return await call(
            lambda: runtime.contacts.alias_list(profile_id, cursor, limit),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def contact_alias_set(
        profile_id: str, alias: str, chat_id: str, replace: bool = False
    ) -> ToolResult:
        """Save an exact alias to a canonical peer. Changing its target requires replace=true."""
        return await call(
            lambda: runtime.contacts.alias_set(profile_id, alias, chat_id, replace),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def contact_alias_delete(profile_id: str, alias: str) -> ToolResult:
        """Remove one exact alias from this profile's local state."""
        return await call(
            lambda: runtime.contacts.alias_delete(profile_id, alias), profile_id=profile_id
        )

    @exposed_tool(annotations=readonly)
    async def chats_list(
        profile_id: str,
        cursor: str | None = None,
        limit: PageLimit = 50,
        folder_id: str | None = None,
        kind: Literal["channel", "group", "private"] | None = None,
        unread_only: bool = False,
        unmuted_only: bool = False,
        archived: bool | None = None,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
    ) -> ToolResult:
        """List user dialogs or bot-observed chats. Bot coverage is limited to collected updates."""
        return await call(
            lambda: runtime.chat_list(
                profile_id,
                cursor,
                limit,
                folder_id,
                kind,
                unread_only=unread_only,
                unmuted_only=unmuted_only,
                archived=archived,
            ),
            bounded_read=True,
            tool_name="chats_list",
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def folder_members(
        profile_id: str,
        folder_id: str,
        cursor: str | None = None,
        limit: PageLimit = 50,
        kind: Literal["channel", "group", "private"] | None = None,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
    ) -> ToolResult:
        """Evaluate a Telegram folder; paginate its frozen membership and report inaccessible peers."""
        return await call(
            lambda: runtime.folder_members(profile_id, folder_id, cursor, limit, kind),
            bounded_read=True,
            tool_name="folder_members",
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def chat_resolve(
        profile_id: str,
        target: str,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
    ) -> ToolResult:
        """Resolve an ID, @username or exact observed title. Ambiguous titles never select a recipient."""
        return await call(
            lambda: runtime.resolve(profile_id, target),
            bounded_read=True,
            tool_name="chat_resolve",
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def topics_list(
        profile_id: str,
        chat_id: str,
        cursor: str | None = None,
        limit: PageLimit = 50,
        query: Annotated[str, Field(max_length=256)] | None = None,
    ) -> ToolResult:
        """List forum topics in a bounded snapshot; bots expose only observed topics."""
        return await call(
            lambda: runtime.topics(profile_id, chat_id, cursor, limit, query),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def topic_history(
        profile_id: str,
        chat_id: str,
        topic_id: str,
        cursor: str | None = None,
        limit: PageLimit = 50,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Read one forum topic, preserving topic and message identities."""
        return await call(
            lambda: runtime.thread_page(profile_id, chat_id, topic_id, "topic", cursor, limit),
            bounded_read=True,
            tool_name="topic_history",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def thread_get(
        profile_id: str,
        chat_id: str,
        root_message_id: str,
        cursor: str | None = None,
        limit: PageLimit = 50,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Read replies belonging to one root; missing originals remain explicit."""
        return await call(
            lambda: runtime.thread_page(
                profile_id, chat_id, root_message_id, "thread", cursor, limit
            ),
            bounded_read=True,
            tool_name="thread_get",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def comments_get(
        profile_id: str,
        chat_id: str,
        message_id: str,
        cursor: str | None = None,
        limit: PageLimit = 50,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Map a channel post to its discussion root and read only its comments."""
        return await call(
            lambda: runtime.thread_page(profile_id, chat_id, message_id, "comments", cursor, limit),
            bounded_read=True,
            tool_name="comments_get",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def messages_pinned(
        profile_id: str,
        chat_id: str,
        cursor: str | None = None,
        limit: PageLimit = 50,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Read pinned posts with safe author metadata; bot pins reflect saved updates."""
        return await call(
            lambda: runtime.thread_page(profile_id, chat_id, None, "pinned", cursor, limit),
            bounded_read=True,
            tool_name="messages_pinned",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=read_job)
    async def activity_start(
        profile_id: str,
        folder_id: str | None = None,
        chat_ids: list[str] | None = None,
        kind: Literal["channel", "group", "private"] | None = None,
        top: Annotated[int, Field(ge=1, le=100)] = 5,
        max_requests: Annotated[int, Field(ge=1, le=1000)] = 200,
        max_duration_seconds: Annotated[int | None, Field(ge=1, le=86400)] = None,
    ) -> ToolResult:
        """Freeze selected chats and start a resumable comparison of their last actual posts."""

        async def start() -> dict[str, Any]:
            selection = await read(lambda: runtime.selection(profile_id, folder_id, chat_ids, kind))
            return await runtime.jobs.activity_start(
                profile_id,
                selection,
                top=top,
                max_requests=max_requests,
                max_duration_seconds=max_duration_seconds,
            )

        return await call(start, profile_id=profile_id)

    @exposed_tool(annotations=read_job)
    async def messages_search_many_start(
        profile_id: str,
        since: Annotated[
            datetime,
            Field(description="Inclusive publication-time bound; ISO 8601 with Z or a UTC offset."),
        ],
        until: Annotated[
            datetime,
            Field(
                description="Exclusive publication-time bound; ISO 8601 with Z or a UTC offset, later than since."
            ),
        ],
        query: str,
        folder_id: str | None = None,
        chat_ids: list[str] | None = None,
        max_messages: Annotated[
            int,
            Field(
                ge=1,
                le=10000,
                description="Collected-message budget across all selected chats; 1..10000.",
            ),
        ] = 1000,
        max_characters: Annotated[
            int,
            Field(
                ge=1,
                le=1000000,
                description="Collected text-character budget across all selected chats; 1..1000000 (not response bytes).",
            ),
        ] = 100000,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="Logical history/search batch-attempt budget across all selected chats; 1..1000, not measured Telegram RPCs.",
            ),
        ] = 200,
        max_duration_seconds: Annotated[int | None, Field(ge=1, le=86400)] = None,
    ) -> ToolResult:
        """Start bounded multi-chat search; retrieve frozen evidence pages with jobs_results. Shared collection limits: max_messages 1..10000, max_characters 1..1000000, max_requests 1..1000. These apply across all selected chats, not per chat; max_characters counts text characters, not response bytes."""

        async def start() -> dict[str, Any]:
            selection = await read(lambda: runtime.selection(profile_id, folder_id, chat_ids))
            return await runtime.jobs.evidence_start(
                profile_id,
                selection,
                since=since,
                until=until,
                query=query,
                max_messages=max_messages,
                max_characters=max_characters,
                max_requests=max_requests,
                max_duration_seconds=max_duration_seconds,
            )

        return await call(start, profile_id=profile_id)

    @exposed_tool(annotations=read_job)
    async def digest_context_many_start(
        profile_id: str,
        since: Annotated[
            datetime,
            Field(description="Inclusive publication-time bound; ISO 8601 with Z or a UTC offset."),
        ],
        until: Annotated[
            datetime,
            Field(
                description="Exclusive publication-time bound; ISO 8601 with Z or a UTC offset, later than since."
            ),
        ],
        folder_id: str | None = None,
        chat_ids: list[str] | None = None,
        max_messages: Annotated[
            int,
            Field(
                ge=1,
                le=10000,
                description="Collected-message budget across all selected chats; 1..10000.",
            ),
        ] = 1000,
        max_characters: Annotated[
            int,
            Field(
                ge=1,
                le=1000000,
                description="Collected text-character budget across all selected chats; 1..1000000 (not response bytes).",
            ),
        ] = 100000,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="Logical history/search batch-attempt budget across all selected chats; 1..1000, not measured Telegram RPCs.",
            ),
        ] = 200,
        max_duration_seconds: Annotated[int | None, Field(ge=1, le=86400)] = None,
    ) -> ToolResult:
        """Collect source material across selected chats; the agent writes the summary, without implicit Jev. Shared collection limits: max_messages 1..10000, max_characters 1..1000000, max_requests 1..1000. These apply across all selected chats, not per chat; max_characters counts text characters, not response bytes."""

        async def start() -> dict[str, Any]:
            selection = await read(lambda: runtime.selection(profile_id, folder_id, chat_ids))
            return await runtime.jobs.evidence_start(
                profile_id,
                selection,
                since=since,
                until=until,
                max_messages=max_messages,
                max_characters=max_characters,
                max_requests=max_requests,
                max_duration_seconds=max_duration_seconds,
            )

        return await call(start, profile_id=profile_id)

    @exposed_tool(annotations=read_job)
    async def unread_export_start(
        profile_id: str,
        chat_ids: Annotated[list[str], Field(min_length=1, max_length=200)],
        format: Literal["jsonl", "markdown"] = "jsonl",
        max_messages: Annotated[int, Field(ge=1, le=10000)] = 1000,
        max_requests: Annotated[int, Field(ge=1, le=10000)] = 200,
        max_bytes: Annotated[int, Field(ge=1, le=100000000)] = 20000000,
        max_duration_seconds: Annotated[int | None, Field(ge=1, le=86400)] = None,
    ) -> ToolResult:
        """Export a frozen exact unread selection to a private job-owned file. Resume with jobs_control; inspect per-chat coverage via jobs_status/results. Never acknowledges or sends; bot evidence covers saved pending updates only."""
        return await call(
            lambda: runtime.jobs.reading.unread_start(
                profile_id,
                chat_ids,
                format,
                max_messages=max_messages,
                max_requests=max_requests,
                max_duration_seconds=max_duration_seconds,
                max_bytes=max_bytes,
            ),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def jobs_results(
        profile_id: str,
        job_id: str | None = None,
        cursor: Annotated[
            str | None,
            Field(
                description="Result-page next_cursor, without evidence_ref or message_keys. Keep job_id. Coverage and aggregate views take no page cursor."
            ),
        ] = None,
        limit: PageLimit = 50,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        evidence_ref: Annotated[
            str | None,
            Field(
                max_length=128,
                description="Opaque frozen-snapshot reference, without cursor. Use for exact message_keys, coverage or aggregates; it is distinct from next_cursor, grants no access and never triggers a live reread.",
            ),
        ] = None,
        message_keys: EvidenceKeys | None = None,
        view: Literal["messages", "coverage", "aggregate"] = "messages",
        coverage: Literal["full", "compact"] = "full",
        aggregate: EvidenceAggregate | None = None,
        max_output_bytes: OutputBudget | None = None,
        original_field: Annotated[
            Literal["record", "text", "original_text", "rich_text", "transcript"],
            Field(
                description="Read one exact frozen original as bounded compact JSON chunks. Concatenate content.value chunks and JSON-decode once; record includes the complete normalized source record."
            ),
        ]
        | None = None,
        content_cursor: Annotated[
            str,
            Field(
                max_length=1024,
                description="Opaque original-content continuation; retain evidence_ref, the single exact message key and original_field. Distinct from the result-page cursor.",
            ),
        ]
        | None = None,
    ) -> ToolResult:
        """Read frozen evidence pages, coverage, exact originals or original JSON chunks. Never combine cursor and evidence_ref: paginate with job_id + cursor; retrieve originals with evidence_ref + message_keys, without cursor. Direct excerpt references omit job_id; job references keep it. Opt into coverage=compact for counts/warnings and a full-details reference; full is the default. view=coverage omits originals. Typed aggregates require a terminal reading job and no cursor or message_keys; timezone accepts UTC, fixed offsets (+04:00), or IANA names (Indian/Mauritius). Observed counts are not Telegram totals. References never grant access or trigger live refetch."""
        return await call(
            lambda: runtime.jobs.results(
                profile_id,
                job_id,
                cursor,
                limit,
                evidence_ref=evidence_ref,
                message_keys=message_keys,
                view=view,
                coverage=coverage,
                aggregate=aggregate,
                max_output_bytes=max_output_bytes,
                original_field=original_field,
                content_cursor=content_cursor,
                output_projection=(
                    resolve_fields("jobs_results", fields, preset).metadata()
                    if fields is not None or preset not in {None, "full"}
                    else None
                ),
            ),
            tool_name="jobs_results",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def attachment_capabilities(
        profile_id: str, transcription_model_path: str | None = None
    ) -> ToolResult:
        """Report installed local extractors and exact setup requirements; never download models."""

        async def capabilities() -> dict[str, Any]:
            settings.profile(profile_id)
            return runtime.attachments.capabilities(transcription_model_path)

        return await call(capabilities, profile_id=profile_id)

    @exposed_tool(annotations=read_job)
    async def attachments_read_start(
        profile_id: str,
        chat_id: str,
        message_ids: list[str],
        max_bytes: Annotated[int, Field(ge=1, le=100000000)] = 10000000,
        max_characters: Annotated[int, Field(ge=1, le=500000)] = 50000,
        timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 30,
        retention_hours: Annotated[int, Field(ge=1, le=168)] = 24,
        transcription_model_path: str | None = None,
    ) -> ToolResult:
        """Read only selected attachments in a private durable job with local extraction and retention."""
        return await call(
            lambda: runtime.attachments.start(
                profile_id,
                chat_id,
                message_ids,
                max_bytes=max_bytes,
                max_characters=max_characters,
                timeout_seconds=timeout_seconds,
                retention_hours=retention_hours,
                transcription_model_path=transcription_model_path,
            ),
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def attachments_cleanup(profile_id: str, job_id: str | None = None) -> ToolResult:
        """Delete attachment files and extracted content owned by this profile/job."""
        return await call(
            lambda: runtime.attachments.cleanup(profile_id, job_id), profile_id=profile_id
        )

    @exposed_tool(annotations=readonly)
    async def messages_get(
        profile_id: str,
        chat_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: PageLimit = 50,
        source: Literal["live", "index"] = "live",
        message_ids: list[str] | None = None,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Read date-bounded messages or explicit reply-context IDs. Dates require timezone; end is exclusive."""
        return await call(
            lambda: runtime.history(
                profile_id,
                chat_id,
                since=since,
                until=until,
                cursor=cursor,
                limit=limit,
                source=source,
                message_ids=message_ids,
            ),
            bounded_read=True,
            tool_name="messages_get",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def messages_search(
        profile_id: str,
        chat_id: str,
        query: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: PageLimit = 50,
        source: Literal["live", "index"] = "live",
        sender_id: str | None = None,
        max_requests: Annotated[int, Field(ge=1, le=10)] = 3,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Search one readable chat by text and/or exact canonical sender_id. Sender filtering scans bounded batches; empty pages can have continuation. Names/signatures are not identities; bots search saved updates only."""
        return await call(
            lambda: runtime.history(
                profile_id,
                chat_id,
                since=since,
                until=until,
                cursor=cursor,
                limit=limit,
                query=query,
                source=source,
                sender_id=sender_id,
                max_requests=max_requests,
            ),
            bounded_read=True,
            tool_name="messages_search",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def messages_search_local(
        profile_id: str,
        chat_ids: list[str],
        query: str,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: PageLimit = 20,
        max_hits: Annotated[int, Field(ge=1, le=1000)] = 200,
        snippet_characters: Annotated[int, Field(ge=32, le=1000)] = 240,
    ) -> ToolResult:
        """Search only explicitly selected readable local chats using FTS5 literal AND. Freeze ranked hits and original versions with an owned evidence_ref; use jobs_results for exact originals. No implicit sync, Telegram history or AI upload. Index freshness/gaps and saved-only bot limits remain explicit."""

        async def search() -> dict[str, Any]:
            return runtime.jobs.reading.local_search(
                profile_id,
                chat_ids,
                query,
                since=since,
                until=until,
                cursor=cursor,
                limit=limit,
                max_hits=max_hits,
                snippet_characters=snippet_characters,
            )

        return await call(search, bounded_read=True, profile_id=profile_id)

    @exposed_tool(annotations=readonly)
    async def chats_search(
        profile_id: str,
        query: str,
        scope: Literal["dialogs", "public"] = "dialogs",
        cursor: str | None = None,
        limit: PageLimit = 50,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
    ) -> ToolResult:
        """Find dialogs by observed title/username or search Telegram public peers (user only). Selected policies search allowed peers. Results keep exact IDs; discovery does not grant mutation permissions."""
        return await call(
            lambda: runtime.chat_search(profile_id, query, scope, cursor, limit),
            bounded_read=True,
            tool_name="chats_search",
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def context_get(
        profile_id: str,
        chat_id: str,
        message_id: str,
        context_size: Annotated[int, Field(ge=0, le=20)] = 3,
        include_replies: bool = True,
        max_reply_chats: Annotated[int, Field(ge=1, le=10)] = 5,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Read a central message and nearest neighbors on both sides, plus bounded exact reply context. Deleted ID gaps do not consume neighbors. Bots expose saved updates only; no read acknowledgment."""
        return await call(
            lambda: runtime.message_context(
                profile_id, chat_id, message_id, context_size, include_replies, max_reply_chats
            ),
            bounded_read=True,
            tool_name="context_get",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def messages_search_global(
        profile_id: str,
        query: str,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: PageLimit = 50,
        max_requests: Annotated[int, Field(ge=1, le=10)] = 3,
        kind: Literal["channel", "group", "private"] | None = None,
        sender_id: str | None = None,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Bounded Telegram-wide user search with exact UTC bounds. Selected read policies search only allowed peers. Cursors bind query, generation, policy and budgets for 15 minutes; empty pages may have a continuation."""
        return await call(
            lambda: runtime.global_search(
                profile_id,
                query,
                since=since,
                until=until,
                cursor=cursor,
                limit=limit,
                max_requests=max_requests,
                kind=kind,
                sender_id=sender_id,
            ),
            bounded_read=True,
            tool_name="messages_search_global",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def inbox_get(
        profile_id: str,
        limit: PageLimit = 20,
        per_chat_limit: PageLimit = 20,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Review Telegram unread (user) or locally unprocessed updates (bot), without acknowledging."""
        return await call(
            lambda: runtime.inbox(profile_id, limit, per_chat_limit),
            bounded_read=True,
            tool_name="inbox_get",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=write)
    async def inbox_ack(
        profile_id: str,
        chat_id: str,
        through_message_id: str,
        snapshot_id: str | None = None,
        plan_id: str | None = None,
        plan_hash: str | None = None,
        confirmed: bool = False,
    ) -> ToolResult:
        """Acknowledge through an owner-reviewed message; user profiles change Telegram read state."""

        async def acknowledge() -> dict[str, Any]:
            if settings.profile(profile_id).kind == "bot":
                return await runtime.acknowledge(
                    profile_id, chat_id, through_message_id, snapshot_id
                )
            if not plan_id or not plan_hash:
                raise TeleloomError(
                    "confirmation_required",
                    "Preview a read_ack operation and confirm its immutable plan before acknowledging Telegram.",
                )
            plan = runtime.jobs._owned(profile_id, plan_id, "plans")
            if plan["payload"].get("operation") != {
                "kind": "read_ack",
                "chat_id": chat_id,
                "through_message_id": through_message_id,
            }:
                raise TeleloomError(
                    "plan_changed", "The acknowledgment target does not match the immutable plan."
                )
            return await runtime.jobs.execute(profile_id, plan_id, plan_hash, confirmed)

        return await call(acknowledge, profile_id=profile_id)

    @exposed_tool(annotations=readonly)
    async def digest_context(
        profile_id: str,
        chat_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: PageLimit = 50,
        source: Literal["live", "index"] = "live",
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Return original evidence for a sourced digest written by the agent; never generate a summary."""
        return await call(
            lambda: runtime.history(
                profile_id,
                chat_id,
                since=since,
                until=until,
                cursor=cursor,
                limit=limit,
                source=source,
            ),
            bounded_read=True,
            tool_name="digest_context",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def sync_start(
        profile_id: str, chat_id: str, since: datetime | None = None, until: datetime | None = None
    ) -> ToolResult:
        """Start resumable history sync for a CLI-selected chat; defaults to the last 30 days."""
        return await call(
            lambda: runtime.jobs.sync_start(profile_id, chat_id, since, until),
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def export_start(
        profile_id: str,
        chat_id: str | None = None,
        format: Literal["jsonl", "markdown"] = "jsonl",
        since: datetime | None = None,
        until: datetime | None = None,
        source: FrozenEvidenceSource | None = None,
    ) -> ToolResult:
        """Export an owned frozen evidence source or a legacy chat index into a private job-owned JSONL/Markdown file. Frozen source excludes chat/date filters, needs no sync grant and makes no history calls. Resume with jobs_control; jobs_status exposes the completed path after current policy/expiry checks. Copied bytes are outside server revocation."""
        return await call(
            lambda: runtime.jobs.export_start(profile_id, chat_id, format, since, until, source),
            profile_id=profile_id,
        )

    @exposed_tool(annotations=local_write)
    async def delivery_preview(
        profile_id: str,
        recipients: list[str],
        text: str,
        reply_to_message_id: str | None = None,
        broadcast: bool = False,
    ) -> ToolResult:
        """Create an immutable allowlisted preview. Show full content/recipients to the owner before execution."""
        return await call(
            lambda: runtime.jobs.preview(
                profile_id, recipients, text, reply_to_message_id, broadcast
            ),
            profile_id=profile_id,
        )

    @exposed_tool(annotations=write)
    async def delivery_execute(
        profile_id: str, plan_id: str, plan_hash: str, confirmed: bool = False
    ) -> ToolResult:
        """Execute a hash-matching plan after owner confirmation. The client is trusted to obtain that confirmation."""
        return await call(
            lambda: runtime.jobs.execute(profile_id, plan_id, plan_hash, confirmed),
            profile_id=profile_id,
        )

    @exposed_tool(annotations=read_job)
    async def message_operation_preview(profile_id: str, operation: MessageOperation) -> ToolResult:
        """Preview a typed exact-chat send/reply/edit/delete/forward/reaction/poll/pin/ack/draft/schedule/inline operation. Confirm the complete preview, then call delivery_execute. send permission never grants edit/admin permission."""
        return await call(
            lambda: runtime.jobs.operation_preview(profile_id, operation),
            bounded_read=True,
            profile_id=profile_id,
        )

    @exposed_tool(annotations=readonly)
    async def message_state(
        profile_id: str,
        kind: MessageState,
        chat_id: str | None = None,
        message_id: str | None = None,
        limit: PageLimit = 50,
        query: Annotated[str, Field(max_length=256)] = "",
        bot_id: str | None = None,
        cursor: str | None = None,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
    ) -> ToolResult:
        """Read drafts, schedules, inline buttons/results, send-as peers, reactions or receipts. Drafts without chat_id use scoped account-wide snapshot pagination; other reads require an exact chat. Backend restrictions are explicit errors; reads never acknowledge or send."""

        async def state() -> dict[str, Any]:
            from .runtime import number

            if chat_id is None and kind == "drafts":
                return await runtime.drafts(profile_id, limit, cursor)
            if chat_id is None:
                raise TeleloomError("chat_required", "This query requires an exact chat_id.")
            if cursor:
                raise TeleloomError(
                    "invalid_cursor", "Only account-wide drafts accept this cursor."
                )
            number(chat_id)
            config = settings.profile(profile_id)
            config.require_read(chat_id)
            if message_id:
                number(message_id, positive=True)
            if bot_id:
                number(bot_id)
                config.require_read(bot_id)
            adapter = await runtime.adapter(profile_id)
            return {
                "profile_id": profile_id,
                "chat_id": chat_id,
                "kind": kind,
                **await adapter.message_state(chat_id, kind, message_id, limit, query, bot_id),
            }

        return await call(
            state,
            bounded_read=True,
            profile_id=profile_id,
            tool_name="message_state",
            fields=fields,
            preset=preset,
        )

    register_media(
        runtime, call, exposed_tool, readonly=readonly, read_job=read_job, local_write=local_write
    )

    @exposed_tool(annotations=readonly)
    async def jobs_status(profile_id: str, job_id: str | None = None) -> ToolResult:
        """Inspect job status and receipts; unknown outcomes must be reconciled, never automatically replayed."""

        return await call(lambda: runtime.jobs.status(profile_id, job_id), profile_id=profile_id)

    @exposed_tool(annotations=local_write)
    async def jobs_control(
        profile_id: str, job_id: str, action: Literal["pause", "resume", "cancel"]
    ) -> ToolResult:
        """Pause/resume/cancel an owned job; cancel also removes terminal frozen exports or retries their journalled cleanup without replay. Delivered messages and unknown outcomes remain intact."""
        return await call(
            lambda: runtime.jobs.control(profile_id, job_id, action), profile_id=profile_id
        )

    @exposed_tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    )
    async def messages_classify(
        profile_id: str,
        chat_id: str,
        message_ids: list[str],
        query: str = "",
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        max_output_bytes: OutputBudget | None = None,
    ) -> ToolResult:
        """Optional Jev judgments for permitted chats; original evidence survives unavailable or disabled analysis."""

        async def classify() -> dict[str, Any]:
            if not message_ids or len(message_ids) > 20:
                raise TeleloomError("invalid_limit", "Provide 1–20 explicit message IDs.")
            deadline = asyncio.get_running_loop().time() + settings.read_timeout_seconds
            evidence = await read(
                lambda: runtime.history(profile_id, chat_id, message_ids=message_ids, limit=100)
            )
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            try:
                async with asyncio.timeout(remaining):
                    return await runtime.analysis.classify(profile_id, chat_id, evidence, query)
            except TimeoutError:
                return {
                    "evidence": evidence,
                    "judgments": None,
                    "analysis_status": "unavailable",
                    "reason": "Jev analysis timed out; use original evidence.",
                }

        return await call(
            classify,
            tool_name="messages_classify",
            max_output_bytes=max_output_bytes,
            fields=fields,
            preset=preset,
            profile_id=profile_id,
        )

    @exposed_tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    )
    async def response_fields_select(
        tool_name: str,
        request: str,
        fields: ResponseFields | None = None,
        preset: ResponsePreset | None = None,
        use_jev: bool = False,
        detail: Literal["full", "compact"] = "full",
    ) -> ToolResult:
        """Choose response fields from static schema and owner task only. Jev is optional;
        never include Telegram text in request. Pass returned fields to a supported
        reader; source identities and coverage always survive projection. detail=compact
        returns the same decision without the per-field reason map; full stays default.
        """
        return await call(
            lambda: field_selector.select(tool_name, request, fields, preset, use_jev, detail)
        )

    unknown = set(settings.exposed_tools) - registered
    if settings.exposure_mode == "selected" and unknown:
        raise TeleloomError(
            "invalid_exposure", "Unknown selected tool names: " + ", ".join(sorted(unknown))
        )
    return server
