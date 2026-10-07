import json

from typer.testing import CliRunner

from teleloom.cli import app


def test_evaluator_replays_original_low_text_judgment_through_mcp():
    result = CliRunner().invoke(app, ["evaluate"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    regression = next(case for case in report["cases"] if case["id"] == "ru-summary-regression")
    assert report["mode"] == "replay"
    assert report["ok"]
    assert regression["raw_scores"]["text"] == 0.29
    assert {"text", "entities"} <= set(regression["final_fields"])
    assert "text" in regression["safeguards"]
    assert regression["violations"] == []
    assert regression["model"] == "jev-1.13.0"
    assert regression["model_source"] == "historical docs/acceptance.md"
    assert regression["usage"] is None
    assert regression["latency_ms"] >= 0
    assert regression["schema_id"]
    assert regression["policy_version"] == "response-fields-v4"
    assert report["schemas"]["response_fields_select"] > 0
    assert report["schemas"]["messages_get"] > 0
    assert regression["round_trips"] == 1
    assert regression["schema_bytes"] == (
        report["schemas"]["response_fields_select"] + report["schemas"]["messages_get"]
    )
    assert regression["selection_overhead_bytes"] > 0
    assert regression["structured_content_bytes"] > 0
    assert regression["text_json_bytes"] > 0


def test_static_bilingual_corpus_covers_intents_explicit_precedence_and_fallback():
    result = CliRunner().invoke(app, ["evaluate"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    cases = {case["id"]: case for case in report["cases"]}
    assert {case["language"] for case in cases.values()} == {"ru", "en"}
    assert {
        "en-summary-links",
        "en-history",
        "ru-history",
        "en-only-counts",
        "ru-only-counts",
        "en-mixed-negation",
        "ru-mixed-negation",
        "en-explicit-fields",
        "ru-explicit-minimal",
        "en-fallback-invalid",
    } <= cases.keys()
    assert cases["en-summary-links"]["mode"] == "fake"
    for name in ("en-only-counts", "ru-only-counts"):
        assert "text" not in cases[name]["final_fields"]
    assert cases["en-explicit-fields"]["external_calls"] == 0
    assert cases["en-explicit-fields"]["model"] is None
    assert cases["en-explicit-fields"]["model_source"] == "not_called_explicit_selection"
    assert cases["ru-explicit-minimal"]["external_calls"] == 0
    assert cases["en-fallback-invalid"]["fallback"]
    assert cases["en-fallback-invalid"]["status"] == "unavailable"
    assert all(case["violations"] == [] for case in cases.values())


def test_benchmark_accounts_for_selection_and_read_and_exposes_break_even_and_integrity():
    result = CliRunner().invoke(app, ["benchmark"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["ok"]
    assert report["mode"] == "replay"
    assert report["sizes"] == [1, 20, 100]
    assert len(report["rows"]) == 12
    rows = {(row["messages"], row["strategy"]): row for row in report["rows"]}
    for size in (1, 20, 100):
        full = rows[size, "full"]
        uncached = rows[size, "jev_uncached"]
        cached = rows[size, "jev_cached"]
        preset = rows[size, "preset"]
        assert len(full["mcp_calls"]) == 1
        assert len(uncached["mcp_calls"]) == 2
        assert uncached["external_calls"]["jev"] == 1
        assert cached["external_calls"]["jev"] == 0
        assert cached["cache_warmup"]["external_calls"] == 1
        assert cached["cold_start_total_json_bytes"] > cached["total_json_bytes"]
        assert cached["selection_status"] == "cached"
        assert all(uncached["integrity"].values())
        assert all(cached["integrity"].values())
        assert preset["integrity"]["entities"] is False
        assert preset["expected_integrity"]["entities"] is False
        assert preset["limitation"]
        for row in (full, uncached, cached, preset):
            assert row["violations"] == []
            assert row["usage"] is None
            assert row["total_json_bytes"] == row["mcp_json_bytes"] + row["external_json_bytes"]
            assert all(call["representations_agree"] for call in row["mcp_calls"])
            assert all(
                call["structured_content_bytes"] > 0 and call["text_json_bytes"] > 0
                for call in row["mcp_calls"]
            )
    assert rows[1, "jev_uncached"]["savings_bytes"] < 0
    assert "not tokens" in report["limitations"]


def test_benchmark_separates_schema_overhead_round_trips_and_proves_select_once_and_compact():
    result = CliRunner().invoke(app, ["benchmark"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["ok"]
    schemas = report["schemas"]
    assert schemas["combined_bytes"] == (
        schemas["response_fields_select"] + schemas["messages_get"]
    )
    rows = {(row["messages"], row["strategy"]): row for row in report["rows"]}
    full = rows[20, "full"]
    uncached = rows[20, "jev_uncached"]
    assert full["round_trips"] == 1
    assert full["selection_overhead_bytes"] == 0
    assert full["schema_bytes"] == schemas["messages_get"]
    assert uncached["round_trips"] == 2
    assert uncached["selection_overhead_bytes"] > 0
    assert uncached["schema_bytes"] == schemas["combined_bytes"]
    assert full["structured_content_bytes"] > 0
    assert full["text_json_bytes"] > 0

    once = report["select_once"]
    assert once["violations"] == []
    assert once["selector_calls"] == 1
    assert once["external_calls"] == 0
    assert once["pages_read"] == 10
    assert once["returned_records"] == 100
    assert once["coverage_baseline_calls"] == 10
    assert once["round_trips"] == 11
    assert once["selection_overhead_bytes"] > 0

    compact = report["compact_selection"]
    assert compact["violations"] == []
    assert compact["same_effective_fields"]
    assert compact["full"]["status"] == compact["compact"]["status"] == "disabled"
    assert compact["compact"]["total_bytes"] * 2 <= compact["full"]["total_bytes"]
    assert (
        compact["compact"]["structured_content_bytes"] < compact["full"]["structured_content_bytes"]
    )
    assert compact["compact"]["text_json_bytes"] < compact["full"]["text_json_bytes"]
    assert compact["reduction_percent"] >= 50
    assert "not tokens" in report["limitations"]


def test_evaluation_limits_stop_before_another_external_request_and_preserve_owner_state(
    monkeypatch, tmp_path
):
    from teleloom.secrets import Secrets

    owner = tmp_path / "owner"
    owner.mkdir()
    marker = owner / "config.json"
    marker.write_text('{"private":"do not read or change"}', encoding="utf-8")
    monkeypatch.setenv("TELELOOM_DATA_DIR", str(owner))
    monkeypatch.setenv("TYPESAFE_API_KEY", "owner-key-must-not-leak")
    monkeypatch.setattr(
        Secrets,
        "get",
        lambda *args: (_ for _ in ()).throw(AssertionError("Credential vault access")),
    )
    result = CliRunner().invoke(app, ["evaluate", "--max-requests", "1"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["external_calls"] == 1
    assert report["stopped"] == "request_budget_exceeded"
    assert "owner-key-must-not-leak" not in result.output
    assert marker.read_text(encoding="utf-8") == '{"private":"do not read or change"}'
    assert list(owner.iterdir()) == [marker]


def test_character_limits_fail_truthfully_without_external_call():
    result = CliRunner().invoke(app, ["evaluate", "--max-characters", "1"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["external_calls"] == 0
    assert report["stopped"] == "character_budget_exceeded"


def test_explicit_live_path_is_bounded_with_a_fake_external_sdk_and_reports_available_usage(
    monkeypatch,
):
    import sys
    from types import ModuleType, SimpleNamespace

    calls = []

    class Question:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def model_dump(self, **kwargs):
            return {
                name: value.model_dump() if isinstance(value, Question) else value
                for name, value in self.kwargs.items()
            }

    class SDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def system_one(self, *, state, questions, model):
            calls.append(state)
            payload = {
                "model": "external-sdk-test-model",
                "usage": {"input_tokens": 9, "output_tokens": None},
                "answers": {name: {"type": "noul", "noul": 0.1} for name in questions},
            }
            return SimpleNamespace(model_dump=lambda **kwargs: payload)

    sdk = ModuleType("typesafe_sdk")
    sdk.AsyncTypeSafeClient = SDK
    sdk.Noul = sdk.NoulCriteria = sdk.RetryPolicy = Question
    monkeypatch.setitem(sys.modules, "typesafe_sdk", sdk)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-external-sdk-test-key")
    result = CliRunner().invoke(app, ["evaluate", "--live", "--max-requests", "1"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["mode"] == "live"
    assert report["external_calls"] == 1
    assert len(calls) == 1
    assert report["cases"][0]["usage"] == {"input_tokens": 9, "output_tokens": None}
    assert report["cases"][0]["model"] == "external-sdk-test-model"
    assert "fake-external-sdk-test-key" not in result.output


def test_live_requires_existing_env_key_without_credential_vault_access(monkeypatch):
    from teleloom.secrets import Secrets

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        Secrets,
        "get",
        lambda *args: (_ for _ in ()).throw(AssertionError("Credential vault access")),
    )
    result = CliRunner().invoke(app, ["evaluate", "--live"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "credentials_missing"


def test_benchmark_request_budget_stops_without_misreporting_uncached_results():
    result = CliRunner().invoke(app, ["benchmark", "--max-requests", "1"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["stopped"] == "request_budget_exceeded"
    assert report["external_calls"]["jev"] == 1
    assert len(report["rows"]) < 12


def test_overall_deadline_cleans_up_a_stalled_fake_external_sdk(monkeypatch):
    import asyncio
    import sys
    from types import ModuleType

    class Question:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def model_dump(self, **kwargs):
            return {
                name: value.model_dump() if isinstance(value, Question) else value
                for name, value in self.kwargs.items()
            }

    exited = []

    class SDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            exited.append(True)

        async def system_one(self, **kwargs):
            await asyncio.Event().wait()

    sdk = ModuleType("typesafe_sdk")
    sdk.AsyncTypeSafeClient = SDK
    sdk.Noul = sdk.NoulCriteria = sdk.RetryPolicy = Question
    monkeypatch.setitem(sys.modules, "typesafe_sdk", sdk)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-stalled-sdk-key")
    result = CliRunner().invoke(app, ["evaluate", "--live", "--timeout-seconds", "2"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["stopped"] == "time_budget_exceeded"
    assert report["external_calls"] == 1
    assert exited == [True]
    assert sys.modules["typesafe_sdk"] is sdk
