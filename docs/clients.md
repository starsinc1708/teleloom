# Agent clients

Run `teleloom config client --client CLIENT` from the installed environment to print
a fragment, then merge it into the host configuration. Generated commands use the
installed interpreter and `-m teleloom mcp`; the bridge loads credentials itself.
Printing never registers MCP. Explicit `--install` supports OpenCode 2 and Hermes
through their native CLIs; other clients still require a manual merge.

From a checkout containing native installation support (v0.5.0 only prints fragments),
choose the command pair for your client:

```sh
uv run teleloom config client --client opencode --install
uv run teleloom skills install --client opencode
uv run teleloom config client --client hermes --install
uv run teleloom skills install --client hermes
```

The client CLI must be on PATH. OpenCode runs `mcp add teleloom --global`; Hermes
runs `mcp add teleloom --command ... --env ... --args ...`. Both receive the same
interpreter and `TELELOOM_DATA_DIR` as the printed fragment, without shell execution.
Existing settings and other servers are left to the native client's merge logic.
An identical active connection returns `already_configured` without rewriting it,
retaining client-specific options. A different command, arguments, environment or
disabled connection is a conflict and requires manual review; there is no automatic
replacement. On Windows, equivalent executable/data-directory paths (including
repeated separators) match without rewriting them; other arguments and environment
values remain exact. Hermes receives one empty response to enable discovered tools;
its refusal defaults for overwrite or failed discovery remain in effect.
After `mcp add`, Teleloom checks that the requested active entry was
saved. Missing CLI, command failure, cancellation or unreadable configuration is an
error, never installation success. Registration alone does not establish live
Telegram connectivity; reconnect and use the discovery checks below.

On Windows, generated fragments use the sibling pythonw.exe when available, so
GUI clients can start the stdio bridge without opening a console. Existing
python.exe fragments need this command-path update and a host restart. Linux
commands and interactive CLI/auth invocations keep their normal interpreter.

| Client | Configuration | Skill installation |
|---|---|---|
| Codex | TOML `mcp_servers.teleloom` in its config | `teleloom skills install --client codex` → `~/.agents/skills` |
| Claude Code | JSON `mcpServers.teleloom`, or `claude mcp add-json` | `--client claude` → `~/.claude/skills` |
| Claude Desktop | JSON `mcpServers.teleloom` | Skill loading depends on host; use workflow instructions manually if unavailable |
| OpenCode 2 | `mcp.servers.teleloom`, type local, command array | `--client opencode` → `~/.config/opencode/skills` |
| OpenCode 1 | `mcp.teleloom`, with `enabled: true` | `--client opencode-v1` → `~/.config/opencode/skills` |
| Hermes | YAML-compatible JSON `mcp_servers.teleloom` fragment | `--client hermes` → `skills/` beside `hermes config path` |
| Pi | JSON `mcpServers.teleloom` in `~/.pi/agent/mcp.json` | `--client pi` → `~/.pi/agent/skills` |
| Other hosts | stdio command `teleloom mcp` | `--target PATH`, or load instructions manually |

Public v0.5.0 includes the OpenCode 2, explicit v1 and Pi targets.

Merge only the `teleloom` server entry into existing configurations; keep unrelated
settings and servers. OpenCode uses `~/.config/opencode/opencode.json(c)` (or a project
`opencode.json(c)`), Hermes resolves its configuration through `hermes config path`,
and Claude Desktop uses
`%APPDATA%/Claude/claude_desktop_config.json` on Windows. For Claude Code, pass the
single server object, without its `mcpServers` wrapper, to
`claude mcp add-json --scope user teleloom JSON`.

Hermes resolves `HERMES_HOME`, its platform-specific installation home and the
active profile itself. Teleloom installs skills beside the returned config, such
as `%LOCALAPPDATA%/hermes/skills` on the reported Windows installation or
`HERMES_HOME/profiles/work/skills` for an active named profile. It does not fall
back to a guessed `~/.hermes` directory if discovery fails. Explicit `--target PATH`
takes priority and skips client discovery. Existing skills remain protected unless
`--force` is supplied.

Check native discovery without a model call:

```text
opencode reload
opencode mcp list
hermes mcp test teleloom
pi mcp list
claude mcp list
```

OpenCode 2 starts connections asynchronously. An initial empty/pending result
is inconclusive: leave its service running, then check again after startup.
Reload a running service after registering MCP. `opencode api mcp.list` exposes
the native v2 connection status for the location in its response. Do not interpret
an empty catalog as success. Pi's current official package is
`@earendil-works/pi-coding-agent`; native MCP does not require another extension.

DeepSeek/GLM are model choices in a supporting host. A model name is not a client
installation target. Configure the host, then choose the model separately.

After upgrading the package, run diagnostics from the same installed environment
used by the generated command:

```text
teleloom doctor --mode local
teleloom doctor --mode mcp
```

Local mode checks the package and optional engines without starting an owner.
MCP mode requires an already running owner and reports its metadata, discovered
tools and key schema parameters without autostart, Telegram reads or AI calls.
The legacy `--live` flag is only an alias for MCP connectivity. Compare
`package_version`, `build_id`, `source_commit` and `build_type`; the same version
can identify different local builds. Unknown provenance remains explicit.

Regenerate the fragment when its interpreter changed. Stop the owner using
`teleloom stop`, restart the host and reconnect MCP after an intentional upgrade;
discovery metadata is cached by the bridge. Then use MCP diagnostics to verify
the running owner's build and `response_fields_select` plus reader
`fields`/`preset` parameters. See
[build provenance](adr/0005-build-provenance.md) and
[the upgrade procedure](install-ru.md#проверка-сборки-и-обновление).

For direct Streamable HTTP use `http://127.0.0.1:8765/mcp` (configured port) and
`Authorization: Bearer OWNER_TOKEN`. Provision tokens privately using environment or
OS credential tooling; do not paste them into conversations. Stdio avoids putting
tokens in host config files. Use `TELELOOM_DATA_DIR` for an explicit workspace.

## Protocol compatibility and recovery

The lockfile selects Python MCP SDK **1.30.0** (`mcp>=1.30,<2`). Both supported
transports use `initialize` → `notifications/initialized` → `tools/list` →
`tools/call`. SDK revisions are `2024-11-05`, `2025-03-26`, `2025-06-18` and
`2025-11-25`. An unknown revision proposed in `initialize` negotiates
`2025-11-25`; the client must accept that revision or disconnect.

| Mode or observation | Contract / recovery |
|---|---|
| Stdio `teleloom mcp` | Use the generated client command and the initialization handshake. Each bridge caches discovery and forwards calls to the same owner. |
| Streamable HTTP `/mcp` | Authenticated loopback endpoint, stateless JSON responses. Send `Accept: application/json, text/event-stream` and the negotiated `MCP-Protocol-Version` header after initialization. |
| Modern `server/discover` / pinned `2026-07-28` | Unsupported. `server/discover` returns JSON-RPC `-32601`; unsupported version metadata/header on tool requests returns `-32600` (HTTP 400). Both errors name the supported handshake and revisions. Select the host's legacy/initialize mode; Python SDK v2 clients expose `mode="legacy"`. |
| Old HTTP+SSE `/sse` endpoint | Unsupported (HTTP 404). Configure stdio or Streamable HTTP `/mcp`. The Streamable HTTP Accept header does not enable the old SSE transport. |
| Empty or unexpected catalog | Check owner exposure and workspace, run `teleloom doctor --mode mcp` against the installed environment, then reconnect. An intentionally empty `selected` set can expose zero tools; default `all` must expose the complete catalog. A discovery error is a failed connection, never a successful empty catalog. |
| Second client / reconnect | Keep the same data directory, port and owner credentials. Closing a bridge leaves the owner running; reconnect creates a bridge, without another Telegram connection. |
| Owner stopped during a call | A later call may start the owner again. The interrupted request is never replayed; inspect job/receipt state before retrying a mutation. |
| Exposure or package changed | Stop/restart the intended owner and reconnect each client to refresh cached discovery. Hidden tools remain unavailable to dispatch, even from an old catalog. |

Automated public MCP checks use SDK 1.30.0, real HTTP/stdio and temporary SQLite
with fake Telegram. They cover every listed handshake revision and fallback,
identical schemas in HTTP and two stdio bridges, allowed reads after reconnect,
and rejection of hidden writes: 67 tools in `all`, 54 in `read-only`, and three in
the tested `selected` set. Modern discovery and tool requests are rejected before
dispatch, including requests with version metadata but no HTTP version header.
These checks do not establish native host GUI or live Telegram acceptance.
SDK major migration requires a separate accepted compatibility decision.

Protocol sources: [SDK 1.30.0 revisions](https://github.com/modelcontextprotocol/python-sdk/blob/v1.30.0/src/mcp/shared/version.py),
[SDK initialization](https://github.com/modelcontextprotocol/python-sdk/blob/v1.30.0/src/mcp/server/session.py),
[MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
and [modern client modes](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/protocol-versions.md).
Reproduce with `uv run pytest -n 0 tests/test_transport.py tests/test_stdio.py`.

Verification status: native Codex observed the installed v0.4.0 owner and its
67-tool catalog. OpenCode 2.0.24, Hermes 0.21.5, Pi 1.0.4 and Claude Code 2.1.268
connected to that owner using isolated client configurations; OpenCode, Hermes
and Pi reported 67 tools. [Acceptance](acceptance.md#native-client-discovery--2026-10-07)
records the commands and limits. Claude Desktop GUI was explicitly waived.
The official Python MCP client is covered in automated HTTP/stdio tests.
Native model workflows and skill discovery were not exercised in these checks.
A host must support tool calling, MCP and any desired native skill discovery.

Sources: [OpenCode 2 MCP](https://dev.opencode.ai/v2/docs/mcp-servers/),
[OpenCode 1 MCP](https://opencode.ai/docs/mcp-servers/),
[OpenCode 2 Skills](https://dev.opencode.ai/v2/docs/skills/),
[Pi MCP](https://pi.dev/docs/latest/mcp), [Pi Skills](https://pi.dev/docs/latest/skills),
[Hermes MCP](https://github.com/hermes-agent-org/hermes/blob/main/website/docs/user-guide/features/mcp.md),
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude Desktop local configuration](https://modelcontextprotocol.io/docs/develop/connect-local-servers),
[Agent Skills specification](https://agentskills.io/specification).
