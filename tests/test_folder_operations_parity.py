import pytest
from telethon import functions, types

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.telegram_fakes import SDK, factory
from tests.test_transport import client, running


class FoldersSDK(SDK):
    def __init__(self):
        super().__init__()
        self.filters = [
            types.DialogFilterDefault(),
            types.DialogFilter(
                id=4,
                title=types.TextWithEntities("Work", [types.MessageEntityBold(0, 4)]),
                include_peers=[types.InputPeerUser(7, 123456789)],
                pinned_peers=[types.InputPeerSelf()],
                exclude_peers=[types.InputPeerUser(8, 123456789)],
                groups=True,
                exclude_archived=True,
                title_noanimate=True,
                emoticon="W",
                color=3,
            ),
            types.DialogFilterChatlist(
                id=6,
                title=types.TextWithEntities("Shared", []),
                include_peers=[],
                pinned_peers=[],
                has_my_invites=True,
                color=2,
            ),
        ]

    async def get_input_entity(self, value):
        if isinstance(value, types.InputPeerUser):
            return value
        return types.InputPeerUser(int(getattr(value, "id", value)), 123456789)

    async def get_entity(self, value):
        return types.User(id=int(getattr(value, "user_id", value)), first_name="Peer")

    async def __call__(self, request, *args, **kwargs):
        bytes(request)
        self.calls.append(request)
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return types.messages.DialogFilters(filters=self.filters)
        if isinstance(request, functions.help.GetAppConfigRequest):
            return types.help.AppConfig(
                hash=1,
                config=types.JsonObject(
                    [
                        types.JsonObjectValue("dialog_filters_limit_default", types.JsonNumber(10)),
                        types.JsonObjectValue(
                            "dialog_filters_chats_limit_default", types.JsonNumber(100)
                        ),
                        types.JsonObjectValue(
                            "dialogs_folder_pinned_limit_default", types.JsonNumber(100)
                        ),
                    ]
                ),
            )
        if isinstance(request, functions.messages.UpdateDialogFilterRequest):
            index = next(
                (
                    index
                    for index, item in enumerate(self.filters)
                    if getattr(item, "id", None) == request.id
                ),
                None,
            )
            if request.filter is None:
                if index is not None:
                    self.filters.pop(index)
            elif index is None:
                self.filters.append(request.filter)
            else:
                self.filters[index] = request.filter
            return True
        if isinstance(request, functions.messages.UpdateDialogFiltersOrderRequest):
            custom = {item.id: item for item in self.filters if hasattr(item, "id")}
            self.filters = [types.DialogFilterDefault(), *[custom[id_] for id_ in request.order]]
            return True
        raise AssertionError(type(request).__name__)


@pytest.mark.asyncio
async def test_owner_reads_complete_folder_snapshot_revision_and_current_limits(tmp_path):
    sdk = FoldersSDK()
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as session:
        snapshot = data(await session.call_tool("folders_snapshot", {"profile_id": "personal"}))
        assert snapshot["ok"]
        state = snapshot["data"]
        assert state["order"] == ["0", "4", "6"]
        private = state["items"][1]
        assert private["definition"]["included_chat_ids"] == ["7"]
        assert private["definition"]["pinned_chat_ids"] == ["1"]
        assert private["definition"]["excluded_chat_ids"] == ["8"]
        assert private["definition"]["rules"]["groups"] is True
        assert private["definition"]["rules"]["contacts"] is False
        assert private["definition"]["title_entities"] == [
            {"kind": "bold", "offset": 0, "length": 4}
        ]
        assert private["definition"]["title_noanimate"] is True
        assert private["definition"]["color"] == 3
        assert state["items"][2]["type"] == "shared"
        assert state["items"][2]["has_my_invites"] is True
        assert len(private["revision"]) == len(state["revision"]) == 64
        assert "123456789" not in str(snapshot)
        limits = data(await session.call_tool("folder_limits", {"profile_id": "personal"}))
        assert limits["data"]["limits"] == {
            "folders": 10,
            "chats_per_folder": 100,
            "pinned_per_folder": 100,
        }
        assert limits["data"]["title_limit_utf16"] == 12


@pytest.mark.asyncio
async def test_folder_preview_requires_separate_management_scope_before_connection(tmp_path):
    from tests.fakes import TelegramAPI

    class NoConnection(TelegramAPI):
        async def start(self):
            raise AssertionError("Denied folder mutation must not connect")

    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", send_chats=["7"])}
    )
    async with running(settings, NoConnection) as app, client(app, settings) as session:
        denied = data(
            await session.call_tool(
                "folder_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "folder_update",
                        "folder_id": "4",
                        "definition": {"title": "Work"},
                    },
                },
            )
        )
        assert denied["error"]["code"] == "management_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "folder_create", "definition": {"title": "Work"}},
        {"kind": "folder_delete", "folder_id": "4"},
        {"kind": "folder_reorder", "folder_ids": ["4", "6"]},
        {"kind": "folder_add", "folder_id": "4", "chat_id": "7", "pinned": True},
        {"kind": "folder_remove", "folder_id": "4", "chat_id": "7"},
    ],
)
async def test_every_typed_folder_family_requires_explicit_owner_management_scope(
    tmp_path, operation
):
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                "folder_preview", {"profile_id": "personal", "operation": operation}
            )
        )
        assert result["error"]["code"] == "management_not_allowed"


@pytest.mark.asyncio
async def test_stale_folder_definition_is_rejected_before_a_confirmable_plan_exists(tmp_path):
    sdk = FoldersSDK()
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", manage_scopes=["folders"])}
    )
    async with running(settings, factory(sdk)) as app, client(app, settings) as session:
        old = data(
            await session.call_tool(
                "folders_snapshot", {"profile_id": "personal", "folder_id": "4"}
            )
        )["data"]["items"][0]
        sdk.filters[1].color = 4
        rejected = data(
            await session.call_tool(
                "folder_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "folder_update",
                        "folder_id": "4",
                        "definition": old["definition"],
                        "expected_revision": old["revision"],
                    },
                },
            )
        )
        assert rejected["error"]["code"] == "stale_folder"


async def execute_folder_plan(session, operation):
    from tests.test_jobs import complete

    result = data(
        await session.call_tool(
            "folder_preview", {"profile_id": "personal", "operation": operation}
        )
    )
    assert result["ok"], str(result)
    plan = result["data"]
    assert plan["preview"]["recipients"] == []
    assert plan["preview"]["targets"] == [
        {"kind": "account", "profile_id": "personal", "scope": "folders"}
    ]
    result = data(
        await session.call_tool(
            "delivery_execute",
            {
                "profile_id": "personal",
                "plan_id": plan["plan_id"],
                "plan_hash": plan["plan_hash"],
                "confirmed": True,
            },
        )
    )
    assert result["ok"], str(result)
    job = await complete(session, "personal", result["data"]["job_id"])
    assert job["status"] == "completed", str(job)
    assert job["deliveries"][0]["receipt"]["atomic_revision_check"] is False
    return plan, job


@pytest.mark.asyncio
async def test_all_folder_families_execute_typed_revision_bound_native_requests(tmp_path):
    sdk = FoldersSDK()
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", manage_scopes=["folders"])}
    )
    settings.limits.interval_seconds = 0
    async with running(settings, factory(sdk)) as app, client(app, settings) as session:
        snapshot = data(await session.call_tool("folders_snapshot", {"profile_id": "personal"}))[
            "data"
        ]
        definition = snapshot["items"][1]["definition"]
        definition["title"] = "New Work"
        definition["title_entities"] = [{"kind": "bold", "offset": 0, "length": 8}]
        await execute_folder_plan(
            session, {"kind": "folder_update", "folder_id": "4", "definition": definition}
        )
        updated = next(item for item in sdk.filters if getattr(item, "id", None) == 4)
        assert updated.title.text == "New Work"
        assert updated.color == 3 and updated.title_noanimate is True
        assert updated.emoticon == "W" and updated.groups is True
        assert [peer.user_id for peer in updated.pinned_peers] == [1]
        assert [peer.user_id for peer in updated.exclude_peers] == [8]
        await execute_folder_plan(
            session,
            {
                "kind": "folder_create",
                "definition": {"title": "Second", "included_chat_ids": ["7"]},
            },
        )
        assert any(getattr(item, "id", None) == 2 for item in sdk.filters)
        await execute_folder_plan(
            session, {"kind": "folder_reorder", "folder_ids": ["2", "6", "4"]}
        )
        assert [item.id for item in sdk.filters if hasattr(item, "id")] == [2, 6, 4]
        await execute_folder_plan(
            session, {"kind": "folder_add", "folder_id": "6", "chat_id": "8", "pinned": True}
        )
        shared = next(item for item in sdk.filters if getattr(item, "id", None) == 6)
        assert isinstance(shared, types.DialogFilterChatlist)
        assert shared.has_my_invites is True and shared.color == 2
        assert [peer.user_id for peer in shared.include_peers] == [8]
        assert [peer.user_id for peer in shared.pinned_peers] == [8]
        await execute_folder_plan(
            session, {"kind": "folder_remove", "folder_id": "6", "chat_id": "8"}
        )
        shared = next(item for item in sdk.filters if getattr(item, "id", None) == 6)
        assert shared.include_peers == shared.pinned_peers == []
        await execute_folder_plan(session, {"kind": "folder_delete", "folder_id": "2"})
        assert not any(getattr(item, "id", None) == 2 for item in sdk.filters)


@pytest.mark.asyncio
async def test_folder_last_revision_guard_fails_definitely_before_the_write_request(tmp_path):
    from tests.test_jobs import complete

    class ChangedDuringResolution(FoldersSDK):
        change = False

        async def get_input_entity(self, value):
            if self.change:
                self.change = False
                self.filters[1].title = types.TextWithEntities("Externally", [])
            return await super().get_input_entity(value)

    sdk = ChangedDuringResolution()
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", manage_scopes=["folders"])}
    )
    async with running(settings, factory(sdk)) as app, client(app, settings) as session:
        preview = data(
            await session.call_tool(
                "folder_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "folder_add", "folder_id": "4", "chat_id": "8"},
                },
            )
        )
        assert preview["ok"], str(preview)
        plan = preview["data"]
        sdk.change = True
        result = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        job = await complete(session, "personal", result["data"]["job_id"])
        assert job["status"] == "failed", str(job)
        assert job["deliveries"][0]["status"] == "failed"
        assert job["error"]["code"] == "stale_folder"
        assert not any(
            isinstance(request, functions.messages.UpdateDialogFilterRequest)
            for request in sdk.calls
        )


@pytest.mark.asyncio
async def test_existing_folder_peers_are_read_policy_bound_for_replacement_delete_and_status(
    tmp_path,
):
    sdk = FoldersSDK()
    profile = Profile(
        kind="user", manage_scopes=["folders"], read_mode="selected", read_chats=["7"]
    )
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, factory(sdk)) as app, client(app, settings) as session:
        snapshot = data(await session.call_tool("folders_snapshot", {"profile_id": "personal"}))[
            "data"
        ]
        assert snapshot["incomplete"] is True
        assert snapshot["items"][1]["definition"]["pinned_chat_ids"] == []
        assert snapshot["items"][1]["definition"]["excluded_chat_ids"] == []
        denied = data(
            await session.call_tool(
                "folder_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "folder_delete", "folder_id": "4"},
                },
            )
        )
        assert denied["error"]["code"] == "read_not_allowed"
        profile.read_chats = ["1", "7", "8"]
        plan = data(
            await session.call_tool(
                "folder_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "folder_delete", "folder_id": "4"},
                },
            )
        )["data"]
        assert plan["preview"]["operation"]["read_chat_ids"] == ["1", "7", "8"]
        profile.read_chats = ["7"]
        denied = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        assert denied["error"]["code"] == "read_not_allowed"
        assert not any(
            isinstance(request, functions.messages.UpdateDialogFilterRequest)
            for request in sdk.calls
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "folder_update", "folder_id": "0", "definition": {"title": "System"}},
        {"kind": "folder_create", "definition": {"title": "abcdefghijklX"}},
        {
            "kind": "folder_create",
            "definition": {
                "title": "\U0001f600",
                "title_entities": [{"kind": "bold", "offset": 1, "length": 1}],
            },
        },
        {"kind": "folder_reorder", "folder_ids": ["4"]},
    ],
)
async def test_system_folder_utf16_boundaries_and_incomplete_orders_cannot_be_approved(
    tmp_path, operation
):
    sdk = FoldersSDK()
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", manage_scopes=["folders"])}
    )
    async with running(settings, factory(sdk)) as app, client(app, settings) as session:
        result = await session.call_tool(
            "folder_preview", {"profile_id": "personal", "operation": operation}
        )
        assert result.isError
        assert not any(
            isinstance(
                request,
                (
                    functions.messages.UpdateDialogFilterRequest,
                    functions.messages.UpdateDialogFiltersOrderRequest,
                ),
            )
            for request in sdk.calls
        )


@pytest.mark.asyncio
async def test_custom_emoji_folder_title_and_sdk_peer_identity_guards(tmp_path):
    from tests.test_jobs import complete

    class ChangedPeer(FoldersSDK):
        swap = False

        async def get_input_entity(self, value):
            if self.swap and value == 7:
                return types.InputPeerUser(8, 123456789)
            return await super().get_input_entity(value)

    sdk = ChangedPeer()
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", manage_scopes=["folders"])}
    )
    settings.limits.interval_seconds = 0
    async with running(settings, factory(sdk)) as app, client(app, settings) as session:
        await execute_folder_plan(
            session,
            {
                "kind": "folder_create",
                "definition": {
                    "title": "\U0001f600",
                    "title_entities": [
                        {"kind": "custom_emoji", "offset": 0, "length": 2, "document_id": "7"}
                    ],
                    "color": 6,
                    "title_noanimate": False,
                    "included_chat_ids": ["7"],
                },
            },
        )
        custom = next(item for item in sdk.filters if getattr(item, "id", None) == 2)
        assert custom.title.entities[0].document_id == 7
        assert custom.title.entities[0].length == 2
        assert custom.color == 6 and custom.title_noanimate is False
        plan = data(
            await session.call_tool(
                "folder_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "folder_add",
                        "folder_id": "2",
                        "chat_id": "7",
                        "pinned": True,
                    },
                },
            )
        )["data"]
        sdk.swap = True
        result = data(
            await session.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )
        job = await complete(session, "personal", result["data"]["job_id"])
        assert job["status"] == "failed", str(job)
        assert job["error"]["code"] == "peer_identity_changed"
        assert job["deliveries"][0]["status"] == "failed"
        assert (
            len(
                [
                    request
                    for request in sdk.calls
                    if isinstance(request, functions.messages.UpdateDialogFilterRequest)
                ]
            )
            == 1
        )
