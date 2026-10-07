"""Public administration read contract; mutable fixtures stay per case."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from telethon import functions, types

from teleloom.adapters import make_adapter
from teleloom.config import Profile, Settings
from teleloom.models import Chat, Message
from teleloom.store import Store
from tests.administration_fakes import GROUP
from tests.administration_fakes import sdk as sdk
from tests.fakes import data
from tests.test_transport import client, running


async def test_selected_group_participants_are_scoped_and_do_not_authorize_delivery(tmp_path, sdk):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user", identity={"id": "7"}, read_mode="selected", read_chats=[GROUP]
            ),
            "other": Profile(
                kind="user", identity={"id": "7"}, read_mode="selected", read_chats=[]
            ),
        },
    )
    async with running(settings, adapter_factory=make_adapter) as app:
        async with client(app, settings) as session:
            result = data(
                await session.call_tool(
                    "administration_read",
                    {
                        "profile_id": "work",
                        "operation": {"kind": "participants", "chat_id": GROUP},
                    },
                )
            )
            assert result["ok"], result
            assert result["data"]["items"][0]["id"] == "8"
            assert "access_hash" not in json.dumps(result)
            before = len(sdk.calls)
            denied = data(
                await session.call_tool(
                    "administration_read",
                    {
                        "profile_id": "other",
                        "operation": {"kind": "participants", "chat_id": GROUP},
                    },
                )
            )
            assert denied["error"]["code"] == "read_not_allowed"
            assert len(sdk.calls) == before
            blocked = data(
                await session.call_tool(
                    "delivery_preview",
                    {
                        "profile_id": "work",
                        "recipients": [GROUP],
                        "text": "no implied send permission",
                    },
                )
            )
            assert blocked["error"]["code"] == "recipient_not_allowed"


async def test_mtproto_bot_reads_arbitrary_messages_members_and_avatar_history(tmp_path, sdk):
    sdk.me.bot = True
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "robot": Profile(
                kind="bot",
                bot_backend="mtproto",
                identity={"id": "7"},
                read_mode="selected",
                read_chats=[GROUP, "8"],
            )
        },
    )
    async with running(settings, adapter_factory=make_adapter) as app:
        async with client(app, settings) as session:
            result = data(
                await session.call_tool(
                    "messages_get",
                    {
                        "profile_id": "robot",
                        "chat_id": GROUP,
                        "message_ids": ["88"],
                    },
                )
            )
            assert result["ok"], result
            assert result["data"]["items"][0]["text"] == "arbitrary selected post"
            assert result["data"]["source"] == "telegram"
            members = data(
                await session.call_tool(
                    "administration_read",
                    {
                        "profile_id": "robot",
                        "operation": {"kind": "participants", "chat_id": GROUP},
                    },
                )
            )
            assert members["data"]["items"][0]["id"] == "8"
            photos = data(
                await session.call_tool(
                    "account_read",
                    {
                        "profile_id": "robot",
                        "operation": {"kind": "photos", "user_id": "8"},
                    },
                )
            )
            assert photos["data"]["items"][0]["id"] == "31"
            assert "file_reference" not in json.dumps(photos)
            assert "access_hash" not in json.dumps(photos)


async def test_mtproto_bot_ack_is_local_and_keeps_edits_after_review_and_restart(tmp_path, sdk):
    sdk.me.bot = True
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "robot": Profile(
                kind="bot",
                bot_backend="mtproto",
                polling=True,
                identity={"id": "7"},
                read_mode="selected",
                read_chats=[GROUP],
            )
        },
    )
    async with running(settings, adapter_factory=make_adapter) as app:
        async with client(app, settings) as session:
            selected = data(
                await session.call_tool(
                    "messages_get",
                    {"profile_id": "robot", "chat_id": GROUP, "message_ids": ["88"]},
                )
            )
            row = Message.model_validate(selected["data"]["items"][0])
            profiles = data(await session.call_tool("profiles_list", {}))["data"]["profiles"]
            assert profiles[0]["polling"]["source"] == "mtproto_updates"
            assert profiles[0]["polling"]["status"] == "running"
            # A real persisted collected update; only the external SDK is replaced.
            store = Store(tmp_path)
            try:
                store.chat("robot", Chat(id=GROUP, title="Engineering", kind="group"))
                store.save_messages([row], ("bot_offset:robot", 2), pending_update_id=1)
            finally:
                store.close()
            reviewed = data(await session.call_tool("inbox_get", {"profile_id": "robot"}))["data"][
                "chats"
            ][0]
            row.text = "Edited after review"
            store = Store(tmp_path)
            try:
                store.save_messages([row], ("bot_offset:robot", 3), pending_update_id=2)
            finally:
                store.close()
            ack = data(
                await session.call_tool(
                    "inbox_ack",
                    {
                        "profile_id": "robot",
                        "chat_id": GROUP,
                        "through_message_id": "88",
                        "snapshot_id": reviewed["snapshot_id"],
                    },
                )
            )
            assert ack["ok"], ack
            pending = data(await session.call_tool("inbox_get", {"profile_id": "robot"}))["data"]
            assert pending["source"] == "local_unprocessed"
            assert pending["chats"][0]["messages"][0]["text"] == "Edited after review"
    async with running(settings, adapter_factory=make_adapter) as app:
        async with client(app, settings) as session:
            pending = data(await session.call_tool("inbox_get", {"profile_id": "robot"}))["data"][
                "chats"
            ][0]
            assert pending["messages"][0]["text"] == "Edited after review"
            ack = data(
                await session.call_tool(
                    "inbox_ack",
                    {
                        "profile_id": "robot",
                        "chat_id": GROUP,
                        "through_message_id": "88",
                        "snapshot_id": pending["snapshot_id"],
                    },
                )
            )
            assert ack["ok"], ack
            assert not data(await session.call_tool("inbox_get", {"profile_id": "robot"}))["data"][
                "chats"
            ]


async def test_full_metadata_and_common_chats_preserve_policy_and_safe_evidence(tmp_path, sdk):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user", identity={"id": "7"}, read_mode="selected", read_chats=[GROUP, "8"]
            )
        },
    )
    async with running(settings, adapter_factory=make_adapter) as app:
        async with client(app, settings) as session:
            requests = [
                ("administration_read", {"kind": "chat", "chat_id": GROUP}),
                ("administration_read", {"kind": "member", "chat_id": GROUP, "user_id": "8"}),
                ("administration_read", {"kind": "audit", "chat_id": GROUP}),
                ("administration_read", {"kind": "common_chats", "user_id": "8"}),
                ("account_read", {"kind": "me"}),
                ("account_read", {"kind": "user", "user_id": "8"}),
                ("account_read", {"kind": "status", "user_id": "8"}),
            ]
            results = []
            for tool, operation in requests:
                result = data(
                    await session.call_tool(tool, {"profile_id": "work", "operation": operation})
                )
                assert result["ok"], result
                assert "access_hash" not in json.dumps(result)
                assert "SECRET_PHONE" not in json.dumps(result)
                results.append(result["data"])
            assert results[0]["item"]["about"] == "Team description"
            assert results[0]["item"]["linked_chat_id"] is None
            assert results[1]["item"]["admin_rights"]["delete_messages"] is True
            assert results[2]["items"][0]["action"]["new_value"] == "After"
            assert [x["id"] for x in results[3]["items"]] == [GROUP]
            assert results[5]["item"]["birthday"]["year"] == 1980
            assert results[5]["item"]["personal_channel_id"] is None


async def test_participant_cursor_is_scoped_expiring_and_revocable(tmp_path, sdk, monkeypatch):
    sdk.participant_users.append(types.User(id=9, first_name="Second"))
    profile = Profile(kind="user", identity={"id": "7"}, read_mode="selected", read_chats=[GROUP])
    settings = Settings(data_dir=tmp_path, profiles={"work": profile})
    async with running(settings, adapter_factory=make_adapter) as app:
        async with client(app, settings) as session:
            arguments = {
                "profile_id": "work",
                "operation": {"kind": "participants", "chat_id": GROUP, "limit": 1},
            }
            first = data(await session.call_tool("administration_read", arguments))["data"]
            assert first["items"][0]["id"] == "8"
            arguments["operation"]["cursor"] = first["next_cursor"]
            second = data(await session.call_tool("administration_read", arguments))["data"]
            assert second["items"][0]["id"] == "9"
            profile.read_chats = []
            denied = data(await session.call_tool("administration_read", arguments))
            assert denied["error"]["code"] == "read_not_allowed"
            profile.read_chats = [GROUP]
            monkeypatch.setattr(
                "teleloom.administration.utcnow",
                lambda: datetime.now(UTC) + timedelta(minutes=20),
                raising=False,
            )
            expired = data(await session.call_tool("administration_read", arguments))
            assert not expired["ok"]
            assert expired["error"]["code"] == "invalid_cursor"


async def test_selected_chat_metadata_and_real_exported_thread_link(tmp_path, sdk):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user", identity={"id": "7"}, read_mode="selected", read_chats=[GROUP]
            )
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        chat = data(
            await session.call_tool(
                "administration_read",
                {"profile_id": "work", "operation": {"kind": "chat", "chat_id": GROUP}},
            )
        )
        assert chat["ok"], chat
        item = chat["data"]["item"]
        assert (item["unread_count"], item["archived"], item["unread_mark"]) == (3, True, True)
        assert item["latest_message"]["text"] == "Latest selected message"
        link = data(
            await session.call_tool(
                "administration_read",
                {
                    "profile_id": "work",
                    "operation": {
                        "kind": "message_link",
                        "chat_id": GROUP,
                        "message_id": "88",
                        "thread": True,
                    },
                },
            )
        )
        assert link["ok"], link
        assert link["data"]["item"]["link"] == "https://t.me/c/123/88?thread=77"
        request = next(
            x for x in sdk.calls if isinstance(x, functions.channels.ExportMessageLinkRequest)
        )
        assert request.thread is True
        assert "private embed" not in json.dumps(link)


async def test_common_chat_cursor_does_not_expose_outside_policy_peer(tmp_path, sdk):
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user", identity={"id": "7"}, read_mode="selected", read_chats=[GROUP, "8"]
            )
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        args = {
            "profile_id": "work",
            "operation": {"kind": "common_chats", "user_id": "8", "limit": 1},
        }
        first = data(await session.call_tool("administration_read", args))["data"]
        assert first["items"] == []
        assert first["coverage"]["offset"] is None
        assert len(first["next_cursor"]) == 32
        args["operation"]["cursor"] = first["next_cursor"]
        second = data(await session.call_tool("administration_read", args))["data"]
        assert [x["id"] for x in second["items"]] == [GROUP]


async def test_admin_filter_query_matches_values_and_privacy_bot_read_variants(tmp_path, sdk):
    sdk.user.bot = True
    settings = Settings(
        data_dir=tmp_path,
        profiles={
            "work": Profile(
                kind="user",
                identity={"id": "7"},
                manage_scopes=["account"],
                read_mode="selected",
                read_chats=[GROUP, "8"],
            )
        },
    )
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        for filter_ in ("admins", "banned"):
            result = data(
                await session.call_tool(
                    "administration_read",
                    {
                        "profile_id": "work",
                        "operation": {"kind": "participants", "chat_id": GROUP, "filter": filter_},
                    },
                )
            )
            assert result["ok"], result
            assert result["data"]["items"][0]["id"] == "8"
        empty = data(
            await session.call_tool(
                "administration_read",
                {
                    "profile_id": "work",
                    "operation": {
                        "kind": "participants",
                        "chat_id": GROUP,
                        "filter": "admins",
                        "query": "first_name",
                    },
                },
            )
        )
        assert empty["data"]["items"] == []
        privacy = data(
            await session.call_tool(
                "account_read", {"profile_id": "work", "operation": {"kind": "privacy"}}
            )
        )
        assert privacy["data"]["items"] == [{"kind": "PrivacyValueDisallowAll"}]
        bot = data(
            await session.call_tool(
                "account_read",
                {"profile_id": "work", "operation": {"kind": "bot_info", "user_id": "8"}},
            )
        )
        assert bot["data"]["item"]["bot_info"]["description"] == "Public bot biography"


async def test_mtproto_bot_auth_failure_recommends_same_backend(tmp_path, sdk):
    async def unauthorized():
        return False

    sdk.is_user_authorized = unauthorized
    settings = Settings(
        data_dir=tmp_path,
        profiles={"robot": Profile(kind="bot", bot_backend="mtproto", identity={"id": "7"})},
    )
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                "account_read", {"profile_id": "robot", "operation": {"kind": "me"}}
            )
        )
        assert result["error"]["code"] == "auth_required"
        assert "teleloom auth bot --backend mtproto" in result["error"]["message"]


@pytest.mark.parametrize("kind", ["chat", "user", "member"])
async def test_wrong_peer_metadata_is_withheld(tmp_path, sdk, kind):
    settings = Settings(
        data_dir=tmp_path, profiles={"work": Profile(kind="user", identity={"id": "7"})}
    )
    if kind == "chat":
        sdk.full_group.id = 999
        operation = {"kind": "chat", "chat_id": GROUP}
    elif kind == "member":
        operation = {"kind": "member", "chat_id": GROUP, "user_id": "9"}
    else:
        sdk.user.id = 9
        operation = {"kind": "user", "user_id": "8"}
    async with running(settings, make_adapter) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                "account_read" if kind == "user" else "administration_read",
                {"profile_id": "work", "operation": operation},
            )
        )
        assert result["error"]["code"] == "peer_identity_changed", result
        assert result["data"] == {}
