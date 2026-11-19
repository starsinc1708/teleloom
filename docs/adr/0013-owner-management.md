# ADR 0013: owner management authority for exposure changes

Status: **owner-approved boundary**. On November 18, 2026, the owner approved
a separate local management process reached through an explicit CLI launch,
which survives a daemon restart and excludes concurrent daemon startup and
login during Apply. Implementation and acceptance are separate from that approval.

Scope: one scenario — owner chooses an existing exposure mode, reviews the exact
diff, applies it, and the owner comes back with the new effective exposure. Not a
universal settings editor and not a plugin/platform surface.

## What exists today (evidence)

- The panel is built inside the daemon process: `create_application` constructs
  `Panel(settings, runtime, token)` and mounts it on the same listener
  (`daemon.py:186`, dispatch at `daemon.py:99`).
- The panel bootstrap endpoint authenticates with the **MCP bearer**
  (`panel.py:276-283`). Whatever the panel can do is therefore reachable by anyone
  who can read that bearer — and the bearer is handed to agent clients by design.
- The daemon holds `owner.lock` for its whole life (`daemon.py:201`). Every CLI
  configuration write (`cli.py:158-458`, `cli.py:950`) and the login path
  (`cli.py:184`, after `teleloom stop`) take the same lock. A second `FileLock`
  instance on that path times out even inside the same process (spike below), so no
  in-process "apply" can exist.
- Exposure is applied while tools are registered (`server.py:139-173`), so a change
  is only real after a restart.
- A restart invalidates every panel session and cookie (ADR 0012, "Bootstrap and
  browser session"). The process that performs the change therefore cannot display
  its result: the page that asked is gone.

Consequence: the daemon must not own the management authority. It owns Telegram,
serves `/mcp`, and renders `/ui`; it never writes `config.json`.

## Decision: three authorities, three credentials, one writer

| Role | Holds | May do | Never |
| --- | --- | --- | --- |
| CLI / OS user | nothing at rest | start/stop `control` and the owner, read state | be reachable over MCP |
| `control` (new) | `control.lock`, its own bootstrap/session | stop owner, write exposure, restart owner, report result | open Telegram, keep the MCP bearer, expose an MCP route |
| owner daemon | `owner.lock`, MCP bearer, `/ui` session | read, jobs, panel | write `config.json`, issue management grants |

`teleloom control` is a separate local process started explicitly from the CLI
(`teleloom control`, not through the daemon or any MCP client). Implementation note:
it runs foreground in that CLI process rather than as a detached child of
`teleloom ui --manage`; that keeps one launch path, one handoff and one place that
prints the URL. It binds its own **ephemeral loopback port**, mints a single-use
bootstrap (same primitive as ADR 0012: fragment → HttpOnly, `SameSite=Strict`,
port-qualified cookie, its own CSRF token), and exposes only
`/control/open` + `/control/session` (one-time exchange), `GET /control` (current
state + exact diff) and `POST /control/apply` (CSRF-protected). It never shares the panel's cookie, origin,
port or process, and it is useless to another local listener because only it knows
its session. It stays available after reporting a result, so a lost browser
response is recovered by reloading `/control`; Ctrl+C ends it. An idle timeout was
dropped: a foreground process already ends with its terminal.

The browser reaches it by top-level navigation, not `fetch`: `teleloom control`
prints and opens the control URL itself, and adds no cross-origin request to the
panel. ADR 0012's same-origin and Host policy is untouched; the panel page is
unchanged and gains no management link.

Rejected alternative: proxy `/ui/manage/*` through the daemon. It keeps one origin
but makes the daemon a path to the management authority, and the daemon is the
process the agent's tools run in. The boundary would be nominal.

## Revision: bytes, not schema

The revision is `sha256` of the actual `config.json` bytes read by `control`, shown
with the diff and recomputed under the lock immediately before writing. A mismatch
returns `409 conflict` with both digests and the instruction to reload; nothing is
written. Apply then loads `Settings` **fresh from disk**, replaces only
`exposure_mode`/`exposed_tools`, and saves through the existing atomic
tmp+replace path (`config.py:168-174`).

No revision field, no migration, no second config source. Apply reads the raw JSON
document, sets only `exposure_mode`/`exposed_tools`, and writes it back through the
same tmp+replace path. Unlike `Settings.save()` (a pydantic round trip that drops
unknown keys), this preserves unrelated and unknown stored fields exactly; the
scratch spike that motivated the earlier limitation is superseded by that
implementation choice. `teleloom config exposure` now uses the same helper.

## Preconditions (checked before stop and again before write)

Refuse, with one actionable message each, when:

- `store.active_jobs()` is non-empty (queued/running) — tasks are never cancelled to
  change a setting.
- any job has a delivery in `sending`/`unknown` or a transcription receipt in
  `sending`/`unknown` (`jobs.py:952-971`) — unknown work is preserved for review,
  never replayed.
- `owner.lock` is held by a login or another mutation rather than the daemon we
  intend to stop — report `owner_busy`, retry.

Expected side effects that must be stated, not hidden: switching to `read-only`/
`selected` pauses pending external delivery and denies resume while delivery is
excluded (ADR 0006); restart rewrites `sending` → `unknown` and `running` →
`queued` (`jobs.py:62-73`). The transition asserts the unknown-delivery count
before and after is equal.

## Locking order and the one honest gap

`control.lock` (new, `data_dir`, `timeout=0`, OS-released — measured at 1 ms after a
kill) → `owner.lock` (timeout 0; proves no daemon, login or CLI write is active) →
verify digest → drain check → write → release `owner.lock` → spawn
`teleloom serve` via the existing `ensure_daemon`/`startup.lock` path
(`daemon.py:257-311`). No second start path, no second Telegram owner, and the
stdio bridge auto-start race stays covered by `startup.lock` + `owner_busy`.

The ordering is implemented once, in `control.py`: `governed()` takes
`control.lock`, checks the record, then takes `owner.lock`, and every CLI
configuration/login path uses it. The daemon takes `claim_owner()` — the same gate
and record check, then **only** `owner.lock` for its lifetime; it must never hold
`control.lock`, or later management runs could not start. `ensure_daemon` probes
the gate and a live record before spawning, so bridge auto-start refuses with
`transition_in_progress` instead of launching a doomed process.

Windows offers no file-lock handoff, so the release→spawn window cannot be closed
by a lock: the lock that would prove continuity is the one being released. It is
closed by a **durable transition record** plus a **one-use handoff** — `state`/
`transition` in the existing SQLite store, phases
`drafted → stopped → written → starting → verified` (or `failed`, `abandoned`),
carrying the control process identity (pid, start time, executable). Before
spawning, `control` writes `starting` and the digest of a random handoff token;
it passes the token only in the child's environment (the same channel that already
carries `TELELOOM_MCP_TOKEN`), and `serve` may pass the live record only when the
digest matches, consuming the token. Ordinary `serve`, login and CLI writes pass
no handoff and refuse with `transition_in_progress` while the record's manager is
alive. A record whose manager is gone becomes `abandoned` on the next action; it
is reported, never silently retried and never rolled back. Pid reuse can only make
that check fail closed (temporarily refuse), never authorize a write.

## Recovery for intermediate failure

| Ends after | Durable truth | What the owner sees / does |
| --- | --- | --- |
| before write | config untouched, record `stopped` | run Apply again; revision still matches |
| write raises | config untouched (tmp+replace), record `written`→`failed` | permission/disk message; no restart attempted |
| write done, owner down | new config durable, record `written` | `teleloom serve` or rerun Apply; no rollback of the current base |
| owner spawned, unverified | record `starting` | next action checks liveness and finalizes `verified`/`failed` |
| browser lost the response | record + config are authoritative | reopening control reads actual state; Apply is idempotent |

Repeating Apply with an unchanged configuration is a no-op: same digest, same
projection, no restart.

## Verifying success

- Liveness: `ensure_daemon` waits for `/health`, so the process answers after spawn.
- Identity by construction and observation: same `data_dir`, same installed
  interpreter (`daemon.py:189-195`), same `build_info()` artifact; `control`
  compares the observed `workspace_id` (and `service`) from `/health` with its own
  fingerprint and records the observed `owner_id`/version. A foreign or replaced
  owner therefore fails verification instead of being reported as success.
- Effective exposure: only a process holding the bearer can list registered tools,
  so `Lifecycle.observed` opens one ordinary MCP session (`daemon.session`) and reads
  `tools/list` plus `server_status`. It reads the bearer from the OS store for that
  call and never stores or hands it to the browser; it is not a second credential.
  The restart counts as verified only when the observed `workspace_id`, `build`,
  `scope.tool_exposure` and `tools/list` agree with the selection: `selected` must
  list exactly the chosen names, `all` every registered tool, `read-only` a subset
  of them. Any difference reports the mismatch and the durable configuration instead
  of claiming success. The UI states that the MCP client must reconnect to refresh
  its discovery cache.

## Implementation surface (work/teleloom-23)

- `src/teleloom/control.py` — locks, transition record, byte revision, raw-document
  exposure write, blockers, the `/control` app and the `Lifecycle` seam.
- `src/teleloom/daemon.py` — `claim_owner()` startup, handoff environment, gate and
  live-record refusal in `ensure_daemon`.
- `src/teleloom/cli.py` — all configuration/login writes through `governed()`;
  `teleloom config exposure` uses the shared raw write; new `teleloom control`.
- `tests/test_panel_control.py` — owner-only bootstrap/CSRF/origin, exact diff and
  foreign-field preservation, stale revision, active job and unknown delivery
  refusal, cross-process exclusion and dead-holder release, one-use handoff,
  stop/start failure honesty, idempotent no-op. No real Telegram or keyring.

Observed end to end with real temporary processes and SQLite (fake Telegram, no
profiles, `TELELOOM_MCP_TOKEN` overridden): the `/control` page was driven in Chromium
through the one-time bootstrap, a review of `read-only` (exact diff `all -> read-only`)
and the Apply form. Apply returned a verified result naming the observed build and 54
listed tools; `server_status` then reported `read-only` with `messages_get` present
and `send_message` hidden, the build and workspace matched, `config.json` kept
`future_setting` and the port, and the record ended `verified` with no handoff left.
Reloading `/control` showed `read-only` and a disabled Apply with "No difference from
the current configuration", i.e. a repeated Apply restarts nothing.

The listener itself is reserved before anything is issued: `Control` binds and listens
on the loopback socket at construction, the bootstrap URL carries that socket's port,
and uvicorn serves the same socket (`Server.run(sockets=[...])`) rather than re-binding
the number. The URL is printed and the browser opened only after uvicorn reports
started, so no other local process can take the port in between and no page is opened
against a listener that does not answer yet. No unauthenticated health route exists.

## Compatibility with #20 / #32, and what stays non-mutating

#20 (`/ui` read-only, bootstrap, Host/Origin, sessions) is unchanged; #21 and #22
keep extending `Panel`. #23 adds one process, one CLI command and one durable record.

#32 requires the same-port local/remote SSH forward that ADR 0012 assumes; an
ephemeral control port cannot be forwarded predictably, so remote **management** is
explicitly deferred — remote owners keep the read-only panel until #32 decides a
forwarded control port. Local management ships first.

#27 (client connect / runtime skills) is not authorized to mutate anything by this
ADR and ships read-only export until it reuses `control.lock`, the transition phases
and the byte revision. One writer, one lock, one revision — no parallel installer.

## Approved decisions

The control process is separate from the daemon and its bootstrap is never
authorized by the MCP bearer. Startup and login must refuse during a live
transition, not merely detect a conflict after starting. A crashed transition
must leave enough durable state for an explicit recovery without replay.

## Not in scope

Public or non-loopback listeners, multi-user or remote management, rollback of a
written configuration, a raw settings editor, and any new MCP tool.

## Evidence

Scratch spike (`tl23_lock_spike.py`, throwaway, no real settings or credentials):
a second `FileLock` in the same process times out (no in-process apply); a live
holder blocks another process; a killed holder releases the lock in 1 ms
(OS-released ownership, restart-safe); a model round trip preserves other known
fields and changes the byte digest, and drops unknown JSON keys. The last point is
why the implementation writes the raw document instead of a model round trip.
Code references above are from `work/teleloom-23` at base `a5e6a85` plus the
#20/#21/#22/#32 snapshot.
