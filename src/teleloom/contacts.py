"""Safe contact evidence and profile-scoped exact aliases."""

import re
from difflib import get_close_matches
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from telethon import functions, types, utils

from .models import TeleloomError, utcnow

if TYPE_CHECKING:
    from .runtime import Runtime


class ContactChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ContactDelete(ContactChange):
    kind: Literal["contacts_delete"]
    user_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("user_ids")
    @classmethod
    def exact_users(cls, values: list[str]) -> list[str]:
        from .runtime import number

        if len(values) != len(set(values)):
            raise ValueError("Use unique contact user IDs.")
        for value in values:
            number(value, positive=True)
        return values


class PhoneContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(
        pattern=r"^\+[1-9][0-9]{5,14}$",
        description="Owner-supplied E.164 phone, shown in the immutable approval artifact; never inferred from stored Telegram data.",
    )
    first_name: str = Field(default="", max_length=64)
    last_name: str = Field(default="", max_length=64)


class ContactAdd(ContactChange):
    kind: Literal["contacts_add"]
    user_id: str | None = None
    username: str | None = Field(default=None, pattern=r"^@[a-zA-Z][a-zA-Z0-9_]{3,31}$")
    phone: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{5,14}$")
    first_name: str = Field(default="", max_length=64)
    last_name: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def exact_identity(self) -> Self:
        from .runtime import number

        if not (self.user_id or self.username or self.phone):
            raise ValueError("Supply an exact user ID, @username or owner-provided phone.")
        if self.user_id:
            number(self.user_id, positive=True)
        return self


class ContactBlock(ContactChange):
    kind: Literal["contacts_block", "contacts_unblock"]
    user_id: str

    @field_validator("user_id")
    @classmethod
    def exact_user(cls, value: str) -> str:
        from .runtime import number

        number(value, positive=True)
        return value


class ContactImport(ContactChange):
    kind: Literal["contacts_import"]
    contacts: list[PhoneContact] = Field(min_length=1, max_length=100)

    @field_validator("contacts")
    @classmethod
    def unique_phones(cls, values: list[PhoneContact]) -> list[PhoneContact]:
        if len({row.phone for row in values}) != len(values):
            raise ValueError("Use unique owner-provided phone numbers.")
        return values


ContactOperation = Annotated[
    ContactAdd | ContactDelete | ContactBlock | ContactImport, Field(discriminator="kind")
]


def safe_contact(user: Any, *, mutual: bool | None = None) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "first_name": getattr(user, "first_name", None) or "",
        "last_name": getattr(user, "last_name", None) or "",
        "username": getattr(user, "username", None),
        "is_bot": bool(getattr(user, "bot", False)),
        "mutual": mutual,
    }


async def read_contacts(adapter: Any, view: str) -> dict[str, Any]:
    from .runtime import fingerprint

    if view == "ids":
        ids = await adapter.client(functions.contacts.GetContactIDsRequest(hash=0))
        items = [{"id": str(id_)} for id_ in ids if id_]
    else:
        response = await adapter.client(functions.contacts.GetContactsRequest(hash=0))
        if isinstance(response, types.contacts.ContactsNotModified):
            raise TeleloomError("telegram_incomplete", "Telegram omitted uncached contact records.")
        contacts = {item.user_id: item.mutual for item in response.contacts}
        items = [
            safe_contact(user, mutual=contacts[user.id])
            for user in response.users
            if user.id in contacts
        ]
    items.sort(key=lambda item: int(item["id"]))
    return {
        "items": items,
        "incomplete": False,
        "source": "telegram",
        **({"revision": fingerprint(items)} if view != "ids" else {}),
        "warnings": [
            "Stored phone numbers are excluded; contact export contains safe identity/name fields only."
        ]
        if view == "export"
        else [],
    }


async def search_contacts(adapter: Any, query: str) -> dict[str, Any]:
    response = await adapter.client(functions.contacts.SearchRequest(q=query, limit=100))
    ids = {
        item.user_id
        for item in [*response.my_results, *response.results]
        if isinstance(item, types.PeerUser)
    }
    return {
        "items": [safe_contact(user) for user in response.users if user.id in ids],
        "incomplete": len(response.results) >= 100,
        "source": "telegram_search",
    }


def safe_chat(raw: Any) -> dict[str, Any]:
    return {
        "id": str(utils.get_peer_id(raw)),
        "title": getattr(raw, "title", None) or getattr(raw, "first_name", None) or str(raw.id),
        "username": getattr(raw, "username", None),
        "kind": "group"
        if isinstance(raw, types.Chat) or getattr(raw, "megagroup", False)
        else "channel"
        if isinstance(raw, types.Channel)
        else "private",
    }


async def blocked_contacts(adapter: Any) -> dict[str, Any]:
    from .runtime import fingerprint

    items: list[dict[str, Any]] = []
    complete = False
    for _ in range(10):
        response = await adapter.client(
            functions.contacts.GetBlockedRequest(offset=len(items), limit=100)
        )
        peers = {str(utils.get_peer_id(raw)): raw for raw in [*response.users, *response.chats]}
        for row in response.blocked:
            id_ = str(utils.get_peer_id(row.peer_id))
            raw = peers.get(id_)
            record = (
                safe_contact(raw)
                if isinstance(raw, types.User)
                else safe_chat(raw)
                if raw
                else {"id": id_, "unavailable": True}
            )
            record["blocked_at"] = row.date.isoformat() if row.date else None
            items.append(record)
        complete = isinstance(response, types.contacts.Blocked) or len(items) >= response.count
        if complete or not response.blocked:
            break
    return {
        "items": items,
        "source": "telegram_blocked",
        "incomplete": not complete,
        "revision": fingerprint(items),
    }


async def common_contact_chats(adapter: Any, contact_id: str) -> dict[str, Any]:
    user = utils.get_input_user(await adapter._input_peer(contact_id))
    items: list[dict[str, Any]] = []
    before = 0
    complete = False
    seen: set[str] = set()
    for _ in range(10):
        response = await adapter.client(
            functions.messages.GetCommonChatsRequest(user_id=user, max_id=before, limit=100)
        )
        for raw in response.chats:
            record = safe_chat(raw)
            if record["id"] not in seen:
                seen.add(record["id"])
                items.append(record)
        complete = isinstance(response, types.messages.Chats) or len(items) >= response.count
        if complete or not response.chats:
            break
        following = response.chats[-1].id
        if following == before:
            raise TeleloomError(
                "pagination_stalled", "Telegram common-chat pagination did not advance."
            )
        before = following
    return {"items": items, "source": "telegram_common_chats", "incomplete": not complete}


class Contacts:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime

    def require_user(self, profile_id: str) -> None:
        if self.runtime.settings.profile(profile_id).kind != "user":
            raise TeleloomError(
                "unsupported_capability", "Telegram address-book methods are user-only."
            )

    def restrict(
        self, profile_id: str, snapshot: dict[str, Any], id_field: str = "id"
    ) -> dict[str, Any]:
        config = self.runtime.settings.profile(profile_id)
        selected = [row for row in snapshot["items"] if config.allows_read(row[id_field])]
        if len(selected) != len(snapshot["items"]):
            snapshot["incomplete"] = True
            snapshot["warnings"] = [
                *snapshot.get("warnings", []),
                "Some contact evidence is outside the owner's selected read policy.",
            ]
        snapshot["items"] = selected
        return snapshot

    def empty_selection(self, profile_id: str) -> bool:
        config = self.runtime.settings.profile(profile_id)
        return config.read_mode == "selected" and not config.read_chats

    async def list(
        self, profile_id: str, view: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        self.require_user(profile_id)
        snapshot = None
        if not cursor:
            snapshot = (
                {"items": [], "incomplete": False, "source": "telegram"}
                if self.empty_selection(profile_id)
                else await (await self.runtime.adapter(profile_id)).contacts(view)
            )
            snapshot = self.restrict(profile_id, snapshot)
            snapshot["snapshot_at"] = utcnow().isoformat()
        return self.runtime.snapshot_page(profile_id, ["contacts", view], cursor, limit, snapshot)

    def alias_key(self, alias: str) -> str:
        if (
            not re.fullmatch(r"[\w .-]{1,64}", alias)
            or alias != alias.strip()
            or alias.lstrip("-").isdigit()
        ):
            raise TeleloomError(
                "invalid_alias",
                "Use 1-64 letters, digits, spaces, dots or underscores; an alias cannot be a numeric ID.",
            )
        return alias.casefold()

    def aliases(self, profile_id: str) -> dict[str, Any]:
        self.runtime.settings.profile(profile_id)
        return dict(self.runtime.store.state(f"contact_aliases:{profile_id}", {}))

    def exact_alias(self, profile_id: str, target: str) -> str | None:
        row = self.aliases(profile_id).get(target.casefold())
        return str(row["chat_id"]) if row else None

    async def alias_set(
        self, profile_id: str, alias: str, chat_id: str, replace: bool
    ) -> dict[str, Any]:
        from .runtime import number

        number(chat_id)
        key = self.alias_key(alias)
        aliases = self.aliases(profile_id)
        existing = aliases.get(key)
        if existing and existing["chat_id"] != chat_id and not replace:
            raise TeleloomError(
                "alias_exists",
                "This exact alias already names another peer. Use replace=true to change its target explicitly.",
            )
        if key not in aliases and len(aliases) >= 1000:
            raise TeleloomError("alias_limit", "A profile can store at most 1000 aliases.")
        peer = await self.runtime.resolve(profile_id, chat_id)
        if peer["id"] != chat_id:
            raise TeleloomError("peer_identity_changed", "Resolved peer identity changed.")
        row = {
            "alias": alias,
            "chat_id": chat_id,
            "title": peer["title"],
            "username": peer.get("username"),
        }
        # No await after loading here: owner tasks cannot interleave local updates.
        aliases = self.aliases(profile_id)
        existing = aliases.get(key)
        if existing and existing["chat_id"] != chat_id and not replace:
            raise TeleloomError(
                "alias_exists", "Alias changed during resolution; replace it explicitly."
            )
        if key not in aliases and len(aliases) >= 1000:
            raise TeleloomError("alias_limit", "A profile can store at most 1000 aliases.")
        aliases[key] = row
        with self.runtime.store.db:
            self.runtime.store.set_state(f"contact_aliases:{profile_id}", aliases)
        return {"alias": row, "replaced": bool(existing and existing["chat_id"] != chat_id)}

    async def alias_delete(self, profile_id: str, alias: str) -> dict[str, Any]:
        key = self.alias_key(alias)
        aliases = self.aliases(profile_id)
        existed = aliases.pop(key, None) is not None
        with self.runtime.store.db:
            self.runtime.store.set_state(f"contact_aliases:{profile_id}", aliases)
        return {"deleted": existed}

    async def alias_list(self, profile_id: str, cursor: str | None, limit: int) -> dict[str, Any]:
        snapshot = (
            None
            if cursor
            else {
                "items": sorted(
                    self.aliases(profile_id).values(), key=lambda row: row["alias"].casefold()
                ),
                "snapshot_at": utcnow().isoformat(),
                "source": "local_aliases",
                "incomplete": False,
            }
        )
        if snapshot:
            snapshot = self.restrict(profile_id, snapshot, "chat_id")
        return self.runtime.snapshot_page(profile_id, ["aliases"], cursor, limit, snapshot)

    async def search(
        self, profile_id: str, query: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        self.require_user(profile_id)
        if not query.strip() or len(query) > 128:
            raise TeleloomError("invalid_query", "Use 1-128 non-whitespace search characters.")
        snapshot = None
        if not cursor:
            snapshot = (
                {"items": [], "incomplete": False, "source": "telegram_search"}
                if self.empty_selection(profile_id)
                else await (await self.runtime.adapter(profile_id)).search_contacts(query)
            )
            snapshot = self.restrict(profile_id, snapshot)
            config = self.runtime.settings.profile(profile_id)
            aliases = {
                key: row
                for key, row in self.aliases(profile_id).items()
                if config.allows_read(row["chat_id"])
            }
            key = query.casefold()
            snapshot.update(
                {
                    "snapshot_at": utcnow().isoformat(),
                    "alias_matches": [aliases[key]] if key in aliases else [],
                    "alias_suggestions": [
                        aliases[k]
                        for k in get_close_matches(key, list(aliases), n=5, cutoff=0.55)
                        if k != key
                    ],
                    "automatic_target_selection": False,
                }
            )
        return self.runtime.snapshot_page(
            profile_id, ["contacts_search", query], cursor, limit, snapshot
        )

    async def blocked(self, profile_id: str, cursor: str | None, limit: int) -> dict[str, Any]:
        self.require_user(profile_id)
        snapshot = None
        if not cursor:
            snapshot = (
                {"items": [], "incomplete": False, "source": "telegram_blocked"}
                if self.empty_selection(profile_id)
                else await (await self.runtime.adapter(profile_id)).blocked_contacts()
            )
            snapshot = self.restrict(profile_id, snapshot)
            snapshot["snapshot_at"] = utcnow().isoformat()
        return self.runtime.snapshot_page(profile_id, ["blocked_contacts"], cursor, limit, snapshot)

    async def direct(
        self, profile_id: str, query: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        self.require_user(profile_id)
        if not query.strip() or len(query) > 128:
            raise TeleloomError("invalid_query", "Use 1-128 non-whitespace search characters.")
        snapshot = None
        if not cursor:
            if self.empty_selection(profile_id):
                snapshot = {
                    "items": [],
                    "snapshot_at": utcnow().isoformat(),
                    "source": "telegram_direct_chats",
                    "incomplete": False,
                    "automatic_target_selection": False,
                }
            else:
                adapter = await self.runtime.adapter(profile_id)
                contacts = await adapter.contacts("records")
                key = query.casefold()
                ids = {
                    row["id"]
                    for row in contacts["items"]
                    if key
                    in " ".join(
                        str(row.get(field) or "")
                        for field in ["first_name", "last_name", "username"]
                    ).casefold()
                }
                alias = self.exact_alias(profile_id, query)
                if alias:
                    ids.add(alias)
                chats = await adapter.chats()
                items = [
                    row.model_dump(mode="json")
                    for row in chats
                    if row.kind in {"private", "user", "bot"}
                    and (row.id in ids or key in row.title.casefold())
                ]
                snapshot = {
                    "items": items[:1000],
                    "snapshot_at": utcnow().isoformat(),
                    "source": "telegram_direct_chats",
                    "incomplete": bool(contacts["incomplete"]) or len(items) > 1000,
                    "automatic_target_selection": False,
                }
            snapshot = self.restrict(profile_id, snapshot)
        return self.runtime.snapshot_page(
            profile_id, ["direct_contacts", query], cursor, limit, snapshot
        )

    async def chats(
        self, profile_id: str, contact_id: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        from .runtime import number

        self.require_user(profile_id)
        number(contact_id, positive=True)
        self.runtime.settings.profile(profile_id).require_read(contact_id)
        snapshot = None
        if not cursor:
            adapter = await self.runtime.adapter(profile_id)
            direct = await self.runtime.resolve(profile_id, contact_id)
            if direct["kind"] != "private":
                raise TeleloomError("invalid_contact", "Select an exact Telegram user ID.")
            snapshot = await adapter.common_contact_chats(contact_id)
            snapshot["items"] = [direct, *snapshot["items"]]
            snapshot = self.restrict(profile_id, snapshot)
            snapshot["snapshot_at"] = utcnow().isoformat()
        return self.runtime.snapshot_page(
            profile_id, ["contact_chats", contact_id], cursor, limit, snapshot
        )

    async def interactions(self, profile_id: str, contact_id: str, limit: int) -> dict[str, Any]:
        from .runtime import number

        self.require_user(profile_id)
        number(contact_id, positive=True)
        result = await self.runtime.history(profile_id, contact_id, limit=limit)
        result["contact_id"] = contact_id
        return result

    async def preview(self, profile_id: str, operation: ContactOperation) -> dict[str, Any]:
        prepared = await prepare_contact_operation(
            self.runtime, profile_id, operation.model_dump(mode="json")
        )
        return self.runtime.jobs.confirmed_preview(profile_id, **prepared)


async def prepare_contact_operation(
    runtime: "Runtime", profile_id: str, operation: dict[str, Any]
) -> dict[str, Any]:
    from .runtime import fingerprint

    config = runtime.settings.profile(profile_id)
    if config.kind != "user":
        raise TeleloomError(
            "unsupported_capability", "Telegram address-book changes are user-only."
        )
    if "contacts" not in config.manage_scopes:
        raise TeleloomError(
            "management_not_allowed",
            "Enable contacts through the owner-only profile management CLI before preparing a contact plan.",
        )
    parsed: ContactOperation = TypeAdapter(ContactOperation).validate_python(operation)
    ids: list[str] = []
    if isinstance(parsed, ContactDelete):
        ids = parsed.user_ids
    elif isinstance(parsed, (ContactBlock, ContactAdd)) and parsed.user_id:
        ids = [parsed.user_id]
    for id_ in ids:
        config.require_read(id_)
    adapter = await runtime.adapter(profile_id)
    current = (
        await adapter.blocked_contacts()
        if isinstance(parsed, ContactBlock)
        else await adapter.contacts("records")
    )
    if current["incomplete"]:
        raise TeleloomError(
            "coverage_incomplete", "A contact mutation requires a complete state revision."
        )
    revision = fingerprint(current["items"])
    if parsed.expected_revision and parsed.expected_revision != revision:
        raise TeleloomError(
            "stale_contacts", "Contact state changed; read it and preview a new plan."
        )
    parsed.expected_revision = revision
    resolved = [await runtime.resolve(profile_id, id_) for id_ in ids]
    if any(
        row["id"] != id_ or row["kind"] != "private" for id_, row in zip(ids, resolved, strict=True)
    ):
        raise TeleloomError("peer_identity_changed", "Select an exact Telegram user identity.")
    if isinstance(parsed, ContactAdd) and parsed.username:
        peer = await runtime.resolve(profile_id, parsed.username)
        if peer["kind"] != "private" or parsed.user_id and parsed.user_id != peer["id"]:
            raise TeleloomError(
                "peer_identity_changed",
                "Username and requested contact do not identify the same exact Telegram user.",
            )
        parsed.user_id = peer["id"]
        resolved = [peer]
    return {
        "operation": {
            **parsed.model_dump(mode="json"),
            "read_chat_ids": [row["id"] for row in resolved],
        },
        "targets": [{"kind": "account", "profile_id": profile_id, "scope": "contacts"}],
        "sources": [],
        "resolved": resolved,
    }


def contact_operation_allowed(config: Any, operation: dict[str, Any]) -> None:
    if config.kind != "user":
        raise TeleloomError(
            "unsupported_capability", "Telegram address-book changes are user-only."
        )
    if "contacts" not in config.manage_scopes:
        raise TeleloomError("management_not_allowed", "Contacts management permission was removed.")
    for id_ in operation.get("read_chat_ids", []):
        config.require_read(id_)


async def validate_contact_revision(adapter: Any, operation: dict[str, Any]) -> None:
    from .runtime import fingerprint

    contact_operation_allowed(adapter.profile, operation)
    current = await (
        adapter.blocked_contacts()
        if operation["kind"] in {"contacts_block", "contacts_unblock"}
        else adapter.contacts("records")
    )
    if current["incomplete"]:
        raise TeleloomError(
            "coverage_incomplete", "A contact mutation requires a complete state revision."
        )
    if fingerprint(current["items"]) != operation["expected_revision"]:
        raise TeleloomError("stale_contacts", "Contact state changed; preview a new plan.")


async def execute_contact_operation(
    adapter: Any, operation: dict[str, Any], random_id: int
) -> dict[str, Any]:
    parsed: ContactOperation = TypeAdapter(ContactOperation).validate_python(
        {key: value for key, value in operation.items() if key != "read_chat_ids"}
    )
    contact_operation_allowed(adapter.profile, operation)
    imported_rows: list[PhoneContact] | None = None
    request: Any
    if isinstance(parsed, ContactDelete):
        peers = [
            utils.get_input_user(await exact_contact_peer(adapter, id_)) for id_ in parsed.user_ids
        ]
        request = functions.contacts.DeleteContactsRequest(id=peers)
    elif isinstance(parsed, ContactBlock):
        peer = await exact_contact_peer(adapter, parsed.user_id)
        request = (
            functions.contacts.BlockRequest
            if parsed.kind == "contacts_block"
            else functions.contacts.UnblockRequest
        )(id=peer)
    elif isinstance(parsed, ContactAdd) and parsed.user_id:
        request = functions.contacts.AddContactRequest(
            id=utils.get_input_user(await exact_contact_peer(adapter, parsed.user_id)),
            first_name=parsed.first_name,
            last_name=parsed.last_name,
            phone=parsed.phone or "",
        )
    else:
        if isinstance(parsed, ContactAdd) and parsed.phone is None:
            raise TeleloomError("invalid_plan", "The approved owner-supplied phone is missing.")
        imported_rows = (
            parsed.contacts
            if isinstance(parsed, ContactImport)
            else [
                PhoneContact(
                    phone=parsed.phone or "",
                    first_name=parsed.first_name,
                    last_name=parsed.last_name,
                )
            ]
        )
        request = functions.contacts.ImportContactsRequest(
            contacts=[
                types.InputPhoneContact(
                    client_id=(random_id + index) % (2**63),
                    phone=row.phone,
                    first_name=row.first_name,
                    last_name=row.last_name,
                )
                for index, row in enumerate(imported_rows)
            ]
        )
    await validate_contact_revision(adapter, operation)
    response = await adapter.client(request)
    if response is False:
        raise TeleloomError("telegram_rejected", "Telegram rejected the contact change.")
    if imported_rows is not None:
        imported = {row.client_id: row.user_id for row in response.imported}
        retry = set(response.retry_contacts)
        items = []
        for index in range(len(imported_rows)):
            client_id = (random_id + index) % (2**63)
            row = {
                "position": index,
                "status": "retry_required"
                if client_id in retry
                else "imported"
                if client_id in imported
                else "not_imported",
            }
            if client_id in imported:
                id_ = str(imported[client_id])
                if adapter.profile.allows_read(id_):
                    row["user_id"] = id_
                else:
                    row["identity_withheld"] = True
            items.append(row)
        return {
            "accepted": True,
            "complete": all(row["status"] == "imported" for row in items),
            "kind": parsed.kind,
            "items": items,
        }
    return {"accepted": True, "complete": True, "kind": parsed.kind}


async def exact_contact_peer(adapter: Any, id_: str) -> Any:
    peer = await adapter._input_peer(id_)
    if str(utils.get_peer_id(peer)) != id_:
        raise TeleloomError(
            "peer_identity_changed", "Resolved contact identity differs from the approved user ID."
        )
    return peer
