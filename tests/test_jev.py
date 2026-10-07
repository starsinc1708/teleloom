import asyncio

import pytest

from teleloom.config import Profile, Settings
from teleloom.server import create_server
from tests.fakes import TelegramAPI, data
from tests.test_transport import client, running


@pytest.mark.asyncio
async def test_jev_disabled_keeps_original_evidence_without_cloud_call(tmp_path):
    server = create_server(
        Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")}),
        adapter_factory=TelegramAPI,
    )
    result = data(
        await server.call_tool(
            "messages_classify",
            {"profile_id": "personal", "chat_id": "100", "message_ids": ["5", "4"]},
        )
    )
    assert result["ok"]
    assert result["data"]["analysis_status"] == "disabled"
    assert [message["id"] for message in result["data"]["evidence"]["items"]] == ["5", "4"]


@pytest.mark.asyncio
async def test_opted_in_jev_batches_typed_questions_caches_and_bounds_cost(tmp_path, monkeypatch):
    import typesafe_sdk

    requests = []

    class TypeSafeAPI:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def system_one(self, *, state, questions, model):
            requests.append((state, questions))
            assert {type(q).__name__ for q in questions.values()} == {"Score", "Choice", "Noul"}
            return typesafe_sdk.SystemOneResponse(
                model=model,
                usage={"input_tokens": 100, "output_tokens": 20},
                answers={"m5_actionable": {"type": "noul", "noul": 0.8}},
            )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", TypeSafeAPI)
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", jev_chats=["100"])}
    )
    settings.limits.jev_daily_calls = 1
    args = {"profile_id": "personal", "chat_id": "100", "message_ids": ["5", "4"]}
    async with running(settings) as app, client(app, settings) as session:
        first = data(await session.call_tool("messages_classify", args))["data"]
        assert first["analysis_status"] == "evaluated"
        assert first["judgments"]["usage"]["input_tokens"] == 100
        cached = data(await session.call_tool("messages_classify", args))["data"]
        assert cached["analysis_status"] == "cached"
        exceeded = data(await session.call_tool("messages_classify", {**args, "query": "new"}))[
            "data"
        ]
        assert exceeded["analysis_status"] == "budget_exceeded"
        assert exceeded["evidence"]["items"] == first["evidence"]["items"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_unavailable_jev_keeps_evidence_without_exposing_credentials(tmp_path, monkeypatch):
    import typesafe_sdk

    def unavailable(**kwargs):
        raise ConnectionError("credential-must-not-leak")

    monkeypatch.setenv("TYPESAFE_API_KEY", "credential-must-not-leak")
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", unavailable)
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", jev_chats=["100"])}
    )
    async with running(settings) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                "messages_classify",
                {"profile_id": "personal", "chat_id": "100", "message_ids": ["5"]},
            )
        )
        assert result["data"]["analysis_status"] == "unavailable"
        assert result["data"]["evidence"]["items"][0]["id"] == "5"
        assert "credential-must-not-leak" not in str(result)


@pytest.mark.asyncio
async def test_slow_jev_retains_evidence_within_read_deadline(tmp_path, monkeypatch):
    import typesafe_sdk

    class StalledTypeSafeAPI:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def system_one(self, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setenv("TYPESAFE_API_KEY", "isolated-test-key")
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", StalledTypeSafeAPI)
    settings = Settings(
        data_dir=tmp_path,
        profiles={"personal": Profile(kind="user", jev_chats=["100"])},
        read_timeout_seconds=0.05,
    )
    async with running(settings) as app, client(app, settings) as session:
        result = data(
            await asyncio.wait_for(
                session.call_tool(
                    "messages_classify",
                    {"profile_id": "personal", "chat_id": "100", "message_ids": ["5"]},
                ),
                timeout=1,
            )
        )
        assert result["ok"]
        assert result["data"]["analysis_status"] == "unavailable"
        assert [m["id"] for m in result["data"]["evidence"]["items"]] == ["5"]
