import pytest

from teleloom.config import Profile, Settings
from tests.fakes import TelegramAPI, data
from tests.test_transport import client, running


@pytest.mark.asyncio
async def test_user_can_read_folder_membership_through_mcp(tmp_path):
    folder = {
        "id": "4",
        "title": "Самара",
        "included_chat_ids": ["-1000000000100"],
        "pinned_chat_ids": ["-1000000000200"],
        "excluded_chat_ids": [],
        "rules": {},
    }

    class FoldersAPI(TelegramAPI):
        async def folders(self):
            return [folder]

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, FoldersAPI) as app, client(app, settings) as session:
        result = data(await session.call_tool("folders_list", {"profile_id": "personal"}))
        assert result["ok"]
        assert result["data"]["items"] == [folder]
        assert result["data"]["source"] == "telegram"
        assert result["data"]["incomplete"] is False


@pytest.mark.asyncio
async def test_bot_folder_request_reports_unsupported_capability(tmp_path):
    settings = Settings(data_dir=tmp_path, profiles={"helper": Profile(kind="bot")})
    async with running(settings, TelegramAPI) as app, client(app, settings) as session:
        result = data(await session.call_tool("folders_list", {"profile_id": "helper"}))
        assert result["error"]["code"] == "unsupported_capability"


@pytest.mark.asyncio
async def test_folder_response_preserves_rules_without_exposing_access_hashes(
    tmp_path, monkeypatch
):
    from telethon import functions, types
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    from teleloom.adapters import make_adapter

    class TelegramResponses:
        def __init__(self, *args, **kwargs):
            self.session = args[0]

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return types.User(id=1, first_name="Test owner")

        def add_event_handler(self, *args):
            pass

        async def get_entity(self, target):
            return types.Channel(
                id=300,
                title="Uncached discussion",
                photo=types.ChatPhotoEmpty(),
                date=None,
                megagroup=True,
            )

        async def iter_dialogs(self):
            from types import SimpleNamespace

            yield SimpleNamespace(
                id=-1000000000100,
                name="Channel",
                is_channel=True,
                is_group=False,
                entity=types.Channel(
                    id=100, title="Channel", photo=types.ChatPhotoEmpty(), date=None, broadcast=True
                ),
                unread_count=0,
                dialog=SimpleNamespace(read_inbox_max_id=0, top_message=1),
            )
            yield SimpleNamespace(
                id=-1000000000200,
                name="Discussion",
                is_channel=True,
                is_group=True,
                entity=types.Channel(
                    id=200,
                    title="Discussion",
                    photo=types.ChatPhotoEmpty(),
                    date=None,
                    megagroup=True,
                ),
                unread_count=0,
                dialog=SimpleNamespace(read_inbox_max_id=0, top_message=1),
            )

        async def __call__(self, request):
            assert isinstance(request, functions.messages.GetDialogFiltersRequest)
            return types.messages.DialogFilters(
                filters=[
                    types.DialogFilterDefault(),
                    types.DialogFilter(
                        id=4,
                        title=types.TextWithEntities(text="Самара", entities=[]),
                        include_peers=[types.InputPeerChannel(100, 123456789)],
                        pinned_peers=[types.InputPeerChat(200)],
                        exclude_peers=[types.InputPeerUser(300, 987654321)],
                        broadcasts=True,
                        exclude_archived=True,
                    ),
                ]
            )

    saved = StringSession()
    saved.set_dc(2, "127.0.0.1", 443)
    saved.auth_key = AuthKey(b"\x01" * 256)
    monkeypatch.setenv("TELELOOM_PERSONAL_SESSION", saved.save())
    monkeypatch.setenv("TELELOOM_PERSONAL_API_HASH", "synthetic-api-hash")
    monkeypatch.setattr("teleloom.adapters.TelegramClient", TelegramResponses)
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user", api_id=1)})
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        result = data(await session.call_tool("folders_list", {"profile_id": "personal"}))
        assert result["ok"]
        assert result["data"]["items"] == [
            {
                "id": "4",
                "title": "Самара",
                "included_chat_ids": ["-1000000000100"],
                "pinned_chat_ids": ["-200"],
                "excluded_chat_ids": ["300"],
                "rules": {"broadcasts": True, "exclude_archived": True},
            }
        ]
        assert "123456789" not in str(result)
        assert "987654321" not in str(result)
        dialogs = data(await session.call_tool("chats_list", {"profile_id": "personal"}))
        assert [(c["title"], c["kind"]) for c in dialogs["data"]["items"]] == [
            ("Channel", "channel"),
            ("Discussion", "group"),
        ]
        resolved = data(
            await session.call_tool(
                "chat_resolve",
                {
                    "profile_id": "personal",
                    "target": "@uncached_discussion",
                },
            )
        )
        assert resolved["data"]["kind"] == "group"
