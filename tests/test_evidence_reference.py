"""T08: exact originals through an owned, frozen evidence reference."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from telethon import functions, types

from teleloom.config import Profile, Settings
from tests.fakes import data
from tests.telegram_fakes import CHAT, SDK, factory
from tests.test_jobs import complete
from tests.test_reading_jobs_v02 import NOW, ReadingAPI
from tests.test_transport import client, running


def key(chat_id: str, message_id: str) -> dict[str, str]:
    return {"chat_id": chat_id, "message_id": message_id}


def args(since_days: int = 20) -> dict[str, object]:
    return {
        "profile_id": "personal",
        "chat_ids": ["100", "200"],
        "since": (NOW - timedelta(days=since_days)).isoformat(),
        "until": NOW.isoformat(),
    }


class CapturingAPI(ReadingAPI):
    """Record the live adapter instance so drift can be injected through real seams."""

    instances: list["CapturingAPI"] = []

    def __init__(self, *args):
        super().__init__(*args)
        self.history_calls = 0
        CapturingAPI.instances.append(self)

    async def history(self, chat, **kwargs):
        self.history_calls += 1
        return await super().history(chat, **kwargs)


async def frozen_job(server) -> str:
    started = data(await server.call_tool("digest_context_many_start", args()))["data"]
    await complete(server, "personal", started["job_id"])
    return started["job_id"]


@pytest.mark.asyncio
async def test_reference_is_separate_from_cursor_and_serves_frozen_originals_after_drift(
    tmp_path,
):
    CapturingAPI.instances = []
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")},
    )
    async with running(settings, CapturingAPI) as app, client(app, settings) as server:
        job = await frozen_job(server)
        adapter = CapturingAPI.instances[-1]
        history_calls = adapter.history_calls

        first = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 1}
            )
        )["data"]
        reference = first["evidence_ref"]
        version = first["source_version"]
        assert isinstance(reference, str) and reference
        assert reference != first["next_cursor"]
        assert isinstance(version, str) and version

        # Paginating the same frozen snapshot keeps the same reference and source version.
        second = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "limit": 1,
                    "cursor": first["next_cursor"],
                },
            )
        )["data"]
        assert second["evidence_ref"] == reference
        assert second["source_version"] == version

        # Edit and delete drift in the live source and the mutable local index.
        adapter.rows[0].text = "Edited after collection"
        adapter.rows[0].edited_at = NOW
        adapter.store.save_messages([adapter.rows[0]])
        adapter.store.delete_messages("personal", "200", ["2"])
        assert adapter.store.messages("personal", "100")[0].text == "Edited after collection"
        assert adapter.store.messages("personal", "200") == []

        original = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1"), key("200", "2")],
                },
            )
        )["data"]
        assert [(row["chat_id"], row["id"]) for row in original["items"]] == [
            ("100", "1"),
            ("200", "2"),
        ]
        assert original["items"][0]["text"] == "Old original"
        assert original["items"][1]["text"] == "Recent original"
        assert original["source_version"] == version
        assert original["evidence_ref"] == reference
        assert original["coverage"]["chats"]
        assert adapter.history_calls == history_calls  # zero live history RPC

        unknown = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("100", "999")],
                },
            )
        )
        assert unknown["error"]["code"] == "message_not_in_snapshot"

        without_reference = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "message_keys": [key("100", "1")],
                },
            )
        )
        assert without_reference["error"]["code"] == "invalid_reference"

        # A reference alone exposes coverage and the verifiable source version, never items.
        envelope = data(
            await server.call_tool(
                "jobs_results",
                {"profile_id": "personal", "job_id": job, "evidence_ref": reference},
            )
        )["data"]
        assert envelope["items"] == []
        assert envelope["source_version"] == version
        assert envelope["coverage"]["chats"]

        foreign = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "work",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1")],
                },
            )
        )
        assert foreign["error"]["code"] == "job_not_found"


@pytest.mark.asyncio
async def test_reference_rechecks_profile_policy_and_generation_at_every_resolve(tmp_path):
    CapturingAPI.instances = []
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")},
    )
    async with running(settings, CapturingAPI) as app, client(app, settings) as server:
        job = await frozen_job(server)
        reference = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 1}
            )
        )["data"]["evidence_ref"]

        # Narrowing the read policy revokes a mixed frozen selection as a whole.
        settings.profiles["personal"].read_mode = "selected"
        settings.profiles["personal"].read_chats = ["100"]
        revoked = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1")],
                },
            )
        )
        assert revoked["error"]["code"] == "read_not_allowed"
        settings.profiles["personal"].read_mode = "all"
        settings.profiles["personal"].read_chats = []

    replaced = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(replaced, CapturingAPI) as app, client(app, replaced) as server:
        stale = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1")],
                },
            )
        )
        assert stale["error"]["code"] == "account_changed"


@pytest.mark.asyncio
async def test_reference_survives_restart_with_the_same_verifiable_source_version(tmp_path):
    CapturingAPI.instances = []
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, CapturingAPI) as app, client(app, settings) as server:
        job = await frozen_job(server)
        first = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 1}
            )
        )["data"]
        reference = first["evidence_ref"]
        version = first["source_version"]

    async with running(settings, CapturingAPI) as app, client(app, settings) as server:
        resolved = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1")],
                },
            )
        )["data"]
        assert resolved["source_version"] == version
        assert resolved["items"][0]["text"] == "Old original"


@pytest.mark.asyncio
async def test_expired_reference_reports_errors_and_preserves_durable_originals(
    tmp_path, monkeypatch
):
    clock = NOW
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: clock)
    CapturingAPI.instances = []
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, CapturingAPI) as app, client(app, settings) as server:
        job = await frozen_job(server)
        first = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 1}
            )
        )["data"]
        reference = first["evidence_ref"]
        cursor = first["next_cursor"]

        # The reference pins its snapshot past the short-lived pagination cursor.
        clock += timedelta(minutes=16)
        stale_cursor = data(
            await server.call_tool(
                "jobs_results",
                {"profile_id": "personal", "job_id": job, "cursor": cursor},
            )
        )
        assert stale_cursor["error"]["code"] == "invalid_cursor"
        pinned = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1")],
                },
            )
        )["data"]
        assert pinned["items"][0]["text"] == "Old original"

        # Beyond its own bounded lifetime the reference expires without touching originals.
        clock += timedelta(minutes=15)
        expired = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1")],
                },
            )
        )
        assert expired["error"]["code"] == "reference_expired"

        # The job's durable originals survive an expired derivative reference.
        durable = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 10}
            )
        )["data"]
        texts = sorted(row["text"] for row in durable["items"])
        assert texts == ["Old original", "Recent original"]


@pytest.mark.asyncio
async def test_reference_separates_paging_and_rejects_keys_outside_the_snapshot(tmp_path):
    CapturingAPI.instances = []
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user"), "work": Profile(kind="user")}
    )
    async with running(settings, CapturingAPI) as app, client(app, settings) as server:
        job = await frozen_job(server)
        first = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 1}
            )
        )["data"]
        reference = first["evidence_ref"]

        # Paging and the owned reference are separate mechanisms, never combined.
        both = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "cursor": first["next_cursor"],
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1")],
                },
            )
        )
        assert both["error"]["code"] == "invalid_reference"

        # A key outside the frozen selection is rejected with the scope that was pinned.
        outside = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("300", "1")],
                },
            )
        )
        assert outside["error"]["code"] == "message_not_in_snapshot"
        assert outside["error"]["details"]["reference_chats"] == ["100", "200"]

        # Repeated keys collapse and the caller's order is preserved.
        picked = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": reference,
                    "message_keys": [key("200", "2"), key("100", "1"), key("200", "2")],
                },
            )
        )["data"]
        assert [(row["chat_id"], row["id"]) for row in picked["items"]] == [
            ("200", "2"),
            ("100", "1"),
        ]

        # A job that stores no frozen evidence selection never issues a reference.
        started = data(
            await server.call_tool(
                "activity_start",
                {"profile_id": "personal", "chat_ids": ["100", "200"], "top": 1},
            )
        )["data"]
        await complete(server, "personal", started["job_id"])
        page = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": started["job_id"], "limit": 1}
            )
        )["data"]
        assert page["evidence_ref"] is None
        refused = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": started["job_id"],
                    "evidence_ref": reference,
                    "message_keys": [key("100", "1")],
                },
            )
        )
        assert refused["error"]["code"] == "unsupported_job_results"


@pytest.mark.asyncio
async def test_reference_metadata_source_revision_and_projection_stay_consistent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    CapturingAPI.instances = []
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, CapturingAPI) as app, client(app, settings) as server:
        job = await frozen_job(server)

        # The reference and exact keys are discoverable arguments of the same tool.
        tools = {tool.name: tool for tool in (await server.list_tools()).tools}
        properties = tools["jobs_results"].inputSchema["properties"]
        assert {"evidence_ref", "message_keys"} <= set(properties)

        first = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 1}
            )
        )["data"]

        # Two independent views of one finished job report the same source revision,
        # while each keeps its own opaque reference.
        again = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": job, "limit": 1}
            )
        )["data"]
        assert again["evidence_ref"] != first["evidence_ref"]
        assert again["source_version"] == first["source_version"]

        view = data(
            await server.call_tool(
                "jobs_results",
                {"profile_id": "personal", "job_id": job, "evidence_ref": first["evidence_ref"]},
            )
        )["data"]
        assert view["items"] == []
        assert view["source_version"] == first["source_version"]
        assert view["reference"] == {
            "job_id": job,
            "chats": ["100", "200"],
            "generation": settings.profiles["personal"].generation,
            "status": "completed",
            "snapshot_at": view["result_snapshot_at"],
            "expires_at": view["reference_expires_at"],
        }

        # Exact originals carry the same pinned scope and version as the reference view.
        originals = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": first["evidence_ref"],
                    "message_keys": [key("100", "1")],
                },
            )
        )["data"]
        assert originals["reference"] == view["reference"]

        # The reference travels through the existing field-projection seam unchanged.
        projected = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": job,
                    "evidence_ref": first["evidence_ref"],
                    "message_keys": [key("100", "1")],
                    "fields": ["text"],
                },
            )
        )["data"]
        assert projected["evidence_ref"] == first["evidence_ref"]
        assert projected["source_version"] == first["source_version"]
        assert projected["items"][0]["text"] == "Old original"


class UnreadSDK(SDK):
    """Return the frozen unread page from the fake SDK's own message rows."""

    async def __call__(self, request, *args, **kwargs):
        assert isinstance(request, functions.messages.GetHistoryRequest)
        self.calls.append(request)
        rows = [row for row in self.rows if not request.offset_id or row.id < request.offset_id]
        return types.messages.MessagesSlice(
            count=len(self.rows), messages=rows[: request.limit], chats=[], users=[], topics=[]
        )


@pytest.mark.asyncio
async def test_unread_export_reference_serves_the_same_frozen_originals(tmp_path, monkeypatch):
    monkeypatch.setattr("teleloom.jobs.utcnow", lambda: NOW)
    sdk = UnreadSDK()
    sdk.rows = [
        types.Message(
            id=id_,
            peer_id=types.PeerChannel(100),
            date=NOW - timedelta(minutes=1),
            message=f"Unread {id_}",
        )
        for id_ in (2, 1)
    ]
    sdk.dialogs = [
        SimpleNamespace(
            id=int(CHAT),
            name="Selected",
            entity=types.Channel(
                id=100, title="Selected", photo=types.ChatPhotoEmpty(), date=NOW, broadcast=True
            ),
            is_group=False,
            is_channel=True,
            unread_count=2,
            dialog=SimpleNamespace(read_inbox_max_id=0, top_message=2, notify_settings=None),
        )
    ]
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", read_mode="selected", read_chats=[CHAT])},
    )
    async with running(settings, factory(sdk)) as app, client(app, settings) as server:
        started = data(
            await server.call_tool(
                "unread_export_start",
                {"profile_id": "personal", "chat_ids": [CHAT], "max_messages": 10},
            )
        )["data"]
        await complete(server, "personal", started["job_id"])
        first = data(
            await server.call_tool(
                "jobs_results", {"profile_id": "personal", "job_id": started["job_id"], "limit": 1}
            )
        )["data"]
        reference = first["evidence_ref"]
        assert isinstance(reference, str) and reference
        history_calls = len(sdk.calls)

        original = data(
            await server.call_tool(
                "jobs_results",
                {
                    "profile_id": "personal",
                    "job_id": started["job_id"],
                    "evidence_ref": reference,
                    "message_keys": [key(CHAT, "2")],
                },
            )
        )["data"]
        assert [(row["chat_id"], row["id"]) for row in original["items"]] == [(CHAT, "2")]
        assert original["items"][0]["text"] == "Unread 2"
        assert original["source_version"] == first["source_version"]
        assert original["reference"]["chats"] == [CHAT]
        assert len(sdk.calls) == history_calls  # the frozen export reference rereads nothing
