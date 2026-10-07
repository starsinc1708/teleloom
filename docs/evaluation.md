# Field-selection evaluator and cost benchmark

Run these manual checks from an installed package or development checkout:

```console
teleloom evaluate
teleloom benchmark
```

For terminal frozen-evidence aggregates, run the public MCP byte regression:

```console
uv run pytest -n 0 tests/test_evidence_aggregate.py -s
```

Its fictional 1000-message/35-bucket fixture reuses this evaluator's complete
request/`CallToolResult` byte measurement, including structuredContent and text
JSON. It compares every chat/day count and incoming count with enumerated
originals, preserving full coverage, source revision and checkpoints; aggregate
calls add no history requests. The aggregate response and request-plus-response
must each stay within 10% of enumeration for this fixture. This byte target is
fixture-specific, not a token or client-context guarantee.

`teleloom evaluate` and `teleloom benchmark` print a versioned JSON report and exit nonzero for unmet
expectations or exhausted budgets. Tests cover their offline mode; extended
hosted CI is manual under the [check policy](agents/implementation-standards.md).
They create and remove their own temporary runtime, SQLite store and queues,
exercise the real HTTP MCP client/server, and restore process-local SDK/clock
substitutions on every exit. They do not load owner configuration, use the owner
daemon, access the credential vault, or change environment variables. Benchmark
Telegram reads use a fictional external adapter in every mode.

## Corpus and evaluator

The static corpus lives in `src/teleloom/evaluation.py`, with version
`response-fields-2026-10-05-v1`. Change the version when changing tasks,
expected necessary/excluded fields or recorded scores. Its twelve RU/EN cases
cover summaries with links, omitted reactions/views, history, counters only,
mixed/negative wording, explicit fields/presets, and malformed external answers.
No case contains fetched Telegram content, credentials or profile data.

Each task calls public MCP `response_fields_select`. Its report keeps model
scores separate from the actual policy result: `raw_missing`, `raw_excluded`
and `raw_uncertain` describe raw judgments; `final_fields`, `safeguards`,
`fallback`/`fallback_fields` and `violations` describe the resulting behavior.
It also records schema hash, policy version, expected fields, model/source,
representation agreement, external calls and available API usage. Per case it
separately reports `round_trips`, `schema_bytes` (the discovery `inputSchema` of
the selector and reader), `selection_overhead_bytes` (the selector round trip)
and the structuredContent/text sizes.

The original `text=0.29` defect is an honest recorded replay. The complete
scores come from the existing regression in `tests/test_field_selection.py`;
the historical model `jev-1.13.0`, usage and service latency are documented in
[acceptance](acceptance.md#историческая-проверка-field-selection). Historical measurements appear separately under
`recorded_measurements`. Current replay `usage` is null and current latency
measures this local run. Other canned scores/model labels are synthetic and
each case explicitly says `mode: fake`. A successful replay demonstrates policy
behavior for those fixtures, not current or general live model quality.

Explicit selections stay authoritative. The explicit counter fields and
minimal preset cases deliberately omit text/entities even when the task asks
for a summary; their expected fields reflect the client's explicit choice.

## Total-cost benchmark

Fictional pages of 1, 20 and 100 records are read through public MCP
`messages_get`. A 101st record makes every measured page include a real,
nonempty cursor. Each size compares a full response, direct `digest` preset,
uncached Jev selection followed by the read, and cached selection followed by
the read. Different static requests keep the uncached cases independent.

Each row records every measured selection/read MCP request and complete
`CallToolResult` response. `structured_content_bytes` and `text_json_bytes`
show the two representations separately; `response_bytes` includes both and
the MCP result envelope. `mcp_json_bytes` sums application requests/results.
`round_trips` counts MCP application calls, `schema_bytes` is the discovery
`inputSchema` size for the tools a row calls, and `selection_overhead_bytes`
is the selector round trip (zero when a row makes no selection call).
`external_json_bytes` separately counts serialized schema-only SDK requests
and responses; `total_json_bytes` adds those two channels. `external_metrics`
keeps their request/response sizes, service-boundary latency and available usage.
Fictional Telegram call counts are separate from Jev calls.

Two extra sections cover the reuse and compact contracts without new external
calls. `select_once` calls the selector once, reuses the returned fields across a
ten-page read, records its `selector_calls`, `round_trips` and coverage, and
compares every projected page against a projection-free read of the same cursor
range. `compact_selection` renders one disabled fixture with `detail=full` and
`detail=compact`, confirms identical effective fields/schema/status and reports the
byte reduction plus separate structured/text sizes.

These are compact UTF-8 JSON measurements, excluding HTTP headers, JSON-RPC
IDs, initialization/discovery and SDK wire framing. They are not token counts;
no tokenizer is used. Clients differ in how they consume structured and text
JSON, so byte savings do not establish identical client-context savings.
Local MCP latency is actually measured. Offline service behavior is simulated;
offline usage is null rather than invented. Replay timings are not live
Telegram or Jev latency estimates.

`savings_bytes` and `savings_percent` include selection overhead and may be
negative. The single-record uncached case exposes a loss. Cached rows describe
a warm cache; `cache_warmup` identifies the separately measured selection that
populated it, and `cold_start_total_json_bytes`/`cold_start_savings_bytes` include
that one-time cost. The full read used as an integrity baseline is its own row
and is not an extra charge assigned to each projected strategy.

Integrity checks compare original text, hidden URL entities, source identities,
date/links/structural markers, nonempty cursor, coverage and envelope fields.
Evidence hashes summarize fictional identity/content integrity. Direct
`digest` intentionally omits URL entities because presets remain authoritative;
that row shows `integrity.entities: false`, `expected_integrity.entities: false`
and an explicit limitation. Inferred Jev rows must preserve all checks. Report
`ok` means observed behavior matched each explicit expectation, including the
documented preset limitation; it does not mean that preset suits link summaries.

## Explicit live opt-in

Only `--live` permits external Jev calls. Supply an existing
`TYPESAFE_API_KEY` environment value; these commands never retrieve or save
vault credentials. For example:

```console
teleloom evaluate --live --max-requests 12 --max-characters 200000 --timeout-seconds 30
teleloom benchmark --live --max-requests 3 --max-characters 100000 --timeout-seconds 30
```

The evaluator defaults to 16 external requests and the benchmark to 6. Both
default to 200000 static-input characters and a 30-second overall deadline;
CLI hard caps are 32 requests, 200000 characters and 60 seconds. Characters
include serialized task, static schema/questions and model, cumulatively across
attempted external calls. Requests are counted before awaiting an API result.
The existing selection deadline and no-retry policy also apply. Budget exhaustion
returns `stopped` and a nonzero exit; fallback and any completed rows remain visible.
Every command uses a fresh temporary selection cache and daily counter.

Live evaluator uses the same static tasks and collects actual available usage,
model and latency through the SDK boundary. Live benchmark sends only tasks and
static schemas to Jev; fictional messages stay local. No live Telegram read,
send or acknowledgment is part of either command. Development tests exercise
`--live` only with a fake external SDK; no paid call was made to implement or
verify these commands.
