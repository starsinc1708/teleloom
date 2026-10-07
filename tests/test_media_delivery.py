"""Public media delivery contract, with real owner/state/process seams."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from telethon import functions, types

from teleloom.config import Limits, Profile, Settings
from tests.fakes import data
from tests.media_fakes import MediaSDK, encoded_image, encoded_voice
from tests.telegram_fakes import CHAT, factory
from tests.test_jobs import complete
from tests.test_transport import client, running


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["send_file", "send_album", "send_voice", "send_sticker", "send_gif"]
)
@pytest.mark.parametrize("owner_authorized", [False, True])
async def test_bot_media_uses_real_aiogram_multipart_models_and_frozen_bytes(
    tmp_path, monkeypatch, kind, owner_authorized
):
    from aiogram import Bot, methods
    from aiogram import types as bot_types
    from aiogram.client.session.base import BaseSession

    from teleloom.adapters import make_adapter

    transmitted = []
    expected_method = {
        "send_file": methods.SendDocument,
        "send_album": methods.SendMediaGroup,
        "send_voice": methods.SendVoice,
        "send_sticker": methods.SendSticker,
        "send_gif": methods.SendAnimation,
    }[kind]

    class API(BaseSession):
        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            yield b""

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, methods.GetMe):
                return bot_types.User(id=123456, is_bot=True, first_name="Bot")
            chat = bot_types.Chat(id=100, type="private", first_name="Target")
            if isinstance(method, methods.GetChat):
                return chat
            assert isinstance(method, expected_method), type(method)
            files = {}
            serialized = json.loads(
                self.prepare_value(method.model_dump(warnings=False), bot=bot, files=files)
            )
            assert serialized["chat_id"] == 100
            assert serialized.get("parse_mode") is None
            if kind == "send_album":
                media = serialized["media"]
                assert len(media) == 2 and all(item["type"] == "photo" for item in media)
                assert (
                    media[0]["caption"] == "<b>Plain caption</b>" and "parse_mode" not in media[0]
                )
            transmitted.extend(
                [b"".join([chunk async for chunk in file.read(bot)]) for file in files.values()]
            )
            response = bot_types.Message(message_id=20, date=datetime.now(UTC), chat=chat)
            return (
                [response, response.model_copy(update={"message_id": 21})]
                if kind == "send_album"
                else response
            )

    api = API()
    monkeypatch.setenv("TELELOOM_HELPER_BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setattr("teleloom.adapters.Bot", lambda token, session: Bot(token, session=api))
    root = tmp_path / "selected"
    root.mkdir()
    extension = {
        "send_file": "txt",
        "send_album": "png",
        "send_voice": "ogg",
        "send_sticker": "webp",
        "send_gif": "gif",
    }[kind]
    content = (
        encoded_voice()
        if kind == "send_voice"
        else encoded_image("WEBP", (512, 256))
        if kind == "send_sticker"
        else encoded_image("GIF")
        if kind == "send_gif"
        else encoded_image()
        if kind == "send_album"
        else b"Exact document bytes"
    )
    source = root / ("original." + extension)
    source.write_bytes(content)
    operation = {"kind": kind, "chat_id": "100"}
    if kind == "send_album":
        second = root / "second.png"
        second.write_bytes(content)
        operation["source_paths"] = [str(source), str(second)]
    else:
        operation["source_path"] = str(source)
    if kind not in {"send_voice", "send_sticker"}:
        operation["caption"] = "<b>Plain caption</b>"
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={
            "helper": Profile(
                kind="bot",
                polling=False,
                send_chats=[] if owner_authorized else ["100"],
                file_roots=[] if owner_authorized else [str(root)],
            )
        },
    )
    async with running(settings, make_adapter) as owner, client(owner, settings) as mcp:
        preview = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "helper",
                    "operation": operation,
                    "owner_authorized": owner_authorized,
                },
            )
        )
        assert preview["ok"], preview
        plan = preview["data"]
        assert transmitted == []
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "helper",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        status = await complete(mcp, "helper", job)
        assert status["status"] == "completed", status
        assert transmitted == [content] * (2 if kind == "send_album" else 1)
        assert status["deliveries"][0]["receipt"]["message_ids"] == (
            ["20", "21"] if kind == "send_album" else ["20"]
        )


@pytest.mark.asyncio
async def test_album_daily_budget_counts_each_outgoing_message_before_upload(tmp_path):
    import asyncio

    from teleloom.store import Store

    root = tmp_path / "selected"
    root.mkdir()
    paths = [root / "one.png", root / "two.png"]
    for path in paths:
        path.write_bytes(encoded_image())
    sdk = MediaSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        limits=Limits(daily_messages=2),
        profiles={"personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        store = Store(settings.data_dir)
        with store.db:
            store.set_state("daily:personal:" + datetime.now(UTC).date().isoformat(), 1)
        plan = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send_album",
                        "chat_id": CHAT,
                        "source_paths": list(map(str, paths)),
                    },
                },
            )
        )["data"]
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        await asyncio.sleep(0.4)
        status = data(
            await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
        )["data"]
        assert status["status"] == "queued" and not sdk.uploaded, status
        assert status["deliveries"][0]["status"] == "pending"
        store.close()


@pytest.mark.asyncio
async def test_file_send_is_immutable_confirmed_and_uses_actual_send_media_rpc(tmp_path):
    root = tmp_path / "selected"
    root.mkdir()
    source = root / "report.txt"
    source.write_bytes(b"Exact immutable original")
    sdk = MediaSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={"personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        preview = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send_file",
                        "chat_id": CHAT,
                        "source_path": str(source),
                        "caption": "Exact caption",
                    },
                },
            )
        )
        assert preview["ok"], preview
        plan = preview["data"]
        assert not sdk.uploaded
        assert plan["preview"]["operation"]["files"][0]["sha256"]
        execute = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
        }
        denied = data(await mcp.call_tool("delivery_execute", execute))
        assert denied["error"]["code"] == "confirmation_required"
        first = data(await mcp.call_tool("delivery_execute", {**execute, "confirmed": True}))[
            "data"
        ]
        second = data(await mcp.call_tool("delivery_execute", {**execute, "confirmed": True}))[
            "data"
        ]
        assert first["job_id"] == second["job_id"]
        status = await complete(mcp, "personal", first["job_id"])
        assert status["status"] == "completed", status
        assert sdk.uploaded == [("report.txt", b"Exact immutable original")]
        assert status["deliveries"][0]["receipt"]["message_ids"] == ["20"]


@pytest.mark.asyncio
async def test_worker_uploads_verified_bytes_even_when_original_changes_during_upload_and_unknown_never_replays(
    tmp_path,
):
    root = tmp_path / "selected"
    root.mkdir()
    source = root / "report.txt"
    source.write_bytes(b"Exact immutable original")
    sdk = MediaSDK()
    sdk.fail_send = True
    sdk.after_upload = lambda: source.write_bytes(b"Changed after validation")
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={"personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": "send_file",
                        "chat_id": CHAT,
                        "source_path": str(source),
                        "caption": "Exact caption",
                    },
                },
            )
        )["data"]
        execute = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        job = data(await mcp.call_tool("delivery_execute", execute))["data"]["job_id"]
        status = await complete(mcp, "personal", job)
        assert status["status"] == "needs_review" and status["deliveries"][0]["status"] == "unknown"
        assert "PRIVATE_NETWORK_DETAIL" not in str(status)
        assert sdk.uploaded == [("report.txt", b"Exact immutable original")]
        again = data(await mcp.call_tool("delivery_execute", execute))["data"]
        assert again["job_id"] == job
        resumed = data(
            await mcp.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )
        assert resumed["error"]["code"] == "delivery_unknown"
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        status = data(
            await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
        )["data"]
        assert status["status"] == "needs_review"
    assert len(sdk.uploaded) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_album", "send_voice", "send_sticker", "send_gif"])
async def test_media_encoding_uses_exact_native_types_attributes_and_album_rpc(tmp_path, kind):
    class EncodingSDK(MediaSDK):
        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            if isinstance(request, functions.messages.UploadMediaRequest):
                assert kind == "send_album" and isinstance(
                    request.media, types.InputMediaUploadedPhoto
                )
                return types.MessageMediaPhoto(
                    photo=types.Photo(
                        id=70,
                        access_hash=777,
                        file_reference=b"PRIVATE",
                        date=datetime.now(UTC),
                        sizes=[],
                        dc_id=2,
                    )
                )
            if kind == "send_album":
                assert isinstance(request, functions.messages.SendMultiMediaRequest)
                assert len(request.multi_media) == 2
                assert [item.message for item in request.multi_media] == ["Album caption", ""]
                assert all(
                    isinstance(item.media, types.InputMediaPhoto) for item in request.multi_media
                )
                assert request.multi_media[0].random_id != request.multi_media[1].random_id
                return types.Updates(
                    updates=[
                        types.UpdateMessageID(id=20 + index, random_id=item.random_id)
                        for index, item in enumerate(request.multi_media)
                    ]
                    + [
                        types.UpdateNewChannelMessage(
                            message=types.Message(
                                id=20 + index,
                                peer_id=types.PeerChannel(100),
                                date=datetime.now(UTC),
                                message=item.message,
                                out=True,
                            ),
                            pts=index + 1,
                            pts_count=1,
                        )
                        for index, item in enumerate(request.multi_media)
                    ],
                    users=[],
                    chats=[],
                    date=datetime.now(UTC),
                    seq=1,
                )
            else:
                assert isinstance(request, functions.messages.SendMediaRequest)
                assert isinstance(request.media, types.InputMediaUploadedDocument)
                if kind == "send_voice":
                    audio = next(
                        a
                        for a in request.media.attributes
                        if isinstance(a, types.DocumentAttributeAudio)
                    )
                    assert audio.voice is True and audio.duration == 1
                    assert request.media.mime_type == "audio/ogg"
                elif kind == "send_sticker":
                    assert any(
                        isinstance(a, types.DocumentAttributeSticker)
                        for a in request.media.attributes
                    )
                    shape = next(
                        a
                        for a in request.media.attributes
                        if isinstance(a, types.DocumentAttributeImageSize)
                    )
                    assert (shape.w, shape.h) == (512, 256)
                else:
                    assert any(
                        isinstance(a, types.DocumentAttributeAnimated)
                        for a in request.media.attributes
                    )
                    assert request.media.mime_type == "image/gif"
            return types.UpdateShortSentMessage(id=20, pts=1, pts_count=1, date=datetime.now(UTC))

    root = tmp_path / "selected"
    root.mkdir()
    if kind == "send_album":
        paths = [root / "first.png", root / "second.png"]
        for path in paths:
            path.write_bytes(encoded_image())
        operation = {
            "kind": kind,
            "chat_id": CHAT,
            "source_paths": list(map(str, paths)),
            "caption": "Album caption",
        }
    else:
        path = (
            root
            / {"send_voice": "note.ogg", "send_sticker": "sticker.webp", "send_gif": "clip.gif"}[
                kind
            ]
        )
        content = (
            encoded_voice()
            if kind == "send_voice"
            else encoded_image("WEBP", (512, 256))
            if kind == "send_sticker"
            else encoded_image("GIF")
        )
        path.write_bytes(content)
        operation = {"kind": kind, "chat_id": CHAT, "source_path": str(path)}
    sdk = EncodingSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={"personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        result = data(
            await mcp.call_tool(
                "media_operation_preview", {"profile_id": "personal", "operation": operation}
            )
        )
        assert result["ok"], result
        plan = result["data"]
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        status = await complete(mcp, "personal", job)
        assert status["status"] == "completed", status
        assert len(sdk.uploaded) == (2 if kind == "send_album" else 1)
        assert status["deliveries"][0]["receipt"]["message_ids"] == (
            ["20", "21"] if kind == "send_album" else ["20"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_file", "send_album"])
async def test_scheduled_media_preserves_exact_reply_topic_and_plain_utf16_caption(tmp_path, kind):
    class ForumSDK(MediaSDK):
        async def get_entity(self, value):
            return types.Channel(
                id=100,
                title="Selected forum",
                date=datetime.now(UTC),
                photo=types.ChatPhotoEmpty(),
                megagroup=True,
                forum=True,
            )

        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            if isinstance(request, functions.messages.UploadMediaRequest):
                assert kind == "send_album"
                return types.MessageMediaPhoto(
                    photo=types.Photo(
                        id=70,
                        access_hash=777,
                        file_reference=b"PRIVATE",
                        date=datetime.now(UTC),
                        sizes=[],
                        dc_id=2,
                    )
                )
            assert isinstance(
                request,
                functions.messages.SendMediaRequest
                if kind == "send_file"
                else functions.messages.SendMultiMediaRequest,
            )
            assert isinstance(request.reply_to, types.InputReplyToMessage)
            assert request.reply_to.reply_to_msg_id == 30 and request.reply_to.top_msg_id == 5
            assert request.schedule_date == scheduled
            if kind == "send_file":
                assert request.message == caption and request.entities == []
            else:
                assert [item.message for item in request.multi_media] == [caption, ""]
                assert all(item.entities == [] for item in request.multi_media)
            request._bytes()
            identifiers = [20] if kind == "send_file" else [20, 21]
            random_ids = (
                [request.random_id]
                if kind == "send_file"
                else [item.random_id for item in request.multi_media]
            )
            return types.Updates(
                updates=[
                    types.UpdateMessageID(id=identifier, random_id=random_id)
                    for identifier, random_id in reversed(
                        list(zip(identifiers, random_ids, strict=True))
                    )
                ]
                + [
                    types.UpdateNewScheduledMessage(
                        message=types.Message(
                            id=identifier,
                            peer_id=types.PeerChannel(100),
                            date=scheduled,
                            message=caption if index == 0 else "",
                            out=True,
                        )
                    )
                    for index, identifier in enumerate(identifiers)
                ],
                users=[],
                chats=[],
                date=datetime.now(UTC),
                seq=1,
            )

    root = tmp_path / "selected"
    root.mkdir()
    paths = [root / "first.png", root / "second.png"]
    for path in paths:
        path.write_bytes(encoded_image())
    sdk = ForumSDK()
    sdk.rows = [
        types.Message(
            id=identifier,
            peer_id=types.PeerChannel(100),
            date=datetime.now(UTC),
            message=f"Selected source {identifier}",
        )
        for identifier in (30, 5)
    ]
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={
            "personal": Profile(
                kind="user",
                send_chats=[CHAT],
                read_mode="selected",
                read_chats=[CHAT],
                file_roots=[str(root)],
            )
        },
    )
    scheduled = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    caption = "🙂" * 512
    operation = {
        "kind": kind,
        "chat_id": CHAT,
        "reply_to_message_id": "30",
        "topic_id": "5",
        "schedule_date": scheduled.isoformat(),
        "caption": caption,
        **(
            {"source_path": str(paths[0])}
            if kind == "send_file"
            else {"source_paths": list(map(str, paths))}
        ),
    }
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        for changed, code in (
            ({"caption": caption + "🙂"}, "invalid_caption"),
            ({"schedule_date": scheduled.replace(tzinfo=None).isoformat()}, "invalid_time"),
            ({"topic_id": "99"}, "message_not_found"),
        ):
            rejected = data(
                await mcp.call_tool(
                    "media_operation_preview",
                    {"profile_id": "personal", "operation": {**operation, **changed}},
                )
            )
            assert rejected["error"]["code"] == code, rejected
            assert not sdk.uploaded
        preview = data(
            await mcp.call_tool(
                "media_operation_preview", {"profile_id": "personal", "operation": operation}
            )
        )
        assert preview["ok"], preview
        plan = preview["data"]
        assert plan["preview"]["operation"]["caption"] == caption
        job = data(
            await mcp.call_tool(
                "delivery_execute",
                {
                    "profile_id": "personal",
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "confirmed": True,
                },
            )
        )["data"]["job_id"]
        status = await complete(mcp, "personal", job)
        assert status["status"] == "completed", status
        native = status["deliveries"][0]["receipt"]
        assert native["message_ids"] == (["20"] if kind == "send_file" else ["20", "21"])
        assert native["schedule_accepted"] is True and native["delivery_confirmed"] is False
        assert datetime.fromisoformat(native["scheduled_for"]) == scheduled
        assert len(sdk.uploaded) == (1 if kind == "send_file" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_file", "send_album"])
async def test_incomplete_native_media_receipts_remain_unknown_without_replay(tmp_path, kind):
    class IncompleteSDK(MediaSDK):
        async def __call__(self, request, *args, **kwargs):
            self.calls.append(request)
            if isinstance(request, functions.messages.UploadMediaRequest):
                return types.MessageMediaPhoto(
                    photo=types.Photo(
                        id=70,
                        access_hash=777,
                        file_reference=b"PRIVATE",
                        date=datetime.now(UTC),
                        sizes=[],
                        dc_id=2,
                    )
                )
            assert isinstance(
                request,
                functions.messages.SendMediaRequest
                if kind == "send_file"
                else functions.messages.SendMultiMediaRequest,
            )
            return types.Updates(
                updates=[]
                if kind == "send_file"
                else [
                    types.UpdateNewChannelMessage(
                        message=types.Message(
                            id=20,
                            peer_id=types.PeerChannel(100),
                            date=datetime.now(UTC),
                            message="Only one album receipt",
                            out=True,
                        ),
                        pts=1,
                        pts_count=1,
                    )
                ],
                users=[],
                chats=[],
                date=datetime.now(UTC),
                seq=1,
            )

    root = tmp_path / "selected"
    root.mkdir()
    paths = [root / "first.png", root / "second.png"]
    for path in paths:
        path.write_bytes(encoded_image())
    sdk = IncompleteSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        profiles={"personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)])},
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {
                        "kind": kind,
                        "chat_id": CHAT,
                        **(
                            {"source_path": str(paths[0])}
                            if kind == "send_file"
                            else {"source_paths": list(map(str, paths))}
                        ),
                    },
                },
            )
        )["data"]
        execute = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        job = data(await mcp.call_tool("delivery_execute", execute))["data"]["job_id"]
        status = await complete(mcp, "personal", job)
        assert status["status"] == "needs_review", status
        assert status["deliveries"][0]["status"] == "unknown"
        assert status["error"]["code"] == "delivery_unknown"
        assert data(await mcp.call_tool("delivery_execute", execute))["data"]["job_id"] == job
        resume = data(
            await mcp.call_tool(
                "jobs_control", {"profile_id": "personal", "job_id": job, "action": "resume"}
            )
        )
        assert resume["error"]["code"] == "delivery_unknown"
        calls = len(sdk.calls)
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        status = data(
            await mcp.call_tool("jobs_status", {"profile_id": "personal", "job_id": job})
        )["data"]
        assert status["status"] == "needs_review"
    assert len(sdk.calls) == calls and len(sdk.uploaded) == (1 if kind == "send_file" else 2)


@pytest.mark.asyncio
async def test_confirmed_bare_upload_returns_temporary_account_handle_reusable_without_second_upload(
    tmp_path,
):
    root = tmp_path / "selected"
    root.mkdir()
    source = root / "report.txt"
    source.write_bytes(b"Exact immutable original")
    sdk = MediaSDK()
    settings = Settings(
        data_dir=tmp_path / "owner",
        limits=Limits(interval_seconds=1),
        profiles={
            "personal": Profile(kind="user", send_chats=[CHAT], file_roots=[str(root)]),
            "other": Profile(kind="user", send_chats=[CHAT]),
        },
    )
    async with running(settings, factory(sdk)) as owner, client(owner, settings) as mcp:
        plan = data(
            await mcp.call_tool(
                "media_operation_preview",
                {
                    "profile_id": "personal",
                    "operation": {"kind": "upload_file", "source_path": str(source)},
                },
            )
        )["data"]
        assert (
            plan["preview"]["targets"] == [{"kind": "account"}]
            and plan["preview"]["recipients"] == []
        )
        assert not sdk.uploaded
        execute = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        job = data(await mcp.call_tool("delivery_execute", execute))["data"]["job_id"]
        status = await complete(mcp, "personal", job)
        assert status["status"] == "completed", status
        receipt = status["deliveries"][0]["receipt"]["upload"]
        assert receipt["handle"] and receipt["expires_at"]
        assert len(sdk.uploaded) == 1 and sdk.calls == []
        source.unlink()
        args = {
            "profile_id": "personal",
            "operation": {
                "kind": "send_file",
                "chat_id": CHAT,
                "upload_handle": receipt["handle"],
                "caption": "Exact caption",
            },
        }
        send = data(await mcp.call_tool("media_operation_preview", args))
        assert send["ok"], send
        plan = send["data"]
        execute = {
            "profile_id": "personal",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "confirmed": True,
        }
        job = data(await mcp.call_tool("delivery_execute", execute))["data"]["job_id"]
        assert (await complete(mcp, "personal", job))["status"] == "completed"
        assert len(sdk.uploaded) == 1 and len(sdk.calls) == 1
        foreign = data(
            await mcp.call_tool("media_operation_preview", {**args, "profile_id": "other"})
        )
        assert foreign["error"]["code"] == "media_handle_not_found"
