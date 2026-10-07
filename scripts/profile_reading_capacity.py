"""One fixed local reading capacity comparison; see docs/research/reading-capacity-gate.md."""

import argparse
import asyncio
import cProfile
import ctypes
import gc
import json
import logging
import os
import platform
import pstats
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Run from the repository root; shared external fake and real MCP transport.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from teleloom.config import Profile, Settings  # noqa: E402
from teleloom.daemon import LocalAuth  # noqa: E402
from teleloom.models import Message  # noqa: E402
from teleloom.runtime import Runtime  # noqa: E402
from teleloom.server import create_server  # noqa: E402
from tests.fakes import TelegramAPI, data  # noqa: E402
from tests.test_transport import client  # noqa: E402

NOW = datetime(2026, 10, 6, tzinfo=UTC)


class CapacityAPI(TelegramAPI):
    async def history_batch(self, chat, **kwargs):
        return {"items": messages(100), "next_before": None, "complete": True}


def messages(count):
    return [
        Message(
            profile_id="personal",
            chat_id="100",
            id=str(i),
            date=NOW - timedelta(minutes=1),
            text="x" * 96,
            link=f"https://t.me/capacity/{i}",
        )
        for i in range(count, 0, -1)
    ]


def process_memory():
    if sys.platform != "win32":
        import resource

        return {"peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    current_process = ctypes.windll.kernel32.GetCurrentProcess
    current_process.restype = ctypes.c_void_p
    memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    memory_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not memory_info(current_process(), ctypes.byref(counters), counters.cb):
        return {"unavailable": True}
    return {
        "rss_bytes": counters.WorkingSetSize,
        "process_lifetime_peak_rss_bytes": counters.PeakWorkingSetSize,
    }


async def fixture(directory, count, history, profiler):
    settings = Settings(data_dir=directory, profiles={"personal": Profile(kind="user")})
    runtime = Runtime(settings, CapacityAPI)
    server = create_server(settings, runtime=runtime)
    app = LocalAuth(server.streamable_http_app(), "test-owner-token", settings.port)
    try:
        async with server.session_manager.run(), client(app, settings) as session:
            job_id = data(
                await session.call_tool(
                    "digest_context_many_start",
                    {
                        "profile_id": "personal",
                        "chat_ids": ["100"],
                        "since": (NOW - timedelta(days=1)).isoformat(),
                        "until": NOW.isoformat(),
                        "max_messages": count,
                        "max_characters": count * 96,
                    },
                )
            )["data"]["job_id"]
            job = runtime.store.get("jobs", job_id)
            # Seed the deterministic supported late checkpoint, excluding setup from timings.
            job["payload"].update(
                before=101, requests=(count - 100) // 100, characters=(count - 100) * 96
            )
            job["progress"] = count - 100
            job["result"]["items"] = [
                row.model_dump(mode="json") for row in messages(count) if int(row.id) > 100
            ]
            job["result"]["coverage"]["chats"][0].update(returned=count - 100, status="reading")
            archive = json.loads(json.dumps(job))
            archive.update(status="completed", progress=1000)
            archive["result"]["items"] = [row.model_dump(mode="json") for row in messages(1000)]
            archive["result"]["incomplete"] = False
            archive["payload"].update(chat_index=1, before=None, characters=96000, requests=10)
            archive["result"]["coverage"]["chats"][0].update(
                returned=1000, complete=True, status="completed"
            )
            with runtime.store.db:
                for index in range(history):
                    archive["id"] = f"archive-{index:024d}"
                    runtime.store.put("jobs", archive)
                runtime.store.put("jobs", job)

            async def reset_tick():
                with runtime.store.db:
                    runtime.store.put("jobs", job)

            async def reset_snapshot():
                with runtime.store.db:
                    runtime.store.db.execute(
                        "DELETE FROM state WHERE key LIKE 'reading_results:%' OR key LIKE 'evidence_ref:%'"
                    )

            async def status():
                return await session.call_tool(
                    "jobs_status", {"profile_id": "personal", "job_id": job_id}
                )

            async def snapshot():
                return await session.call_tool(
                    "jobs_results", {"profile_id": "personal", "job_id": job_id, "limit": 100}
                )

            measurements = {}
            for name, operation, reset in (
                ("tick", runtime.tick, reset_tick),
                ("status", status, reset_snapshot),
                ("snapshot", snapshot, reset_snapshot),
            ):
                await reset()
                gc.collect()
                started = time.perf_counter()
                result = await operation()
                wall_seconds = time.perf_counter() - started
                await reset()
                gc.collect()
                writes = []
                decoded_job_bytes = []
                original_put, original_loads = runtime.store.put, json.loads

                def put(table, record, original_put=original_put, writes=writes):
                    original_put(table, record)
                    if table == "jobs":
                        writes.append(
                            runtime.store.db.execute(
                                "SELECT length(CAST(data AS BLOB)) FROM jobs WHERE id=?",
                                (record["id"],),
                            ).fetchone()[0]
                        )

                def loads(
                    value,
                    *args,
                    original_loads=original_loads,
                    decoded_job_bytes=decoded_job_bytes,
                    **kwargs,
                ):
                    decoded = original_loads(value, *args, **kwargs)
                    if isinstance(decoded, dict) and "payload" in decoded and "id" in decoded:
                        decoded_job_bytes.append(
                            len(value.encode("utf-8") if isinstance(value, str) else value)
                        )
                    return decoded

                runtime.store.put, json.loads = put, loads
                tracemalloc.start()
                profiler.enable()
                started = time.perf_counter()
                try:
                    result = await operation()
                    traced_seconds = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    profiler.disable()
                    tracemalloc.stop()
                    runtime.store.put, json.loads = original_put, original_loads
                measurements[name] = {
                    "wall_seconds": wall_seconds,
                    "instrumented_seconds": traced_seconds,
                    "instrumentation_overhead_seconds": traced_seconds - wall_seconds,
                    "traced_peak_bytes": peak,
                    "job_decodes": len(decoded_job_bytes),
                    "decoded_job_bytes": sum(decoded_job_bytes),
                    "checkpoint_writes": len(writes),
                    "checkpoint_bytes": sum(writes),
                    "process_memory": process_memory(),
                    "response_bytes": len(result.model_dump_json().encode("utf-8"))
                    if result
                    else 0,
                }
            finished = data(await status())["data"]
            assert finished["status"] == "completed" and finished["progress"] == count
            assert finished["result"]["coverage"]["chats"][0]["complete"]
            chosen_bytes = runtime.store.db.execute(
                "SELECT length(CAST(data AS BLOB)) FROM jobs WHERE id=?", (job_id,)
            ).fetchone()[0]
            snapshot_bytes = runtime.store.db.execute(
                "SELECT sum(length(CAST(data AS BLOB))) FROM state WHERE key LIKE 'reading_results:%'"
            ).fetchone()[0]
            return {
                "messages": count,
                "terminal_archives": history,
                "job_bytes": chosen_bytes,
                "snapshot_bytes": snapshot_bytes,
                "measurements": measurements,
            }
    finally:
        await runtime.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--background-load", required=True)
    args = parser.parse_args()
    logging.disable(logging.INFO)
    profiler = cProfile.Profile()
    report = {
        "label": args.label,
        "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "logical_cpus": os.cpu_count(),
            "background_load": args.background_load,
            "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
        },
        "samples_per_boundary": 1,
        "traced_replay_per_boundary": 1,
        "fixtures": [],
    }
    for count in (1000, 10000):
        for history in (0, 10, 100):
            with tempfile.TemporaryDirectory(prefix="teleloom-capacity-") as temporary:
                result = await fixture(Path(temporary), count, history, profiler)
            report["fixtures"].append(result)
            print(
                json.dumps(
                    {
                        "messages": count,
                        "archives": history,
                        "wall_seconds": {
                            key: round(value["wall_seconds"], 4)
                            for key, value in result["measurements"].items()
                        },
                    }
                ),
                flush=True,
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with args.output.with_suffix(".profile.txt").open("w", encoding="utf-8") as stream:
        pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(30)


if __name__ == "__main__":
    asyncio.run(main())
