# Extension map and contracts

Read the relevant implementation and its nearest test before editing. These are
extension points, not instructions to replace the architecture.

| Area | Existing location | Responsibility |
| --- | --- | --- |
| Public tools | `src/teleloom/server.py`, `src/teleloom/tools/` | Shared exposure/validation/bounded/scoped/projection pipeline in server; media workflow registration in tools/media.py; existing typed aliases in tools/__init__.py |
| Domain orchestration | `src/teleloom/runtime.py` | Explicit profiles, adapter lifecycle, validation, snapshot cursors, coverage, stable errors |
| Telegram SDK boundary | `src/teleloom/adapters.py`, `src/teleloom/telegram/evidence.py` | Public Adapter/factory and SDK lifecycle/patch seams stay in adapters; stateless SDK-to-evidence conversions live in telegram/evidence.py |
| Result shapes | `src/teleloom/models.py` | Message, chat, page and error models; backward-compatible optional evidence |
| Response projection | `src/teleloom/projection.py` | Static field catalogs, presets and removal of known optional fields after coverage calculation |
| Bounded content | `src/teleloom/output_budget.py`, `src/teleloom/reading_jobs.py` | Post-projection compact UTF-8 budgets, marked excerpts and owned exact-original JSON continuation |
| Field selection | `src/teleloom/field_selection.py` | Explicit selection priority, bounded optional Jev judgments, cache keyed by policy, content safeguards and fallback |
| Local state | `src/teleloom/store.py` | SQLite WAL/FTS, profile-scoped records, atomic checkpoint writes |
| Durable operations | `src/teleloom/jobs.py` | Start/status/control, dispatch, resumable steps, recovery and result storage |
| Owner and transport | `src/teleloom/daemon.py` | Loopback authentication, Host/Origin checks, ownership lock, stdio bridge |
| CLI and installation | `src/teleloom/cli.py` | Calls through the same MCP owner, profiles, diagnostics, client configuration and six runtime skills |
| Optional AI | `src/teleloom/analysis.py` | Per-chat opt-in classification; original evidence remains available |
| Installation diagnostics | `src/teleloom/diagnostics.py` | Local artifact checks and inspection of a running MCP owner without autostart or content reads |
| Artifact provenance | `src/teleloom/build_info.py`, `hatch_build.py` | Runtime metadata from the artifact; build-time stamping shared by wheel/sdist and editable installs |
| Selection evaluation and cost | `src/teleloom/evaluation.py` | Public MCP replay/fake evaluator and full-path benchmark on static tasks and fictional pages |

## Contract before code

Record these in the issue or a short task note; do not create another universal
architecture document for each tool:

- The concrete owner request and accepted scope.
- Tool inputs: `profile_id`, canonical string IDs, UTC-aware dates and limits.
- Output shape, source links, deterministic ordering and empty-result meaning.
- User/bot capabilities and unsupported cases.
- Snapshot identity, cursor scope, coverage, incomplete data and warnings.
- Local or Telegram side effects; permission requirements and annotation choice.
- Request/byte/message budget, cancellation and retry behavior.
- Public tests proving success, boundary cases, partial failure and isolation.

## Easy-to-miss invariants

- A message key is `(profile_id, chat_id, message_id)`. Numeric string IDs need
  numeric ordering. Telegram peer IDs must be canonical; never return access
  hashes, tokens, sessions or phone numbers as evidence.
- Date intervals are start-inclusive/end-exclusive. Telegram iterators may return
  out-of-range items. Count accepted messages plus lookahead, rather than limiting
  raw SDK items early; the latter previously truncated a 266-message history at
  100 and incorrectly reported complete coverage.
- Cursors bind query/filter/source/profile generation and snapshot semantics.
  Do not silently switch to a different folder membership halfway through a
  paginated read. Inaccessible members and uncovered ranges are not empty chats.
- Folder definitions are not evaluated membership. Apply actual Telegram filter
  semantics, including explicit/pinned/excluded peers and archive/read/mute/type
  criteria. Distinguish shared folders. Verify SDK-dependent behavior against
  official Telegram/Telethon documentation and fake SDK responses.
- A megagroup is a group even though Telethon also calls it a channel. A media-only
  post can be a real post; a service event and an edit timestamp are not a new post.
- User inbox is Telegram unread state. Bot inbox is persisted `local_unprocessed`;
  bot history is only saved updates. Unsupported historical/topic reads must fail
  explicitly instead of pretending the Bot API provides user-account history.
- Existing StringSession storage is not a complete peer access-hash cache. Use
  the adapter's peer resolution path, not a second SDK client in a tool handler.
- Treat text, captions, extracted documents and sender names as untrusted data.
  Their instructions never change permissions, tool arguments or delivery plans.
- Extra evidence must preserve original IDs and distinguish account identity,
  author signatures and forwarded sources. Use optional fields for old stored
  records; migrate indexed columns deliberately. Do not infer unavailable counts.
- A Telegram read job still writes local state. Select truthful MCP annotations;
  do not mark a job-starting tool as side-effect-free solely because it sends no
  Telegram message. Auth and delivery policies remain governed by existing ADRs.

## When the feature is a durable job

Integrate start, status, control, worker dispatch and crash recovery, not just a
background coroutine. Bind the profile's account generation and freeze the input
snapshot. Persist results with the checkpoint atomically. Keep steps small enough
for pause/cancel and transport responsiveness. Bound retries and honor FloodWait
without dropping successful partial results. Isolate each chat's failure.

Test restart, pause/resume, cancellation, repeated invocation where applicable,
partial failures and no duplicate keys with a real temporary store and queue.
Safe read retries must never change the existing rule for uncertain deliveries:
`unknown` remains for review without automatic replay.

For attachments, use explicit message selection, private job-owned paths,
size/type/retention limits and safe file handling. Extraction must report the
method and actual availability. No automatic media archive, external AI upload,
model download or paid API request without the relevant owner authorization.

## Test entry points

- `tests/test_transport.py`: `running` and `client` exercise the real HTTP MCP
  session, lifecycle and temporary store. Reuse rather than bypassing transport.
- `tests/fakes.py`: fake external adapter plus `data` result helper.
- `tests/telegram_fakes.py`, `tests/media_fakes.py` and
  `tests/administration_fakes.py`: shared external SDK/byte fixtures, imported directly
  rather than through collected test modules. Administration `sdk` remains function scoped.
- `tests/test_media_security.py`, `tests/test_media_display.py` and
  `tests/test_media_delivery.py`: file roots/snapshots, inline images/downloads and
  confirmed native/Bot API delivery with immutable receipts.
- `tests/test_administration_read.py`, `tests/test_administration_management.py`:
  policy/cursor/privacy readers and confirmed account/group mutations.
  `docs/test-selectors.json` maps the old selectors, including SDK thread cases
  moved into the existing `tests/test_threads_v02.py`.
- `tests/test_folders.py` and `tests/test_telethon_history.py`: use fake SDK
  responses when adapter-only fakes would hide Telegram pagination or peer rules.
- `tests/test_reading.py`: query/cursor/profile/timeout behavior.
- `tests/test_jobs.py`: durable jobs and observable outcomes through public tools.
- `tests/test_evidence_reference.py`: owned frozen evidence references — exact
  originals after edit/delete drift and restart, reference vs pagination cursor,
  profile/generation/read-policy revocation, bounded lifetime and durable
  originals, and zero live history reread.
- `tests/test_output_budget.py`: public UTF-8 budgets for Unicode/rich/transcript content, metadata errors, original continuation, bounded direct freezes and current permissions/expiry.
- `tests/test_skill_workflows.py`: actual tool sequences and malicious content.
- `tests/test_stdio.py`: CLI/stdio sharing the existing daemon.
- `tests/test_projection.py`, `tests/test_projection_cli.py` and
  `tests/test_field_selection.py`: projection parity, explicit selection, content
  safeguards, external Jev fakes and cache/fallback through public tools.
- `tests/test_evaluation.py`: CLI evaluator/benchmark, replay regression and
  complete request/result cost with real temporary state.
- `tests/test_diagnostics.py`: local/MCP doctor modes, installed versus running
  build metadata and no diagnostic autostart.
- `scripts/smoke_projection_package.py`: installed-wheel CLI/stdio, discovery,
  both JSON representations, six bundled skills and delivery schema isolation.
  Run outside checkout imports in an isolated environment, as CI does.
- `tests/test_package_smoke.py`: checkout-import rejection before runtime startup.
- `scripts/check_build_metadata.py`: public `uv build` artifact verification,
  wheel/sdist version/provenance agreement and source-archive rebuilds without Git.
  Run `uv run python scripts/check_build_metadata.py` after `uv build`.

For new diagnostic or build tests, use `teleloom doctor --mode local|mcp` and
`server_status` as the observable seams. Verify wheel/sdist version and metadata
agreement, including rebuilding from an archive without `.git`; see
[ADR 0005](../../../docs/adr/0005-build-provenance.md). Local diagnostics leave
the daemon lifecycle unchanged; MCP mode inspects the running owner without autostart.
Default modes and legacy `--live` alone make no Telegram read or AI request. Explicit
profile/chat probes extend `server_status`; test normal reader exposure and current
ACL before external connection/read, exact saved peer identity, deadlines, safe
recovery and null uninstrumented RPC counts. The legacy doctor `--live` flag remains
an alias for MCP connectivity and the local profile snapshot, not a Telegram health claim.

Read the relevant fixtures; do not copy their private details into a second harness.
Feature checks should prove meaningful behavior, not mirror field assignments.

## Documentation and release

Update tool contracts in `docs/reference.md`, the affected runtime skill and
only the domain terms/ADRs that changed. Keep actual live evidence in
`docs/acceptance.md`; fake tests do not verify Codex or other clients live.
For reproducible selection and cost checks, use [the evaluator and benchmark
workflow](../../../docs/evaluation.md). Record the report's fake/replay/live mode;
JSON bytes are not tokens or a guarantee of any client's context savings.

For an authorized release, check the current version locations and repository
release conventions and update the lock file. Follow `docs/test-loop.md`: one
full suite before a substantial release unless the owner cancels it, one build,
artifact identity and selected-owner state preservation. Run installed behavioral
smoke for affected packaging contracts; CI and independent reviews apply when
requested. Verify published bytes before reporting release success. Publishing
is governed by the task's authorization, not by this skill.

See [test-loop.md](../../../docs/test-loop.md) for focused development loops and
source-bound release timing evidence.
