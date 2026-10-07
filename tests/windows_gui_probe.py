"""Windows GUI parent for real CLI/MCP console-allocation checks."""

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import traceback
from ctypes import wintypes
from pathlib import Path


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("pid", wintypes.DWORD),
        ("heap", ctypes.c_size_t),
        ("module", wintypes.DWORD),
        ("threads", wintypes.DWORD),
        ("parent", wintypes.DWORD),
        ("priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
        ("exe", wintypes.WCHAR * 260),
    ]


def python_children(known: set[int]) -> list[int]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entries = []
    entry = ProcessEntry()
    entry.size = ctypes.sizeof(entry)
    try:
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            entries.append((entry.pid, entry.parent, entry.exe.lower()))
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    for _ in range(len(entries)):
        found = {pid for pid, parent, _ in entries if parent in known}
        if found <= known:
            break
        known.update(found)
    return [pid for pid, _, exe in entries if pid in known and exe.startswith("python")]


def has_console(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    assert not kernel.GetConsoleWindow(), "The test host must be a GUI process."
    if not kernel.AttachConsole(pid):
        return False
    try:
        return bool(kernel.GetConsoleWindow())
    finally:
        kernel.FreeConsole()


async def exercise(mode: str, command: list[str]) -> dict:
    from teleloom.config import Settings
    from teleloom.daemon import stop_daemon

    known = {os.getpid()}
    console_pids: set[int] = set()
    done = asyncio.Event()

    async def observe():
        while not done.is_set():
            for pid in python_children(known):
                if pid != os.getpid() and has_console(pid):
                    console_pids.add(pid)
            await asyncio.sleep(0.025)

    watcher = asyncio.create_task(observe())

    async def mcp_status():
        # Model a GUI host that provides pipes but no Windows console flags.
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            limit=1024 * 1024,  # Typed tools/list can exceed asyncio's default 64 KiB line cap.
        )

        async def request(identifier, method, params=None):
            payload = {"jsonrpc": "2.0", "method": method}
            if identifier is not None:
                payload["id"] = identifier
            if params is not None:
                payload["params"] = params
            process.stdin.write((json.dumps(payload) + "\n").encode())
            await process.stdin.drain()
            if identifier is None:
                return None
            response = json.loads(await process.stdout.readline())
            assert response["id"] == identifier and "error" not in response
            return response["result"]

        try:
            await request(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "gui-launch-test", "version": "1"},
                },
            )
            await request(None, "notifications/initialized")
            tools = await request(2, "tools/list")
            assert "folders_list" in {tool["name"] for tool in tools["tools"]}, "Missing tools"
            result = await request(3, "tools/call", {"name": "server_status", "arguments": {}})
            assert result["structuredContent"]["ok"], "Daemon unavailable"
            return result["structuredContent"]
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.terminate()
                await process.wait()

    try:
        if mode == "mcp":
            first = await mcp_status()
            # Exiting the bridge must not kill its shared daemon.
            second = await mcp_status()
            assert first["data"]["owner_id"] == second["data"]["owner_id"], "Daemon replaced"
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
                "call",
                "server_status",
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stdout, stderr = await process.communicate()
            assert process.returncode == 0, stderr.decode()
            assert json.loads(stdout)["ok"]
        await asyncio.sleep(0.1)
    finally:
        try:
            await stop_daemon(Settings.load())
        finally:
            done.set()
            await watcher
    return {"console_pids": sorted(console_pids), "public_operation": "passed"}


if __name__ == "__main__":
    output = Path(sys.argv[1])
    try:
        request = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        result = asyncio.run(asyncio.wait_for(exercise(**request), timeout=45))
    except BaseException as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        result = {"failure": type(error).__name__, "message": str(error), "line": frame.lineno}
    output.write_text(json.dumps(result), encoding="utf-8")
