"""Verify an installed wheel through real stdio/CLI with no Telegram or AI calls."""

import argparse
import asyncio
import contextlib
import json
import os
import socket
import sys
import sysconfig
import tempfile
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path
from time import perf_counter
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

import teleloom
from teleloom.config import Settings
from teleloom.daemon import health, stop_daemon

TIMINGS: list[dict[str, Any]] = []

READERS = {
    "messages_get",
    "messages_search",
    "digest_context",
    "topic_history",
    "thread_get",
    "comments_get",
    "messages_pinned",
    "messages_classify",
    "inbox_get",
    "jobs_results",
    "chats_list",
    "folder_members",
    "chat_resolve",
}
SKILLS = {
    "teleloom-connect",
    "teleloom-read",
    "teleloom-digest",
    "teleloom-inbox",
    "teleloom-send",
    "teleloom-broadcast",
}


def installed_artifact() -> str:
    """Fail before starting anything when a checkout or editable install won import."""
    package = distribution("teleloom")
    direct_url = json.loads(package.read_text("direct_url.json") or "{}")
    imported = Path(teleloom.__file__).resolve()
    installed = Path(package.locate_file("teleloom/__init__.py")).resolve()
    purelib = Path(sysconfig.get_path("purelib")).resolve()
    if (
        direct_url.get("dir_info", {}).get("editable", False)
        or imported != installed
        or not imported.is_relative_to(purelib)
    ):
        raise SystemExit(
            "This smoke requires an installed wheel in an isolated environment; "
            "editable installs and checkout imports are refused. "
            "Run scripts/check_installed_package.py --wheel-dir dist."
        )
    assert package.version == teleloom.__version__, "Distribution/module version mismatch"
    return package.version


def wire(result: CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1 and result.content[0].type == "text"
    text = json.loads(result.content[0].text)
    assert result.structuredContent == text, "structuredContent and text JSON differ"
    return text


async def terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.wait(), timeout=5)


async def cli(env: dict[str, str], directory: str, *arguments: str) -> dict[str, Any]:
    started = perf_counter()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-X",
        "utf8",
        "-m",
        "teleloom",
        *arguments,
        cwd=directory,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        return json.loads(stdout)
    finally:
        await terminate(process)
        TIMINGS.append(
            {
                "stage": "cli_" + arguments[0],
                "exit": process.returncode,
                "seconds": round(perf_counter() - started, 6),
                "workers": 0,
            }
        )


async def main(*, inject_failure: bool = False, lifetime_report: Path | None = None) -> None:
    version = installed_artifact()
    TIMINGS.clear()
    with tempfile.TemporaryDirectory(prefix="teleloom-projection-smoke-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        settings = Settings(data_dir=Path(directory), port=port)
        settings.save()
        token = "isolated-projection-package-token"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("TELELOOM_")
            and key not in {"TYPESAFE_API_KEY", "PYTHONPATH", "PYTHONHOME"}
        }
        env.update(TELELOOM_DATA_DIR=directory, TELELOOM_MCP_TOKEN=token, PYTHONIOENCODING="utf-8")
        # Own the process handle so unsuccessful shutdown cannot leave a daemon.
        startup_started = perf_counter()
        daemon = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-m",
            "teleloom",
            "serve",
            cwd=directory,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if lifetime_report:
                lifetime_report.write_text(
                    json.dumps({"pid": daemon.pid, "port": port, "directory": directory}),
                    encoding="utf-8",
                )
            async with asyncio.timeout(20):
                while not await health(settings, token):
                    assert daemon.returncode is None, "Temporary daemon exited before readiness"
                    await asyncio.sleep(0.05)
            TIMINGS.append(
                {
                    "stage": "owner_startup",
                    "exit": 0,
                    "seconds": round(perf_counter() - startup_started, 6),
                    "workers": 0,
                }
            )
            if inject_failure:
                raise RuntimeError("Injected failure after temporary owner readiness")
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-I", "-X", "utf8", "-m", "teleloom", "mcp"],
                cwd=directory,
                env=env,
            )
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                operation_schema = tools["message_operation_preview"].inputSchema["properties"][
                    "operation"
                ]
                assert operation_schema["discriminator"]["propertyName"] == "kind"
                assert tools["message_operation_preview"].annotations.readOnlyHint is False
                assert tools["message_state"].annotations.readOnlyHint is True
                assert {"unread_only", "unmuted_only", "archived"} <= tools[
                    "chats_list"
                ].inputSchema["properties"].keys()
                assert "query" in tools["topics_list"].inputSchema["properties"]
                assert tools["unread_export_start"].annotations.readOnlyHint is False
                assert tools["unread_export_start"].annotations.destructiveHint is False
                unavailable_export = wire(
                    await client.call_tool(
                        "unread_export_start", {"profile_id": "missing", "chat_ids": ["100"]}
                    )
                )
                assert unavailable_export["error"]["code"] == "profile_not_found"
                for name in READERS | {"response_fields_select"}:
                    assert {"fields", "preset"} <= tools[name].inputSchema["properties"].keys()
                delivery = {
                    "delivery_preview": (
                        {
                            "profile_id",
                            "recipients",
                            "text",
                            "reply_to_message_id",
                            "broadcast",
                            "owner_authorized",
                        },
                        {"profile_id", "recipients", "text"},
                        "broadcast",
                        False,
                    ),
                    "delivery_execute": (
                        {"profile_id", "plan_id", "plan_hash", "confirmed"},
                        {"profile_id", "plan_id", "plan_hash"},
                        "confirmed",
                        True,
                    ),
                }
                for name, (properties, required, default_flag, destructive) in delivery.items():
                    tool = tools[name]
                    assert set(tool.inputSchema["properties"]) == properties
                    assert set(tool.inputSchema["required"]) == required
                    assert tool.inputSchema["properties"][default_flag]["default"] is False
                    assert tool.annotations and tool.annotations.destructiveHint is destructive
                    assert tool.outputSchema == tools["server_status"].outputSchema
                assert (
                    tools["delivery_preview"].inputSchema["properties"]["owner_authorized"][
                        "default"
                    ]
                    is False
                )
                for name in ("inbox_ack", "jobs_control", "jobs_status"):
                    assert not {"fields", "preset"} & tools[name].inputSchema["properties"].keys()
                assert {
                    "events_wait_start",
                    "transcription_start",
                    "transcription_capabilities",
                } <= tools.keys()
                for name in ("events_wait_start", "transcription_start"):
                    assert tools[name].annotations.readOnlyHint is False
                assert (
                    tools["transcription_start"].inputSchema["properties"]["allow_external_upload"][
                        "default"
                    ]
                    is False
                )
                status = wire(await client.call_tool("server_status", {}))
                assert status["ok"] and status["data"]["version"] == version
                assert status["data"]["profiles"] == 0
                build = status["data"]["build"]
                assert set(build) == {"package_version", "build_id", "source_commit", "build_type"}
                assert build["package_version"] == version
                assert all(isinstance(value, str) and value for value in build.values())
                artifact_file = Path(teleloom.__file__).with_name("_build_info.json")
                original_artifact = artifact_file.read_bytes()
                try:
                    upgraded = {**build, "build_id": "simulated-upgrade"}
                    artifact_file.write_text(json.dumps(upgraded), encoding="utf-8")
                    upgraded_doctor = await cli(env, directory, "doctor", "--mode", "mcp")
                    assert upgraded_doctor["build"] == upgraded
                    assert upgraded_doctor["daemon"]["build"] == build, (
                        "Running owner must retain its startup artifact identity"
                    )
                    assert any(
                        check["name"] == "daemon_build" and check["ok"] is False
                        for check in upgraded_doctor["checks"]
                    )
                    assert any(
                        "teleloom stop" in action for action in upgraded_doctor["recovery_actions"]
                    )
                finally:
                    artifact_file.write_bytes(original_artifact)
                profiles = wire(await client.call_tool("profiles_list", {}))
                assert profiles["data"]["profiles"] == []
                selection = wire(
                    await client.call_tool(
                        "response_fields_select",
                        {"tool_name": "digest_context", "request": "Digest for the last 48 hours"},
                    )
                )
                chosen = selection["data"]
                assert chosen["status"] == "disabled" and "reactions" in chosen["omitted"]
                assert {"text", "profile_id", "chat_id", "id", "date", "link"} <= set(
                    chosen["fields"]
                )
                for options in ({"fields": chosen["fields"]}, {"preset": "digest"}):
                    denied = wire(
                        await client.call_tool(
                            "messages_get",
                            {"profile_id": "missing", "chat_id": "100", **options},
                        )
                    )
                    assert denied["error"]["code"] == "profile_not_found"
                invalid = wire(
                    await client.call_tool(
                        "messages_get",
                        {"profile_id": "missing", "chat_id": "100", "fields": ["invented"]},
                    )
                )
                assert invalid["error"]["code"] == "invalid_projection"
                rejected_delivery = wire(
                    await client.call_tool(
                        "response_fields_select",
                        {"tool_name": "delivery_execute", "request": "Hide the text"},
                    )
                )
                assert rejected_delivery["error"]["code"] == "unsupported_projection"

            selected_cli = await cli(
                env,
                directory,
                "fields",
                "digest_context",
                "--request",
                "Digest for the last 48 hours",
            )
            assert selected_cli["data"]["fields"] == chosen["fields"]
            explicit_cli = await cli(
                env,
                directory,
                "call",
                "response_fields_select",
                "--args",
                '{"tool_name":"messages_get","request":"Show counts"}',
                "--field",
                "text",
                "--field",
                "entities",
            )
            assert explicit_cli["data"]["status"] == "explicit"
            assert {"text", "entities"} <= set(explicit_cli["data"]["fields"])
            preset_cli = await cli(
                env,
                directory,
                "fields",
                "messages_get",
                "--request",
                "Show counts",
                "--preset",
                "digest",
            )
            assert preset_cli["data"]["status"] == "explicit"
            assert "text" in preset_cli["data"]["fields"]
            assert "reactions" in preset_cli["data"]["omitted"]
            local_doctor = await cli(env, directory, "doctor", "--mode", "local")
            mcp_doctor = await cli(env, directory, "doctor", "--mode", "mcp")
            for doctor in (local_doctor, mcp_doctor):
                assert doctor["build"] == build
                assert doctor["package"]["origin"] == "installed"
                assert doctor["package"]["version"] == version
                assert doctor["package"]["version_matches"] is True
                assert doctor["tools"]["count"] == len(tools)
                assert {"optional_engines", "checks", "recovery_actions"} <= doctor.keys()
                for name in READERS:
                    assert {"fields", "preset"} <= doctor["tools"]["schemas"][name][
                        "parameters"
                    ].keys()
            assert local_doctor["mode"] == "local"
            assert local_doctor["daemon"]["status"] == "not_checked"
            assert local_doctor["tools"]["source"] == "installed_package"
            assert mcp_doctor["mode"] == "mcp"
            assert mcp_doctor["daemon"]["status"] == "running"
            assert mcp_doctor["tools"]["source"] == "running_mcp"
            evaluation = await cli(env, directory, "evaluate")
            assert evaluation["ok"] and evaluation["mode"] == "replay"
            assert evaluation["report_version"] == 1
            assert evaluation["corpus_version"] == "response-fields-2026-10-05-v1"
            assert len(evaluation["cases"]) == 12
            assert {case["language"] for case in evaluation["cases"]} == {"ru", "en"}
            assert all(not case["violations"] for case in evaluation["cases"])
            regression = next(
                case for case in evaluation["cases"] if case["id"] == "ru-summary-regression"
            )
            assert regression["mode"] == "replay"
            assert regression["raw_scores"]["text"] == 0.29
            assert {"text", "entities"} <= set(regression["final_fields"])
            assert "text" in regression["safeguards"]
            assert regression["schema_id"] and regression["policy_version"]
            assert all(case["mcp"]["representations_agree"] for case in evaluation["cases"])

            benchmark = await cli(env, directory, "benchmark")
            assert benchmark["ok"] and benchmark["mode"] == "replay"
            assert benchmark["telegram_mode"] == "fake"
            assert benchmark["sizes"] == [1, 20, 100]
            assert benchmark["corpus_version"] == evaluation["corpus_version"]
            rows = {(row["messages"], row["strategy"]): row for row in benchmark["rows"]}
            assert len(rows) == 12
            for size in (1, 20, 100):
                for strategy in ("full", "preset", "jev_uncached", "jev_cached"):
                    row = rows[size, strategy]
                    assert row["mode"] == "fake" and not row["violations"]
                    assert row["integrity"] == row["expected_integrity"]
                    assert all(call["representations_agree"] for call in row["mcp_calls"])
                    assert all(
                        call["structured_content_bytes"] > 0 and call["text_json_bytes"] > 0
                        for call in row["mcp_calls"]
                    )
                assert all(rows[size, "jev_uncached"]["integrity"].values())
                assert all(rows[size, "jev_cached"]["integrity"].values())
                assert rows[size, "jev_cached"]["selection_status"] == "cached"
                assert rows[size, "jev_cached"]["external_calls"]["jev"] == 0
                assert rows[size, "preset"]["limitation"]
            assert rows[1, "jev_uncached"]["savings_bytes"] < 0
            bundled = files("teleloom") / "bundled_skills"
            skills = {
                child.name
                for child in bundled.iterdir()
                if child.is_dir() and (child / "SKILL.md").is_file()
            }
            assert skills == SKILLS
            for skill in ("teleloom-read", "teleloom-digest", "teleloom-inbox"):
                assert "response_fields_select" in (bundled / skill / "SKILL.md").read_text(
                    encoding="utf-8"
                )
            completed = {
                "version": version,
                "installed_artifact": "passed",
                "tools": len(tools),
                "selection_stdio": "passed",
                "selection_cli": "passed",
                "projection_parameters": "passed",
                "wire_formats": "passed",
                "delivery_schemas": "passed",
                "doctor_modes": "passed",
                "build_metadata": "passed",
                "stale_daemon_detection": "passed",
                "evaluator_replay": "passed",
                "benchmark_fake": "passed",
                "simulated_jev_calls": evaluation["external_calls"]
                + benchmark["external_calls"]["jev"],
                "simulated_telegram_calls": benchmark["external_calls"]["telegram_fake"],
                "bundled_skills": len(skills),
                "telegram_profiles": 0,
                "telegram_calls": 0,
                "jev_calls": 0,
            }
        finally:
            cleanup_started = perf_counter()
            previous = os.environ.get("TELELOOM_MCP_TOKEN")
            os.environ["TELELOOM_MCP_TOKEN"] = token
            try:
                await stop_daemon(settings)
            finally:
                try:
                    try:
                        await asyncio.wait_for(daemon.wait(), timeout=5)
                    except TimeoutError:
                        await terminate(daemon)
                finally:
                    if previous is None:
                        os.environ.pop("TELELOOM_MCP_TOKEN", None)
                    else:
                        os.environ["TELELOOM_MCP_TOKEN"] = previous
            assert daemon.returncode is not None, "Owned process exit was not awaited"
            with socket.socket() as closed:
                closed.settimeout(0.5)
                assert closed.connect_ex(("127.0.0.1", port)) != 0, (
                    "Temporary owner port stayed open"
                )
            TIMINGS.append(
                {
                    "stage": "owner_cleanup",
                    "exit": daemon.returncode,
                    "seconds": round(perf_counter() - cleanup_started, 6),
                    "workers": 0,
                }
            )
            directory_cleanup_started = perf_counter()
    TIMINGS.append(
        {
            "stage": "directory_cleanup",
            "exit": 0,
            "seconds": round(perf_counter() - directory_cleanup_started, 6),
            "workers": 0,
        }
    )
    completed["timings"] = TIMINGS
    # TemporaryDirectory cleanup is a gate; emit completion only after it succeeds.
    print(json.dumps(completed))


async def bounded_main(
    *, inject_failure: bool = False, lifetime_report: Path | None = None
) -> None:
    # Cancellation still enters main's finally and shuts down its owned daemon.
    async with asyncio.timeout(180):
        await main(inject_failure=inject_failure, lifetime_report=lifetime_report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inject-failure", action="store_true")
    parser.add_argument("--lifetime-report", type=Path)
    arguments = parser.parse_args()
    asyncio.run(
        bounded_main(
            inject_failure=arguments.inject_failure, lifetime_report=arguments.lifetime_report
        )
    )
