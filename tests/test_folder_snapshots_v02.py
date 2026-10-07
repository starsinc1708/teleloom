import pytest

from teleloom.config import Profile, Settings
from teleloom.models import Chat
from tests.fakes import TelegramAPI, data
from tests.test_transport import client, running


class FolderAPI(TelegramAPI):
    instances = []

    def __init__(self, *args):
        super().__init__(*args)
        self.instances.append(self)

    async def folders(self):
        return [
            {
                "id": "4",
                "title": "Самара",
                "included_chat_ids": ["100", "999"],
                "pinned_chat_ids": ["200"],
                "excluded_chat_ids": ["300"],
                "rules": {
                    "broadcasts": True,
                    "exclude_read": True,
                    "exclude_muted": True,
                    "exclude_archived": True,
                },
            }
        ]

    async def chats(self):
        return [
            Chat(id="100", title="Explicit", kind="channel", archived=True, muted=True),
            Chat(id="200", title="Pinned group", kind="group"),
            Chat(id="300", title="Excluded", kind="channel", unread_count=1),
            Chat(
                id="400",
                title="Dynamic",
                kind="channel",
                unread_count=1,
                archived=False,
                muted=False,
            ),
            Chat(
                id="500",
                title="Mention",
                kind="channel",
                unread_mentions_count=1,
                archived=False,
                muted=True,
            ),
            Chat(
                id="600",
                title="Archived",
                kind="channel",
                unread_count=1,
                archived=True,
                muted=False,
            ),
            Chat(id="700", title="Mega", kind="group", unread_count=1),
        ]


@pytest.mark.asyncio
async def test_folder_members_freeze_rules_and_report_unavailable(tmp_path):
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")}
    )
    async with running(settings, FolderAPI) as app, client(app, settings) as session:
        first = data(
            await session.call_tool(
                "folder_members", {"profile_id": "personal", "folder_id": "4", "limit": 2}
            )
        )
        assert first["ok"]
        page = first["data"]
        assert [c["id"] for c in page["items"]] == ["200", "100"]
        assert page["unavailable"][0]["chat_id"] == "999"
        adapter = FolderAPI.instances[-1]

        async def changed():
            return []

        adapter.chats = changed
        second = data(
            await session.call_tool(
                "folder_members",
                {
                    "profile_id": "personal",
                    "folder_id": "4",
                    "limit": 2,
                    "cursor": page["next_cursor"],
                },
            )
        )
        assert [c["id"] for c in second["data"]["items"]] == ["400", "500"]
        assert second["data"]["snapshot_id"] == page["snapshot_id"]
        other = data(
            await session.call_tool(
                "folder_members",
                {"profile_id": "work", "folder_id": "4", "cursor": page["next_cursor"]},
            )
        )
        assert other["error"]["code"] == "invalid_cursor"
        assert adapter.sent == adapter.ack == []


@pytest.mark.asyncio
async def test_folder_cursor_expiry_generation_and_filter_are_explicit(tmp_path, monkeypatch):
    from datetime import timedelta

    from teleloom.models import utcnow

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, FolderAPI) as app, client(app, settings) as session:
        first = data(
            await session.call_tool(
                "folder_members", {"profile_id": "personal", "folder_id": "4", "limit": 1}
            )
        )
        args = {"profile_id": "personal", "folder_id": "4", "cursor": first["data"]["next_cursor"]}
        filtered = data(await session.call_tool("chats_list", {**args, "kind": "channel"}))
        assert filtered["error"]["code"] == "invalid_cursor"
        original = settings.profiles["personal"].generation
        settings.profiles["personal"].generation = "replacement"
        changed = data(await session.call_tool("folder_members", args))
        assert changed["error"]["code"] == "invalid_cursor"
        settings.profiles["personal"].generation = original
        later = utcnow() + timedelta(minutes=16)
        monkeypatch.setattr("teleloom.runtime.utcnow", lambda: later)
        expired = data(await session.call_tool("folder_members", args))
        assert expired["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_shared_folder_uses_explicit_members_and_private_type_rules(tmp_path):
    class SharedAPI(FolderAPI):
        async def folders(self):
            return [
                {
                    "id": "4",
                    "title": "Shared",
                    "shared": True,
                    "included_chat_ids": ["600"],
                    "pinned_chat_ids": ["200"],
                    "excluded_chat_ids": [],
                    "rules": {"broadcasts": True, "exclude_archived": True},
                },
                {
                    "id": "5",
                    "title": "Contacts",
                    "included_chat_ids": [],
                    "pinned_chat_ids": [],
                    "excluded_chat_ids": [],
                    "rules": {"contacts": True},
                },
            ]

        async def chats(self):
            return [
                *(await super().chats()),
                Chat(id="800", title="Bot contact", kind="private", is_contact=True, is_bot=True),
                Chat(id="900", title="Contact", kind="private", is_contact=True, is_bot=False),
            ]

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, SharedAPI) as app, client(app, settings) as session:
        shared = data(
            await session.call_tool("folder_members", {"profile_id": "personal", "folder_id": "4"})
        )["data"]
        assert [c["id"] for c in shared["items"]] == ["200", "600"]
        assert shared["incomplete"] is False
        contacts = data(
            await session.call_tool("chats_list", {"profile_id": "personal", "folder_id": "5"})
        )["data"]
        assert [c["id"] for c in contacts["items"]] == ["900"]


@pytest.mark.asyncio
async def test_bot_folder_capability_is_rejected_before_connection(tmp_path):
    class OfflineBot(TelegramAPI):
        async def start(self):
            raise AssertionError("Folder capability does not need a bot connection")

    settings = Settings(data_dir=tmp_path, profiles={"helper": Profile(kind="bot")})
    async with running(settings, OfflineBot) as app, client(app, settings) as session:
        result = data(
            await session.call_tool("folder_members", {"profile_id": "helper", "folder_id": "4"})
        )
        assert result["error"]["code"] == "unsupported_capability"
