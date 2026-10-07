import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from teleloom.config import Profile, Settings
from tests.fakes import TelegramAPI, data
from tests.test_transport import client, running


@pytest.fixture
def jev(monkeypatch):
    import typesafe_sdk

    fake = SimpleNamespace(calls=[], clients=[], values={}, answer_override=None, stall=False)

    class TypeSafeAPI:
        def __init__(self, **kwargs):
            fake.clients.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def system_one(self, *, state, questions, model):
            fake.calls.append((state, questions, model))
            if fake.stall:
                await asyncio.Event().wait()
            answers = {
                name: {
                    "type": "noul",
                    "noul": fake.values.get(name, 0.9 if name == "text" else 0.1),
                }
                for name in questions
            }
            if fake.answer_override is not None:
                answers = fake.answer_override(answers)
            return SimpleNamespace(model_dump=lambda **kwargs: {"answers": answers})

    monkeypatch.setenv("TYPESAFE_API_KEY", "isolated-selection-test-key")
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", TypeSafeAPI)
    return fake


async def select(session, request="Digest for the last 48 hours", **kwargs):
    result = data(
        await session.call_tool(
            "response_fields_select", {"tool_name": "messages_get", "request": request, **kwargs}
        )
    )
    assert result["ok"], result
    return result["data"]


LIVE_SUMMARY_REQUEST = (
    "Сделай саммари всех сообщений за последние 48 часов. "
    "Выдели полезные заметки и технологии со ссылками. "
    "Реакции, просмотры и число комментариев не нужны."
)


@pytest.mark.asyncio
async def test_recorded_live_jev_omission_cannot_remove_summary_content(tmp_path, jev):
    # Replay the owner's 2026-10-05 live Jev answers, not another paid API call.
    jev.values = {
        "sender_name": 0.18,
        "text": 0.29,
        "author_signature": 0.12,
        "entities": 0.4,
        "media": 0.21,
        "pinned": 0.09,
        "sender_username": 0.41,
        "views": 0.29,
        "edited_at": 0.19,
        "forwarded_from": 0.13,
        "outgoing": 0.19,
        "reactions": 0.06,
        "reply_count": 0.08,
    }
    adapters = []

    class SummaryAPI(TelegramAPI):
        def __init__(self, *args):
            super().__init__(*args)
            adapters.append(self)
            for row in self.rows:
                row.text = "Useful technology notes"
                row.entities = [{"type": "text_url", "url": "https://example.org/notes"}]
                row.link = f"https://t.me/example/{row.id}"
                row.reactions = [{"emoji": "🔥", "count": 100}]
                row.views = 200
                row.reply_count = 50

    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, SummaryAPI) as app, client(app, settings) as session:
        selection_result = await session.call_tool(
            "response_fields_select",
            {"tool_name": "messages_get", "request": LIVE_SUMMARY_REQUEST, "use_jev": True},
        )
        selection = data(selection_result)["data"]
        assert {"text", "entities"} <= set(selection["fields"])
        assert {"reactions", "views", "reply_count"} <= set(selection["omitted"])
        assert selection["status"] == "uncertain"
        assert "safeguard" in selection["reasons"]["text"]
        assert json.loads(selection_result.content[0].text) == selection_result.structuredContent
        assert not adapters
        args = {"profile_id": "personal", "chat_id": "100"}
        original = await session.call_tool("messages_get", args)
        reduced = await session.call_tool("messages_get", {**args, "fields": selection["fields"]})
        assert json.loads(reduced.content[0].text) == reduced.structuredContent
        before, after = data(original)["data"], data(reduced)["data"]
        for source, projected in zip(before["items"], after["items"], strict=True):
            for name in ("text", "entities", *selection["required"]):
                assert projected[name] == source[name]
            assert {"reactions", "views", "reply_count"}.isdisjoint(projected)
        for name in ("source", "coverage", "incomplete", "warnings", "next_cursor"):
            assert before.get(name) == after.get(name)
        assert len(reduced.content[0].text) < len(original.content[0].text)
        cached = await select(session, request=LIVE_SUMMARY_REQUEST, use_jev=True)
        assert cached["status"] == "cached"
        assert cached["fields"] == selection["fields"]
        assert cached["reasons"] == selection["reasons"]
        assert len(jev.calls) == 1
        assert "Useful technology notes" not in json.dumps(jev.calls[0][0])
        assert not adapters[0].sent and not adapters[0].ack


@pytest.mark.asyncio
async def test_schema_only_selection_defaults_to_digest_fields_without_telegram(tmp_path):
    def forbidden_adapter(*args):
        raise AssertionError("Field selection must never create a Telegram adapter")

    settings = Settings(data_dir=tmp_path)
    async with running(settings, forbidden_adapter) as app, client(app, settings) as session:
        result = await session.call_tool(
            "response_fields_select",
            {"tool_name": "messages_get", "request": "Сделай дайджест за последние 48 часов"},
        )
        selection = data(result)["data"]
        assert data(result)["ok"]
        assert selection["status"] == "disabled"
        assert "text" in selection["fields"]
        assert "reactions" not in selection["fields"]
        assert {"profile_id", "chat_id", "id", "date", "link"} <= set(selection["fields"])
        assert "reactions" in selection["omitted"]
        assert "views" in selection["omitted"]
        assert "reply_count" in selection["omitted"]
        assert selection["schema_id"]
        assert json.loads(result.content[0].text) == result.structuredContent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_request",
    [
        LIVE_SUMMARY_REQUEST,
        "Нужна суммаризация сообщений со ссылками, реакции, просмотры и комментарии не нужны",
        "Summarize messages with links, without reactions, views or comments",
    ],
)
async def test_summary_fallback_recognizes_intent_and_excluded_engagement(tmp_path, owner_request):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        selection = await select(session, request=owner_request)
        assert selection["status"] == "disabled"
        assert {"text", "entities", "author_signature", "edited_at"} <= set(selection["fields"])
        assert {"reactions", "views", "reply_count"} <= set(selection["omitted"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,owner_request",
    [
        ("messages_get", "Read messages with links"),
        ("messages_search", "Найди сообщения со ссылками"),
        ("digest_context", "Суммаризация за 48 часов со ссылками"),
        ("inbox_get", "Прочитай сообщения со ссылками"),
        ("jobs_results", "Review extracted content with links"),
        ("thread_get", "Покажи сообщения со ссылками"),
        ("messages_get", "Покажи историю за 48 часов со ссылками"),
        ("messages_get", "Сделай краткое резюме сообщений со ссылками"),
        ("messages_get", "Обобщи сообщения за последние 48 часов со ссылками"),
        ("messages_get", "Fetch messages with links"),
        ("messages_get", "Telegram evidence needed for the task, with links"),
        ("messages_get", "Only metadata is not enough; fetch messages with links"),
        ("messages_get", "Только метаданные не нужны; сделай резюме сообщений со ссылками"),
    ],
)
async def test_reading_content_survives_confident_jev_omission(
    tmp_path, jev, tool_name, owner_request
):
    jev.values = {"text": 0.1}
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                "response_fields_select",
                {"tool_name": tool_name, "request": owner_request, "use_jev": True},
            )
        )["data"]
        assert {"text", "entities"} <= set(result["fields"])
        assert result["status"] == "uncertain"
        assert "safeguard" in result["reasons"]["text"]
        assert "safeguard" in result["reasons"]["entities"]


@pytest.mark.asyncio
async def test_metadata_only_intent_can_omit_text_and_explicit_summary_fields_win(tmp_path, jev):
    jev.values = {"text": 0.1}
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        metadata = await select(session, request="Only reaction counts", use_jev=True)
        assert "text" in metadata["omitted"]
        assert metadata["status"] == "evaluated"
        for choice in ({"fields": []}, {"fields": ["views"]}, {"preset": "minimal"}):
            explicit = await select(session, request=LIVE_SUMMARY_REQUEST, use_jev=True, **choice)
            assert explicit["status"] == "explicit"
            assert {"text", "entities"} <= set(explicit["omitted"])
        assert len(jev.calls) == 1


@pytest.mark.asyncio
async def test_jev_questions_name_each_static_field_and_reference_its_definition(tmp_path, jev):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        await select(session, request=LIVE_SUMMARY_REQUEST, use_jev=True)
    state, questions, _ = jev.calls[0]
    for index, field in enumerate(state["available_fields"]):
        question = questions[field["name"]]
        assert f"`{field['name']}`" in question.instructions
        assert f"`available_fields[{index}].description`" in question.instructions
        assert field["description"] in question.instructions
        assert "`request`" in question.instructions
        criteria = question.model_dump()["criteria"]
        assert criteria["true"] and criteria["false"]


@pytest.mark.parametrize(
    "owner_request",
    [
        "Make a digest for 48 hours without reactions",
        "Make a digest for 48 hours, no reaction counts needed",
        "Сделай дайджест за 48 часов без реакций",
        "Дайджест за 48 часов; реакции не нужны",
        "Review messages and make a digest without reactions",
    ],
)
@pytest.mark.asyncio
async def test_deterministic_digest_fallback_honors_explicit_reaction_exclusion(
    tmp_path, owner_request
):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        selection = await select(session, request=owner_request)
        assert selection["status"] == "disabled"
        assert "text" in selection["fields"]
        assert "reactions" in selection["omitted"]
        assert "views" in selection["omitted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_request", ["Review posts and compare views", "Сравни просмотры публикаций"]
)
async def test_deterministic_selection_retains_requested_view_counts(tmp_path, owner_request):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        selection = await select(session, request=owner_request)
        assert "views" in selection["fields"]


@pytest.mark.asyncio
async def test_explicit_fields_and_presets_take_precedence_without_jev(tmp_path, jev):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        explicit = await select(session, fields=["reactions"], use_jev=True)
        assert explicit["status"] == "explicit"
        assert "reactions" in explicit["fields"]
        assert "text" in explicit["omitted"]
        assert explicit["reasons"]["text"] == "Omitted by explicit client selection."
        assert set(explicit["required"]) <= set(explicit["fields"])
        full = await select(session, preset="full", use_jev=True)
        assert full["status"] == "explicit"
        assert full["omitted"] == []
        engagement = await select(session, preset="engagement", use_jev=True)
        assert {"reactions", "views", "reply_count"} <= set(engagement["fields"])
    assert not jev.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"fields": ["nonexistent"]}, "invalid_projection"),
        ({"fields": ["text"], "preset": "digest"}, "invalid_projection"),
        ({"fields": ["text"] * 65}, "invalid_projection"),
        ({"preset": "unknown"}, "invalid_projection"),
        ({"request": "x" * 2001}, "invalid_request"),
        ({"tool_name": "send_preview"}, "unsupported_projection"),
    ],
)
async def test_invalid_selection_fails_before_jev_or_telegram(tmp_path, jev, arguments, code):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        result = data(
            await session.call_tool(
                "response_fields_select",
                {"tool_name": "messages_get", "request": "digest", "use_jev": True, **arguments},
            )
        )
        assert not result["ok"]
        assert result["error"]["code"] == code
    assert not jev.calls


@pytest.mark.asyncio
async def test_jev_receives_only_task_and_static_schema_in_one_noul_batch(tmp_path, jev):
    def forbidden_adapter(*args):
        raise AssertionError("Field selection must never fetch a message or create an adapter")

    jev.values = {"text": 0.9}
    settings = Settings(data_dir=tmp_path, profiles={"personal": Profile(kind="user")})
    async with running(settings, forbidden_adapter) as app, client(app, settings) as session:
        result = await select(session, use_jev=True)
        assert result["status"] == "evaluated"
        assert "text" in result["fields"]
        assert "reactions" in result["omitted"]
        assert len(jev.calls) == 1
        state, questions, model = jev.calls[0]
        assert set(state) == {"request", "tool_name", "available_fields"}
        assert state["request"] == "Digest for the last 48 hours"
        assert 1 <= len(questions) <= 64
        assert {type(question).__name__ for question in questions.values()} == {"Noul"}
        assert set(questions) == {field["name"] for field in state["available_fields"]}
        assert all(
            set(field) == {"name", "description", "required"} for field in state["available_fields"]
        )
        assert all(not field["required"] for field in state["available_fields"])
        assert "personal" not in json.dumps(state)
        assert "isolated-selection-test-key" not in str(result)
        assert model == settings.jev_model
        assert jev.clients[0]["retry"].max_retries == 0
        assert jev.clients[0]["timeout"] == 5


@pytest.mark.asyncio
async def test_uncertain_jev_judgments_keep_deterministic_choices(tmp_path, jev):
    jev.values = {"text": 0.5, "reactions": 0.5}
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        result = await select(session, use_jev=True)
        assert result["status"] == "uncertain"
        assert "text" in result["fields"]
        assert "reactions" in result["omitted"]
        assert "Uncertain" in result["reasons"]["text"]
        assert "Uncertain" in result["reasons"]["reactions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [-0.01, 1.01, float("inf"), float("nan"), True, "0.9"])
async def test_invalid_probabilities_fall_back_without_caching(tmp_path, jev, value):
    jev.values = {"text": value}
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        expected = await select(session)
        result = await select(session, use_jev=True)
        assert result["status"] == "unavailable"
        assert result["fields"] == expected["fields"]
        assert (await select(session, use_jev=True))["status"] == "unavailable"
    assert len(jev.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        lambda answers: {},
        lambda answers: {**answers, "made_up": {"type": "noul", "noul": 0.9}},
        lambda answers: {**answers, "text": {"type": "choice", "noul": 0.9}},
    ],
)
async def test_incomplete_or_wrong_kind_judgments_fall_back(tmp_path, jev, override):
    jev.answer_override = override
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        result = await select(session, use_jev=True)
        assert result["status"] == "unavailable"
        assert "text" in result["fields"]
        assert (await select(session, use_jev=True))["status"] == "unavailable"
    assert len(jev.calls) == 2


@pytest.mark.asyncio
async def test_cache_is_schema_intent_model_bound_expires_and_has_no_task(
    tmp_path, jev, monkeypatch
):
    import teleloom.field_selection as selection_module

    moment = [datetime(2026, 10, 5, tzinfo=UTC)]
    monkeypatch.setattr(selection_module, "utcnow", lambda: moment[0])
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        first = await select(session, request="Private task: digest last 48 hours", use_jev=True)
        cached = await select(
            session, request="  PRIVATE  TASK: Digest LAST 48 hours  ", use_jev=True
        )
        assert first["status"] == "evaluated"
        assert cached["status"] == "cached"
        assert cached["fields"] == first["fields"]
        settings.jev_model = "jev-other-model"
        assert (await select(session, request="Private task: digest last 48 hours", use_jev=True))[
            "status"
        ] == "evaluated"
        different = data(
            await session.call_tool(
                "response_fields_select",
                {
                    "tool_name": "chats_list",
                    "request": "Private task: digest last 48 hours",
                    "use_jev": True,
                },
            )
        )["data"]
        assert different["status"] == "evaluated"
        assert different["schema_id"] != first["schema_id"]
        moment[0] += timedelta(days=1)
        assert (await select(session, request="Private task: digest last 48 hours", use_jev=True))[
            "status"
        ] == "evaluated"
    # Check physical at-rest disclosure, without depending on SQLite's private
    # tables, cache keys or serialized implementation shape.
    stored = (settings.data_dir / "workspace.sqlite").read_bytes()
    assert b"Private task" not in stored
    assert b"private task" not in stored
    assert b"isolated-selection-test-key" not in stored
    assert len(jev.calls) == 4


@pytest.mark.asyncio
async def test_daily_budget_is_shared_with_existing_analysis_and_cache_needs_no_call(tmp_path, jev):
    settings = Settings(
        data_dir=tmp_path, profiles={"personal": Profile(kind="user", jev_chats=["100"])}
    )
    settings.limits.jev_daily_calls = 1
    async with running(settings) as app, client(app, settings) as session:
        first = await select(session, use_jev=True)
        assert first["status"] == "evaluated"
        assert (await select(session, use_jev=True))["status"] == "cached"
        assert (await select(session, request="another task", use_jev=True))[
            "status"
        ] == "budget_exceeded"
        classified = data(
            await session.call_tool(
                "messages_classify",
                {"profile_id": "personal", "chat_id": "100", "message_ids": ["5"]},
            )
        )["data"]
        assert classified["analysis_status"] == "budget_exceeded"
        assert classified["evidence"]["items"][0]["id"] == "5"
    assert len(jev.calls) == 1


@pytest.mark.asyncio
async def test_schema_and_question_character_budget_prevents_external_call(tmp_path, jev):
    settings = Settings(data_dir=tmp_path)
    settings.limits.jev_max_characters = 1000
    async with running(settings) as app, client(app, settings) as session:
        result = await select(session, use_jev=True)
        assert result["status"] == "budget_exceeded"
        assert "text" in result["fields"]
    assert not jev.calls


@pytest.mark.asyncio
async def test_selection_timeout_retains_fallback_without_outer_read_error(tmp_path, jev):
    jev.stall = True
    settings = Settings(data_dir=tmp_path, read_timeout_seconds=0.05)
    async with running(settings) as app, client(app, settings) as session:
        result = await asyncio.wait_for(select(session, use_jev=True), timeout=1)
        assert result["status"] == "unavailable"
        assert "deadline" in result["explanation"]
        assert "text" in result["fields"]
    assert jev.clients[0]["timeout"] == 0.05


@pytest.mark.asyncio
async def test_missing_credentials_and_service_failure_never_expose_secrets(
    tmp_path, jev, monkeypatch
):
    from teleloom.secrets import Secrets

    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(Secrets, "get", lambda *args: None)
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        assert (await select(session, use_jev=True))["status"] == "unavailable"
        assert not jev.calls
        monkeypatch.setenv("TYPESAFE_API_KEY", "isolated-selection-test-key")

        def fail(answers):
            raise ConnectionError("isolated-selection-test-key")

        jev.answer_override = fail
        result = await select(session, use_jev=True)
        assert result["status"] == "unavailable"
        assert "isolated-selection-test-key" not in str(result)


@pytest.mark.asyncio
async def test_cached_selection_survives_owner_restart_and_selection_is_tool_bound(tmp_path, jev):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        first = await select(session, use_jev=True)
    async with running(settings) as app, client(app, settings) as session:
        cached = await select(session, use_jev=True)
        assert cached["status"] == "cached"
        assert cached["fields"] == first["fields"]
        assert cached["tool_name"] == "messages_get"
        # Reuse is limited to the same tool/schema/intent: a different tool needs
        # its own selection even when it shares the record catalog shape.
        other = data(
            await session.call_tool(
                "response_fields_select",
                {
                    "tool_name": "messages_search",
                    "request": "Digest for the last 48 hours",
                    "use_jev": True,
                },
            )
        )["data"]
        assert other["status"] == "evaluated"
        assert other["tool_name"] == "messages_search"
    assert len(jev.calls) == 2


@pytest.mark.asyncio
async def test_selection_flows_into_real_digest_projection_without_changing_policy(tmp_path, jev):
    created = []
    start = datetime(2026, 10, 3, tzinfo=UTC)
    end = datetime(2026, 10, 5, tzinfo=UTC)

    class DigestAPI(TelegramAPI):
        def __init__(self, *args):
            super().__init__(*args)
            created.append(self)
            for index, row in enumerate(self.rows):
                row.date = start + timedelta(hours=index + 1)
                row.link = f"https://t.me/example/{row.id}"
                row.reactions = [{"emoji": "🔥", "count": 150}]
                row.text = f"Private Telegram source content {row.id}"

        async def start(self):
            self.store.save_messages(self.rows)

    jev.values = {"text": 0.9}
    profile = Profile(kind="user")
    settings = Settings(data_dir=tmp_path, profiles={"personal": profile})
    async with running(settings, DigestAPI) as app, client(app, settings) as session:
        selection = data(
            await session.call_tool(
                "response_fields_select",
                {
                    "tool_name": "digest_context",
                    "request": "Digest for the last 48 hours",
                    "use_jev": True,
                },
            )
        )["data"]
        assert selection["status"] == "evaluated"
        assert not created
        args = {
            "profile_id": "personal",
            "chat_id": "100",
            "since": start.isoformat(),
            "until": end.isoformat(),
            "limit": 2,
            "source": "index",
        }
        # Establish the fake's observed originals through a public live read.
        await session.call_tool("messages_get", {"profile_id": "personal", "chat_id": "100"})
        original = await session.call_tool("digest_context", args)
        projected = await session.call_tool(
            "digest_context", {**args, "fields": selection["fields"]}
        )
        full = data(original)["data"]
        reduced = data(projected)["data"]
        assert data(original)["ok"]
        assert data(projected)["ok"]
        assert len(projected.content[0].text) < len(original.content[0].text)
        assert len(json.dumps(projected.structuredContent)) < len(
            json.dumps(original.structuredContent)
        )
        assert json.loads(projected.content[0].text) == projected.structuredContent
        assert "reactions" in full["items"][0]
        assert "reactions" not in reduced["items"][0]
        assert reduced["incomplete"] is True
        assert reduced["source"] == "local_index"
        assert reduced["warnings"]
        for key in ("coverage", "source", "warnings", "incomplete", "next_cursor"):
            assert reduced[key] == full[key]
        for before, after in zip(full["items"], reduced["items"], strict=True):
            for key in selection["required"]:
                assert after[key] == before[key]
        second = data(
            await session.call_tool(
                "digest_context",
                {**args, "cursor": reduced["next_cursor"], "fields": selection["fields"]},
            )
        )["data"]
        assert [row["id"] for row in second["items"]] == ["3", "2"]
        classified = data(
            await session.call_tool(
                "messages_classify",
                {"profile_id": "personal", "chat_id": "100", "message_ids": ["5"]},
            )
        )["data"]
        assert classified["analysis_status"] == "disabled"
        denied = data(
            await session.call_tool(
                "delivery_preview",
                {"profile_id": "personal", "recipients": ["100"], "text": "test"},
            )
        )
        assert not denied["ok"]
        assert profile.jev_chats == profile.send_chats == profile.broadcast_chats == []
        assert len(jev.calls) == 1
        assert "Private Telegram source content" not in json.dumps(jev.calls[0][0])
        assert not created[0].sent
        assert not created[0].ack
        restored = data(await session.call_tool("digest_context", args))["data"]
        assert restored["items"][0]["reactions"] == full["items"][0]["reactions"]


@pytest.mark.asyncio
async def test_optional_sdk_missing_uses_fallback_without_external_call(tmp_path, jev, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        result = await select(session, use_jev=True)
        assert result["status"] == "unavailable"
        assert "Install teleloom[jev]" in result["explanation"]
        assert "text" in result["fields"]
    assert not jev.calls


COMPACT_TASK = "Digest for the last 48 hours"


def _payload_bytes(result):
    return len(
        json.dumps(result.model_dump(mode="json", exclude_none=True), ensure_ascii=False).encode(
            "utf-8"
        )
    )


async def compact(session, request=COMPACT_TASK, **kwargs):
    result = data(
        await session.call_tool(
            "response_fields_select",
            {"tool_name": "messages_get", "request": request, "detail": "compact", **kwargs},
        )
    )
    assert result["ok"], result
    return result["data"]


@pytest.mark.asyncio
async def test_compact_detail_keeps_decision_identity_and_drops_reason_map(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        full = await select(session, request=COMPACT_TASK)
        small = await compact(session, request=COMPACT_TASK)
        assert full["status"] == small["status"] == "disabled"
        for key in ("tool_name", "schema_id", "fields", "status", "explanation"):
            assert small[key] == full[key]
        assert small["detail"] == "compact"
        assert "reasons" not in small and "omitted" not in small
        assert small["safeguards"] == []
        assert small["fallback_reason"] == full["explanation"]
        assert full["reasons"]


@pytest.mark.asyncio
async def test_compact_detail_is_under_half_the_full_mcp_payload(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        full = await session.call_tool(
            "response_fields_select", {"tool_name": "messages_get", "request": COMPACT_TASK}
        )
        small = await session.call_tool(
            "response_fields_select",
            {"tool_name": "messages_get", "request": COMPACT_TASK, "detail": "compact"},
        )
    assert _payload_bytes(small) * 2 <= _payload_bytes(full)
    assert json.loads(small.content[0].text) == small.structuredContent


@pytest.mark.asyncio
async def test_compact_detail_keeps_safeguards_uncertainty_and_cached_status(tmp_path, jev):
    jev.values = {"text": 0.1}
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        evaluated = await compact(session, request=LIVE_SUMMARY_REQUEST, use_jev=True)
        assert evaluated["status"] == "uncertain"
        assert {"text", "entities"} <= set(evaluated["safeguards"])
        assert evaluated["fallback_reason"] == evaluated["explanation"]
        cached = await compact(session, request=LIVE_SUMMARY_REQUEST, use_jev=True)
        assert cached["status"] == "cached"
        assert cached["fields"] == evaluated["fields"]
        assert cached["safeguards"] == evaluated["safeguards"]
        assert cached["fallback_reason"] is None
        # The cache still stores the full decision; a later full call reads it.
        full_cached = await select(session, request=LIVE_SUMMARY_REQUEST, use_jev=True)
        assert full_cached["status"] == "cached"
        assert full_cached["fields"] == evaluated["fields"]
        assert full_cached["reasons"]
    assert len(jev.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["budget_exceeded", "unavailable"])
async def test_compact_detail_keeps_budget_and_ai_failure_fallbacks(tmp_path, jev, status):
    if status == "unavailable":
        jev.answer_override = lambda answers: (_ for _ in ()).throw(ConnectionError("failure"))
    settings = Settings(data_dir=tmp_path)
    if status == "budget_exceeded":
        settings.limits.jev_max_characters = 1000
    async with running(settings) as app, client(app, settings) as session:
        fallback = await compact(session, request=COMPACT_TASK, use_jev=True)
        assert fallback["status"] == status
        assert fallback["fallback_reason"] == fallback["explanation"]
        assert {"profile_id", "chat_id", "id", "date"} <= set(fallback["fields"])


@pytest.mark.asyncio
async def test_compact_detail_keeps_explicit_fields_authoritative_without_jev(tmp_path, jev):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        explicit = await compact(session, request=LIVE_SUMMARY_REQUEST, fields=["reactions"])
        assert explicit["status"] == "explicit"
        assert "reactions" in explicit["fields"]
        assert "text" not in explicit["fields"]
        assert explicit["fallback_reason"] is None
    assert not jev.calls


@pytest.mark.asyncio
async def test_unknown_detail_is_rejected_by_the_public_schema(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with running(settings) as app, client(app, settings) as session:
        result = await session.call_tool(
            "response_fields_select",
            {"tool_name": "messages_get", "request": COMPACT_TASK, "detail": "verbose"},
        )
    assert result.isError


@pytest.mark.asyncio
async def test_cache_never_exceeds_128_judgments_and_evicts_oldest(tmp_path, jev, monkeypatch):
    import teleloom.field_selection as selection_module

    moment = [datetime(2026, 10, 5, tzinfo=UTC)]
    monkeypatch.setattr(selection_module, "utcnow", lambda: moment[0])
    settings = Settings(data_dir=tmp_path)
    settings.limits.jev_daily_calls = 1000
    async with running(settings) as app, client(app, settings) as session:
        for index in range(130):
            assert (await select(session, request=f"digest private intent {index}", use_jev=True))[
                "status"
            ] == "evaluated"
            moment[0] += timedelta(seconds=1)
        assert (await select(session, request="digest private intent 129", use_jev=True))[
            "status"
        ] == "cached"
        assert (await select(session, request="digest private intent 2", use_jev=True))[
            "status"
        ] == "cached"
        assert (await select(session, request="digest private intent 1", use_jev=True))[
            "status"
        ] == "evaluated"
        assert (await select(session, request="digest private intent 0", use_jev=True))[
            "status"
        ] == "evaluated"
    assert len(jev.calls) == 132
