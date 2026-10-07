import pytest
from telethon import functions, types

from teleloom.config import Profile, Settings
from teleloom.models import Chat
from tests.fakes import data
from tests.telegram_fakes import CHAT, SDK, factory, topic
from tests.test_folder_snapshots_v02 import FolderAPI
from tests.test_transport import client, running


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", [None, "4"])
async def test_dialog_filters_bind_snapshot_and_query_after_folder_membership(tmp_path, folder):
    class FilterAPI(FolderAPI):
        async def chats(self):
            return [
                *(await super().chats()),
                Chat(
                    id="800",
                    title="Another dynamic",
                    kind="channel",
                    unread_count=1,
                    muted=False,
                    archived=False,
                ),
            ]

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, FilterAPI) as app, client(app, settings) as mcp:
        arguments = {
            "profile_id": "personal",
            "folder_id": folder,
            "kind": "channel",
            "unread_only": True,
            "unmuted_only": True,
            "archived": False,
            "limit": 1,
        }
        first = data(await mcp.call_tool("chats_list", arguments))
        assert first["ok"], first
        assert [row["id"] for row in first["data"]["items"]] == ["400"]
        assert first["data"]["next_cursor"]
        changed = data(
            await mcp.call_tool(
                "chats_list",
                {**arguments, "archived": True, "cursor": first["data"]["next_cursor"]},
            )
        )
        assert changed["error"]["code"] == "invalid_cursor"
        second = data(
            await mcp.call_tool("chats_list", {**arguments, "cursor": first["data"]["next_cursor"]})
        )
        assert [row["id"] for row in second["data"]["items"]] == ["800"]
        if folder:
            assert first["data"]["unavailable"][0]["chat_id"] == "999"


@pytest.mark.asyncio
async def test_topic_title_search_crosses_native_pages_and_freezes_matching_snapshot(tmp_path):
    class TopicSDK(SDK):
        async def __call__(self, request, *args, **kwargs):
            assert isinstance(request, functions.messages.GetForumTopicsRequest)
            self.calls.append(request)
            rows = (
                [topic(5, 50)]
                if not request.offset_topic
                else [topic(6, 60), topic(7, 70)]
                if request.offset_topic == 5
                else []
            )
            for row in rows:
                row.title = "Other" if row.id == 5 else f"Release {row.id}"
            return types.messages.ForumTopics(
                count=3, topics=rows, messages=[], chats=[], users=[], pts=1
            )

    sdk = TopicSDK()
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, factory(sdk)) as app, client(app, settings) as mcp:
        args = {"profile_id": "personal", "chat_id": CHAT, "query": "release", "limit": 1}
        first = data(await mcp.call_tool("topics_list", args))
        assert first["ok"], first
        assert [row["id"] for row in first["data"]["items"]] == ["6"]
        assert len(sdk.calls) == 3
        other = data(
            await mcp.call_tool(
                "topics_list", {**args, "query": "Other", "cursor": first["data"]["next_cursor"]}
            )
        )
        assert other["error"]["code"] == "invalid_cursor"
        second = data(
            await mcp.call_tool("topics_list", {**args, "cursor": first["data"]["next_cursor"]})
        )
        assert [row["id"] for row in second["data"]["items"]] == ["7"]
        assert len(sdk.calls) == 3 and not second["data"]["next_cursor"]
