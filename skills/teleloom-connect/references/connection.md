# Connection reference

User login uses owner-supplied API credentials from environment or credential storage.
QR lasts only until its displayed expiry and refreshes during a bounded login flow.
`--method code` is the explicit phone/code fallback. A wrong cloud password may be
retried; obey server rate limits. Never ask the agent to capture passwords.

Bot updates require `teleloom profile polling NAME --enable`. Existing webhooks and
other polling consumers are conflicts, not permission to replace an integration.
Run `teleloom doctor` for local installation/credential diagnostics. `--mode mcp`
inspects an already running owner without autostart; legacy `--live` only adds its
local profile snapshot. Inspect `scope` for identity, effective chat grants and
tool exposure, and `failure` for its phase and safe next action. Stop/restart the
configured daemon after changing credentials or profile configuration.

When the owner requests live read timing, explicitly choose one readable user
profile and canonical chat ID. Call `server_status` with `profile_id`, `chat_id`
and `timeout_seconds` (default 5, at most 10), or have the owner run
`teleloom doctor --profile NAME --chat ID --timeout-seconds 5`. The normal
`messages_get` reader must be exposed. The probe uses an exact saved peer and at
most one history request for one message; it returns timings/counts without text.
An absent peer needs an explicit authorized reader to refresh that peer first.
Bot saved updates cannot establish live history health. Complete when
`probe.status=ok` for that exact selection, or report `probe.error.phase` and
`next_action`; the status envelope's `ok` alone is insufficient.

`logical_requests` are attempts, while `observed_rpc_requests=null` means no
transport RPC observation. A timeout waits for owner cleanup, then permits an
explicit selected read retry. Reconnecting the MCP client preserves the owner
session. Inspect delivery receipts after a transport interruption; uncertain
delivery needs reconciliation and never automatic replay. Use the existing owner
for another bridge or wait for its authentication flow; recovery never kills
processes. Credentials and raw exceptions stay local. Copied results/model
context are outside the server's revocation boundary.

For MCP discovery, use stdio `teleloom mcp` or authenticated Streamable HTTP `/mcp`
with `initialize` → `notifications/initialized` → `tools/list`. If the error names
unsupported `server/discover` or protocol mode, select the host's legacy/initialize
mode. A discovery failure needs client configuration recovery, not Telegram login.
For an empty catalog, have the owner check tool exposure and the configured
workspace, then reconnect. Hidden writes remain denied at dispatch. A second
client uses the same workspace/owner; reconnecting a bridge keeps its Telegram
connection. Reconnect all clients after owner exposure or package changes.

If a saved session is revoked, stop the daemon and explicitly renew it with
`teleloom auth user --profile NAME --replace`. A new profile generation resets its
permissions and isolates the previous index; old delivery plans cannot execute.
