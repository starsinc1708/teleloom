"""Complete folder evidence and revision-bound typed folder changes."""

from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)
from telethon import errors, functions, types, utils

from .models import TeleloomError, utcnow

if TYPE_CHECKING:
    from .runtime import Runtime

RULES = (
    "contacts",
    "non_contacts",
    "groups",
    "broadcasts",
    "bots",
    "exclude_muted",
    "exclude_read",
    "exclude_archived",
)
ENTITY_CLASSES = {
    "bold": types.MessageEntityBold,
    "italic": types.MessageEntityItalic,
    "underline": types.MessageEntityUnderline,
    "strike": types.MessageEntityStrike,
    "spoiler": types.MessageEntitySpoiler,
    "code": types.MessageEntityCode,
    "pre": types.MessageEntityPre,
    "custom_emoji": types.MessageEntityCustomEmoji,
    "url": types.MessageEntityUrl,
    "text_url": types.MessageEntityTextUrl,
    "mention": types.MessageEntityMention,
    "mention_name": types.MessageEntityMentionName,
    "email": types.MessageEntityEmail,
    "phone": types.MessageEntityPhone,
    "hashtag": types.MessageEntityHashtag,
    "cashtag": types.MessageEntityCashtag,
    "bot_command": types.MessageEntityBotCommand,
    "bank_card": types.MessageEntityBankCard,
    "unknown": types.MessageEntityUnknown,
}


class TitleEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "bold",
        "italic",
        "underline",
        "strike",
        "spoiler",
        "code",
        "pre",
        "custom_emoji",
        "url",
        "text_url",
        "mention",
        "mention_name",
        "email",
        "phone",
        "hashtag",
        "cashtag",
        "bot_command",
        "bank_card",
        "unknown",
    ]
    offset: StrictInt = Field(ge=0)
    length: StrictInt = Field(ge=1)
    document_id: str | None = None
    user_id: str | None = None
    url: str | None = Field(default=None, max_length=2048)
    language: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def fields_match_kind(self) -> Self:
        from .runtime import number

        for field, kind in (
            ("document_id", "custom_emoji"),
            ("user_id", "mention_name"),
            ("url", "text_url"),
            ("language", "pre"),
        ):
            value = getattr(self, field)
            if (value is not None) != (self.kind == kind):
                raise ValueError(f"{field} is required only for {kind} entities.")
            if value is not None and field.endswith("_id"):
                number(value, positive=True)
        return self


class FolderRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = True


class FolderDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    title_entities: list[TitleEntity] = Field(default_factory=list, max_length=20)
    emoticon: str | None = Field(default=None, max_length=32)
    color: StrictInt | None = Field(default=None, ge=-1, le=6)
    title_noanimate: bool | None = None
    included_chat_ids: list[str] = Field(default_factory=list, max_length=1000)
    pinned_chat_ids: list[str] = Field(default_factory=list, max_length=1000)
    excluded_chat_ids: list[str] = Field(default_factory=list, max_length=1000)
    rules: FolderRules = Field(default_factory=FolderRules)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-16-le")) // 2 > 12:
            raise ValueError("Folder title must be nonempty and at most 12 UTF-16 units.")
        return value

    @field_validator("included_chat_ids", "pinned_chat_ids", "excluded_chat_ids")
    @classmethod
    def exact_peers(cls, values: list[str]) -> list[str]:
        from .runtime import number

        if len(values) != len(set(values)):
            raise ValueError("Use unique canonical peer IDs.")
        for value in values:
            number(value)
        return values

    @model_validator(mode="after")
    def entity_boundaries_and_exceptions(self) -> Self:
        boundaries = {0}
        units = 0
        for char in self.title:
            units += len(char.encode("utf-16-le")) // 2
            boundaries.add(units)
        if any(
            entity.offset not in boundaries or entity.offset + entity.length not in boundaries
            for entity in self.title_entities
        ):
            raise ValueError("Title entities must fit complete UTF-16 character boundaries.")
        if (set(self.included_chat_ids) | set(self.pinned_chat_ids)) & set(self.excluded_chat_ids):
            raise ValueError("Included or pinned peers cannot also be excluded.")
        return self


class FolderChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class FolderTarget(FolderChange):
    folder_id: str

    @field_validator("folder_id")
    @classmethod
    def custom_id(cls, value: str) -> str:
        from .runtime import number

        if not 2 <= number(value, positive=True) < 2**31:
            raise ValueError("Use a custom folder ID in 2..2147483647.")
        return value


class FolderUpdate(FolderTarget):
    kind: Literal["folder_update"]
    definition: FolderDefinition


class FolderCreate(FolderChange):
    kind: Literal["folder_create"]
    folder_id: str | None = None
    definition: FolderDefinition

    @field_validator("folder_id")
    @classmethod
    def custom_id(cls, value: str | None) -> str | None:
        return FolderTarget.custom_id(value) if value is not None else None


class FolderDelete(FolderTarget):
    kind: Literal["folder_delete"]


class FolderReorder(FolderChange):
    kind: Literal["folder_reorder"]
    folder_ids: list[str] = Field(max_length=1000)

    @field_validator("folder_ids")
    @classmethod
    def exact_order(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Provide each custom folder ID exactly once.")
        return [FolderTarget.custom_id(value) for value in values]


class FolderMembership(FolderTarget):
    kind: Literal["folder_add", "folder_remove"]
    chat_id: str
    pinned: bool = False

    @field_validator("chat_id")
    @classmethod
    def exact_peer(cls, value: str) -> str:
        from .runtime import number

        number(value)
        return value


FolderOperation = Annotated[
    FolderCreate | FolderUpdate | FolderDelete | FolderReorder | FolderMembership,
    Field(discriminator="kind"),
]


def title_entity(raw: Any) -> dict[str, Any]:
    kind = next((kind for kind, cls in ENTITY_CLASSES.items() if isinstance(raw, cls)), None)
    if kind is None:
        raise TeleloomError(
            "unsupported_title_entity",
            "This SDK title entity cannot be safely represented for a complete snapshot.",
        )
    item = {"kind": kind, "offset": raw.offset, "length": raw.length}
    for field in ("document_id", "user_id", "url", "language"):
        value = getattr(raw, field, None)
        if value is not None:
            item[field] = str(value) if field.endswith("_id") else value
    return item


async def read_folder_snapshot(adapter: Any) -> dict[str, Any]:
    from .runtime import fingerprint

    response = await adapter.client(functions.messages.GetDialogFiltersRequest())
    if len(response.filters) > 1000:
        raise TeleloomError(
            "folder_limit", "Folder snapshot exceeded the 1000-definition safety budget."
        )
    self_id = None
    if any(
        isinstance(peer, types.InputPeerSelf)
        for raw in response.filters
        for field in ("include_peers", "pinned_peers", "exclude_peers")
        for peer in getattr(raw, field, [])
    ):
        me = await adapter.client.get_me()
        if me is None:
            raise TeleloomError(
                "identity_unavailable",
                "Saved Messages identity is unavailable for a complete folder snapshot.",
            )
        self_id = str(me.id)
    items: list[dict[str, Any]] = []
    for raw in response.filters:
        if isinstance(raw, types.DialogFilterDefault):
            items.append({"id": "0", "type": "system", "editable": False})
            continue
        shared = isinstance(raw, types.DialogFilterChatlist)
        title = raw.title
        definition: dict[str, Any] = {
            "title": title if isinstance(title, str) else title.text,
            "title_entities": [title_entity(entity) for entity in getattr(title, "entities", [])],
            "emoticon": getattr(raw, "emoticon", None),
            "color": getattr(raw, "color", None),
            "title_noanimate": getattr(raw, "title_noanimate", None),
            "rules": {} if shared else {name: bool(getattr(raw, name, False)) for name in RULES},
        }
        for public, sdk in (
            ("included_chat_ids", "include_peers"),
            ("pinned_chat_ids", "pinned_peers"),
            ("excluded_chat_ids", "exclude_peers"),
        ):
            definition[public] = [
                self_id if isinstance(peer, types.InputPeerSelf) else str(utils.get_peer_id(peer))
                for peer in getattr(raw, sdk, [])
            ]
        state: dict[str, Any] = {
            "id": str(raw.id),
            "type": "shared" if shared else "private",
            "editable": not shared,
            "definition": definition,
        }
        if shared:
            state["has_my_invites"] = bool(getattr(raw, "has_my_invites", False))
        canonical = {
            **definition,
            "included_chat_ids": sorted(definition["included_chat_ids"], key=int),
            "excluded_chat_ids": sorted(definition["excluded_chat_ids"], key=int),
        }
        state["revision"] = fingerprint({**state, "definition": canonical})
        items.append(state)
    return {
        "items": items,
        "order": [row["id"] for row in items],
        "revision": fingerprint(items),
        "snapshot_at": utcnow().isoformat(),
        "source": "telegram",
        "incomplete": False,
        "atomic_revision_check": False,
        "warnings": [
            "Telegram has no atomic folder compare-and-swap. Execution checks the approved revision immediately before the mutation."
        ],
    }


async def read_folder_limits(adapter: Any) -> dict[str, Any]:
    me = await adapter.client.get_me()
    premium = bool(getattr(me, "premium", False)) if me is not None else None
    values: dict[str, int] = {}
    available = False
    try:
        response = await adapter.client(functions.help.GetAppConfigRequest(hash=0))
    except errors.FloodWaitError:
        raise
    except errors.RPCError:
        response = None
    if isinstance(response, types.help.AppConfig) and isinstance(response.config, types.JsonObject):
        available = True
        for row in response.config.value:
            value = row.value.value if isinstance(row.value, types.JsonNumber) else None
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
                and value < 2**31
                and int(value) == value
            ):
                values[row.key] = int(value)
    tier = "premium" if premium else "default" if premium is not None else None
    keys = {
        "folders": "dialog_filters_limit",
        "chats_per_folder": "dialog_filters_chats_limit",
        "pinned_per_folder": "dialogs_folder_pinned_limit",
    }
    return {
        "premium": premium,
        "config_available": available,
        "limits": {
            name: values.get(f"{key}_{tier}") if tier else None for name, key in keys.items()
        },
        "config_keys": {name: f"{key}_{tier}" if tier else None for name, key in keys.items()},
        "title_limit_utf16": 12,
        "unknown_limits": "Unavailable limits remain unknown and defer to Telegram server validation.",
    }


class FolderOperations:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime

    def require_user(self, profile_id: str) -> None:
        if self.runtime.settings.profile(profile_id).kind != "user":
            raise TeleloomError("unsupported_capability", "Telegram dialog filters are user-only.")

    async def snapshot(self, profile_id: str, folder_id: str | None = None) -> dict[str, Any]:
        self.require_user(profile_id)
        result = await (await self.runtime.adapter(profile_id)).folder_snapshot()
        if folder_id is not None:
            from .runtime import number

            if folder_id != "0":
                number(folder_id, positive=True)
            result["items"] = [row for row in result["items"] if row["id"] == folder_id]
            if not result["items"]:
                raise TeleloomError("folder_not_found", "Folder ID not found for this profile.")
        config = self.runtime.settings.profile(profile_id)
        for row in result["items"]:
            definition = row.get("definition")
            if not definition:
                continue
            for field in ("included_chat_ids", "pinned_chat_ids", "excluded_chat_ids"):
                selected = [id_ for id_ in definition[field] if config.allows_read(id_)]
                if selected != definition[field]:
                    result["incomplete"] = True
                definition[field] = selected
            selected_entities = [
                entity
                for entity in definition["title_entities"]
                if not entity.get("user_id") or config.allows_read(entity["user_id"])
            ]
            if selected_entities != definition["title_entities"]:
                result["incomplete"] = True
            definition["title_entities"] = selected_entities
        if result["incomplete"]:
            result["warnings"].append(
                "Some folder peer identities fall outside the owner's selected read policy."
            )
        return result

    async def limits(self, profile_id: str) -> dict[str, Any]:
        self.require_user(profile_id)
        return await (await self.runtime.adapter(profile_id)).folder_limits()

    async def preview(self, profile_id: str, operation: FolderOperation) -> dict[str, Any]:
        prepared = await prepare_folder_operation(
            self.runtime, profile_id, operation.model_dump(mode="json")
        )
        return self.runtime.jobs.confirmed_preview(profile_id, **prepared)


async def prepare_folder_operation(
    runtime: "Runtime", profile_id: str, operation: dict[str, Any]
) -> dict[str, Any]:
    config = runtime.settings.profile(profile_id)
    if config.kind != "user":
        raise TeleloomError(
            "unsupported_capability", "Telegram dialog-filter mutations are user-only."
        )
    if "folders" not in config.manage_scopes:
        raise TeleloomError(
            "management_not_allowed",
            "Enable folders through the owner-only profile management CLI before preparing a folder plan.",
        )
    parsed: FolderOperation = TypeAdapter(FolderOperation).validate_python(operation)
    initial_ids = (
        definition_peers(parsed.definition)
        if isinstance(parsed, (FolderCreate, FolderUpdate))
        else {parsed.chat_id}
        if isinstance(parsed, FolderMembership)
        else set()
    )
    for id_ in initial_ids:
        config.require_read(id_)
    adapter = await runtime.adapter(profile_id)
    snapshot = await adapter.folder_snapshot()
    target, definition = folder_change(parsed, snapshot)
    read_chat_ids = set(initial_ids)
    if target and "definition" in target:
        for id_ in definition_peers(FolderDefinition.model_validate(target["definition"])):
            config.require_read(id_)
            read_chat_ids.add(id_)
    peer_ids = definition_peers(definition) if definition else set()
    peers = [await runtime.resolve(profile_id, id_) for id_ in sorted(peer_ids, key=int)]
    if [row["id"] for row in peers] != sorted(peer_ids, key=int):
        raise TeleloomError("peer_identity_changed", "Resolved folder peer identity changed.")
    if definition:
        validate_limits(parsed, definition, snapshot, await adapter.folder_limits())
    after = definition.model_dump(mode="json") if definition else None
    if after and target and target.get("type") == "shared":
        after["rules"] = {}
    resolved: dict[str, Any] = {
        "folder_before": target,
        "folder_after": after,
        "peers": peers,
        "atomic_revision_check": False,
    }
    if isinstance(parsed, FolderReorder):
        resolved = {
            "order_before": snapshot["order"],
            "custom_order_after": parsed.folder_ids,
            "atomic_revision_check": False,
        }
    return {
        "operation": {
            **parsed.model_dump(mode="json"),
            "read_chat_ids": sorted(read_chat_ids | peer_ids, key=int),
        },
        "targets": [{"kind": "account", "profile_id": profile_id, "scope": "folders"}],
        "sources": [],
        "resolved": [resolved],
    }


def definition_peers(definition: FolderDefinition) -> set[str]:
    return (
        set(definition.included_chat_ids)
        | set(definition.pinned_chat_ids)
        | set(definition.excluded_chat_ids)
        | {entity.user_id for entity in definition.title_entities if entity.user_id is not None}
    )


def folder_change(
    operation: FolderOperation, snapshot: dict[str, Any]
) -> tuple[dict[str, Any] | None, FolderDefinition | None]:
    """Bind the exact current revision and compute the approved family-specific change."""
    existing = {row["id"] for row in snapshot["items"] if row["type"] != "system"}
    target = None
    if isinstance(operation, FolderCreate):
        if operation.folder_id is None:
            next_id = 2
            while str(next_id) in existing:
                next_id += 1
            operation.folder_id = str(next_id)
        if operation.folder_id in existing:
            raise TeleloomError(
                "folder_exists", "The planned new folder ID already exists; preview again."
            )
        revision = snapshot["revision"]
        definition = operation.definition
    elif isinstance(operation, FolderReorder):
        if set(operation.folder_ids) != existing:
            raise TeleloomError(
                "invalid_folder_order",
                "Include every existing custom private/shared folder ID exactly once.",
            )
        revision = snapshot["revision"]
        definition = None
    else:
        target = next((row for row in snapshot["items"] if row["id"] == operation.folder_id), None)
        if target is None:
            raise TeleloomError("folder_not_found", "Folder ID not found for this profile.")
        revision = target["revision"]
        if isinstance(operation, FolderUpdate):
            if target["type"] != "private":
                raise TeleloomError(
                    "shared_folder_restricted",
                    "A full-definition update requires a private folder. Shared membership changes remain available.",
                )
            definition = operation.definition
        elif isinstance(operation, FolderMembership):
            definition = FolderDefinition.model_validate(target["definition"])
            if operation.kind == "folder_add":
                if operation.chat_id not in definition.included_chat_ids:
                    definition.included_chat_ids.append(operation.chat_id)
                definition.excluded_chat_ids = [
                    id_ for id_ in definition.excluded_chat_ids if id_ != operation.chat_id
                ]
                if operation.pinned and operation.chat_id not in definition.pinned_chat_ids:
                    definition.pinned_chat_ids.append(operation.chat_id)
            else:
                definition.included_chat_ids = [
                    id_ for id_ in definition.included_chat_ids if id_ != operation.chat_id
                ]
                definition.pinned_chat_ids = [
                    id_ for id_ in definition.pinned_chat_ids if id_ != operation.chat_id
                ]
        else:
            definition = None
    if operation.expected_revision and operation.expected_revision != revision:
        raise TeleloomError(
            "stale_folder", "Folder definition or order changed; read it and preview a new plan."
        )
    operation.expected_revision = revision
    return target, definition


def validate_limits(
    operation: FolderOperation,
    definition: FolderDefinition,
    snapshot: dict[str, Any],
    current: dict[str, Any],
) -> None:
    limits = current["limits"]
    if (
        isinstance(operation, FolderCreate)
        and limits["folders"] is not None
        and sum(row["type"] != "system" for row in snapshot["items"]) >= limits["folders"]
    ):
        raise TeleloomError(
            "folder_limit", "The account's current folder count limit has been reached."
        )
    included = len(set(definition.included_chat_ids) | set(definition.pinned_chat_ids))
    if (
        limits["chats_per_folder"] is not None
        and max(included, len(definition.excluded_chat_ids)) > limits["chats_per_folder"]
        or limits["pinned_per_folder"] is not None
        and len(definition.pinned_chat_ids) > limits["pinned_per_folder"]
    ):
        raise TeleloomError(
            "folder_limit",
            "The approved definition exceeds the account's current folder peer/pin limits.",
        )


def folder_operation_allowed(config: Any, operation: dict[str, Any]) -> None:
    if config.kind != "user":
        raise TeleloomError(
            "unsupported_capability", "Telegram dialog-filter mutations are user-only."
        )
    if "folders" not in config.manage_scopes:
        raise TeleloomError("management_not_allowed", "Folders management permission was removed.")
    for id_ in operation.get("read_chat_ids", []):
        config.require_read(id_)


async def validate_folder_revision(adapter: Any, operation: dict[str, Any]) -> dict[str, Any]:
    folder_operation_allowed(adapter.profile, operation)
    parsed: FolderOperation = TypeAdapter(FolderOperation).validate_python(
        {key: value for key, value in operation.items() if key != "read_chat_ids"}
    )
    snapshot = await adapter.folder_snapshot()
    if snapshot["incomplete"]:
        raise TeleloomError(
            "coverage_incomplete", "Folder changes require a complete state revision."
        )
    folder_change(parsed, snapshot)
    return snapshot


async def execute_folder_operation(
    adapter: Any, operation: dict[str, Any], random_id: int
) -> dict[str, Any]:
    parsed: FolderOperation = TypeAdapter(FolderOperation).validate_python(
        {key: value for key, value in operation.items() if key != "read_chat_ids"}
    )
    snapshot = await validate_folder_revision(adapter, operation)
    target, definition = folder_change(parsed, snapshot)
    request: Any
    if isinstance(parsed, FolderReorder):
        request = functions.messages.UpdateDialogFiltersOrderRequest(
            order=[int(id_) for id_ in parsed.folder_ids]
        )
    elif isinstance(parsed, FolderDelete):
        request = functions.messages.UpdateDialogFilterRequest(
            id=int(parsed.folder_id), filter=None
        )
    else:
        if definition is None or parsed.folder_id is None:
            raise TeleloomError("invalid_plan", "The approved folder definition is missing.")
        validate_limits(parsed, definition, snapshot, await adapter.folder_limits())
        peers = {
            id_: await adapter._input_peer(id_)
            for id_ in sorted(definition_peers(definition), key=int)
        }
        if any(str(utils.get_peer_id(peer)) != id_ for id_, peer in peers.items()):
            raise TeleloomError(
                "peer_identity_changed", "A folder peer differs from its approved exact identity."
            )
        entities = []
        for entity in definition.title_entities:
            attributes = entity.model_dump(exclude_none=True)
            attributes.pop("kind")
            for id_field in ("document_id", "user_id"):
                if id_field in attributes:
                    attributes[id_field] = int(attributes[id_field])
            entities.append(ENTITY_CLASSES[entity.kind](**attributes))
        attributes = {
            "id": int(parsed.folder_id),
            "title": types.TextWithEntities(definition.title, entities),
            "include_peers": [peers[id_] for id_ in definition.included_chat_ids],
            "pinned_peers": [peers[id_] for id_ in definition.pinned_chat_ids],
            "emoticon": definition.emoticon,
            "color": definition.color,
            "title_noanimate": definition.title_noanimate,
        }
        if target and target["type"] == "shared":
            native = types.DialogFilterChatlist(
                **attributes, has_my_invites=target["has_my_invites"]
            )
        else:
            native = types.DialogFilter(
                **attributes,
                exclude_peers=[peers[id_] for id_ in definition.excluded_chat_ids],
                **definition.rules.model_dump(),
            )
        request = functions.messages.UpdateDialogFilterRequest(
            id=int(parsed.folder_id), filter=native
        )
    # Telegram provides no atomic compare-and-swap; this is the last possible local guard.
    await validate_folder_revision(adapter, operation)
    response = await adapter.client(request)
    if response is False:
        raise TeleloomError("telegram_rejected", "Telegram rejected the folder change.")
    return {
        "accepted": True,
        "complete": True,
        "kind": parsed.kind,
        "atomic_revision_check": False,
        "warning": "Folder revision was checked immediately before the request; Telegram does not provide atomic compare-and-swap.",
        **(
            {
                "membership": "Explicit peer membership only; matching dynamic rules can still include a removed peer."
            }
            if isinstance(parsed, FolderMembership) and parsed.kind == "folder_remove"
            else {}
        ),
    }
