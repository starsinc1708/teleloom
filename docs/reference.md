# MCP and CLI reference

Current v0.5.0 public behavior. `tools/list` and typed schemas are authoritative;
the [roadmap](roadmap.md) separates proposals from implemented contracts.

- [Profiles/discovery](#profiles-and-discovery) and [read policy/exposure](#read-policy-and-tool-exposure)
- [Originals](#original-message-evidence), [search/context](#search-and-context), [envelopes](#response-envelopes)
- [Fields](#field-selection-and-projection), [folders/jobs](#folders-and-reading-jobs)
- [Confirmed operations](#confirmed-message-operations), [contacts/folders](#contacts-aliases-and-folder-management)
- [CLI/diagnostics](#cli-and-diagnostics), [events](#incoming-events), [transcription](#transcription)

## Profiles and discovery

`profiles_list` describes each configured backend, owner grants, selected read
policy, media roots and installed/configured transcription engines without
connecting to Telegram or probing OS credentials. Tool exposure appears alongside
profiles; `tools/list` is authoritative for the currently registered operations.
Supported stdio and Streamable HTTP use the initialization handshake; modern
`server/discover` and unsupported per-request protocol versions return explicit
errors with the supported path. See [client compatibility and recovery](clients.md#protocol-compatibility-and-recovery)
for revisions, exposure, reconnect and empty-catalog diagnosis.
For current account names/status, call `account_read` with
`operation={"kind":"me"}` for each returned exact profile. Premium and Telegram
rights remain server-enforced; inspect current self metadata before assuming them.

For dialog descriptions, page `chats_list` with the desired kind/unread/mute/archive
filters, then call `administration_read` with
`operation={"kind":"chat","chat_id":"<returned ID>","include_dialog":false}`
for the selected rows. This bounds description reads to the selected page.
Both bot backends use collected updates for context, topics, threads and pins;
MTProto bots additionally support explicit native message-ID lookup.

Typed group/account metadata and confirmed management are described in
[administration](administration.md), including account/backend restrictions.

## Original message evidence

Rich reads preserve exact original classic text/captions and UTF-16 entity offsets.
`text_source=original` identifies verbatim text. Block-only posts provide readable
`text` with `text_source=reconstructed`; `original_text` preserves the original
empty classic string, while `rich_text` contains safe native blocks, separately
labelled reconstruction, partial/RTL flags, media metadata and reconstruction
warnings. No cleaning is implicit. Original evidence survives projection and local
storage. Pages report partial or unsupported rich content as `incomplete`, with
`coverage.partial_rich_messages` and a warning. `custom_emojis` gives string document IDs and original alt spans without
fetching emoji documents. `reply_quote` retains selected text/entities, its UTF-16
offset and selected-quote flag; a manual flag is available for Bot API quotes,
and stays unknown for MTProto. Cross-chat targets use `reply_to_chat_id`. Safe inline
`buttons` expose text/type/URLs; callbacks require a separately confirmed mutation
and raw callback data is withheld. `web_preview` describes links separately from
actual attachments. Media includes observed type, MIME/size, duration/dimensions,
spoilers and safe poll/sticker metadata where the SDK supplies them.

## Search and context

`messages_search_global(profile_id, query, since?, until?, cursor?, limit=50,
max_requests=3, kind?)` uses Telegram's native candidate ordering for user accounts.
The default upper bound is frozen at the initial request time. Each call makes at
most 1–10 search batches; peer resolution may need additional bounded SDK reads.
Pages have 1–100 messages and the existing 32k text
budget. A page can be empty with a continuation after exact date filtering.
Cursors retain pending original evidence in private local state for 15 minutes,
survive owner restart and bind profile generation, read policy, query, dates, kind
and page/request budgets. Replaying a continuation returns the same frozen result.
Partial RPC failures preserve fetched evidence and report `coverage.partial_error`;
only an explicit continuation issues another read. The query/time bound is frozen;
Telegram may edit or remove future candidates during traversal. In selected read
mode, search queries only exact allowed peers, in chat/descending-ID order. It
never queries unrestricted global search and then discards forbidden hits. Bots
return `unsupported_capability`; their saved updates remain searchable per chat.
Provider exhaustion does not prove Telegram's index independently complete.
See [messages.searchGlobal](https://core.telegram.org/method/messages.searchGlobal).

`chats_search(profile_id, query, scope=dialogs|public, limit?, cursor?)` freezes
matching safe chat records for 15 minutes. Dialog matching uses observed title,
username or exact ID. Public search uses Telegram's
[contacts.search](https://core.telegram.org/method/contacts.search), retains only
returned matched peers and caps the provider request at 100 candidates; reaching
that cap is incomplete and requires a narrower query. A selected policy searches
only allowed exact peers. Public search is user-only; bot dialogs are saved updates.
Discovery never adds recipients to send/broadcast permissions.

`context_get(profile_id, chat_id, message_id, context_size=3, include_replies=true,
max_reply_chats=5)` reads the target and 0–20 nearest messages on each side, counting
available messages across deleted ID gaps. Results are ascending by ID, mark the
target and expose `has_older`/`has_newer` without claiming whole-history coverage.
`reply_context` batches exact reply IDs for at most 1–10 chats and shares the 32k
text budget; missing/inaccessible targets and response budgets remain explicit.
Cross-chat replies outside a selected policy are not fetched. Bot context means
saved updates only. These tools neither acknowledge Telegram reads nor send data
to AI. All three readers support normal response field projection.

`messages_search` additionally accepts `sender_id` as an exact canonical Telegram
identity, and permits an empty/omitted `query` when that identity is supplied.
It filters by account ID, never name, signature or forwarded author. User live
search filters the existing Telegram history/search batches; `max_requests=3`
(1–10) bounds each call to at most 100 candidates per batch. Limits and the 32k
text budget apply to matching messages, with buffered lookahead. An empty page
can carry a continuation; continue it to find quiet authors beyond newest-N.
Coverage includes `unknown_sender`, `scan_complete`, `partial_error` and the
frozen `effective_until`. Unknown senders cannot prove complete absence. Cursors
last 15 minutes and bind sender, query, dates, source, generation, policy and
budgets; replay preserves the previous page. Index and both bot backends search
saved rows only and report incomplete/stale coverage, without a Telegram read.
A group grant permits its author evidence, never the author's private dialog.
`messages_search_global(sender_id=...)` returns `unsupported_capability`; use one
explicit readable chat. `profiles_list.capabilities.exact_sender_search` exposes
these backend limits.

`messages_search_local(profile_id, chat_ids, query, since?, until?, limit=20,
cursor?, max_hits=200, snippet_characters=240)` is the opt-in multi-chat index
reader. Select 1–200 unique canonical readable chats explicitly. It uses the
existing SQLite FTS5 with literal AND of 1–32 whitespace-separated terms,
1–1024 query characters, and optional UTC start-inclusive/end-exclusive bounds.
MATCH operators and wildcards are quoted as literal input. It never resolves
peers, starts sync, reads Telegram history, creates embeddings or uploads to AI.
Both bot backends expose only saved observations. `profiles_list` advertises
`local_retrieval=selected_chat_fts`.

Hits sort by FTS5 BM25 ascending (lower is better), publication date descending,
numeric chat ID ascending, then numeric message ID descending. A first call
creates a completed local evidence job and freezes at most `max_hits` (1–1000)
and 1,000,000 original text characters. `stopped_reason=hit_budget` or
`character_budget` reports omitted matches; the Telegram/overall match total
stays unknown. Pages return at most 100 hits and 32k snippet characters.
Each hit preserves exact profile/chat/message/date/link, `source_message_version`
(a fingerprint of the indexed original record), `text_source`, rank and a
separate `snippet` (character cap 32–1000), `snippet_is_excerpt=true` and
`snippet_truncated`. A snippet is a search excerpt, never a verbatim original
claim. Reconstructed hits use a prefix of their currently policy-redacted view.

Retain `job_id`, `evidence_ref` and `source_version`; fetch the exact frozen
original with `jobs_results(profile_id, job_id, evidence_ref, message_keys)`.
Index edits/deletes or owner restart do not replace that original. The reference
uses the existing 30-minute pin, separately from the 15-minute pagination cursor;
expiry preserves the job's durable originals. Cursor pages bind selection, query,
period and budgets. Current read policy and account generation are checked on
both hits and original lookup; revoking any selected chat closes the mixed scope.
The tool creates local immutable evidence (`readOnlyHint=false`,
`openWorldHint=false`) and is available under read-only exposure. Reconnect clients
for discovery of this new tool.

`source=local_index` always carries conservative incomplete coverage, including
per-chat indexed count/date span, `index_status` (missing/partial/collected),
`sync`, `last_collection_at`, `freshness_unknown`, `stale_possible`, `known_gap`,
`requested_range_collected` and backend scope. Collected spans do not establish
current Telegram completeness, especially offline deletions. A missing index or
no hit never proves that Telegram has no matching message. Narrow the selection
or period if the freeze budget is reached; collection requires its own explicit
owner-authorized workflow.

## Response envelopes

All tools return `{ok, data, error}` in structuredContent and serialized JSON text.
Execution errors additionally set MCP isError. Error includes code, message,
retryable, retry_after and details. IDs are canonical decimal strings; dates require
timezones and are serialized in UTC. Message date ranges include start, exclude end.
History order is descending message ID, cursors bind profile/chat/query/range.
Telegram RPC date bounds have integer-second precision and search bounds are
strict. The adapter widens server bounds conservatively and applies exact
inclusive/exclusive comparisons locally, retaining messages at the start and
below a fractional end or comparison timestamp.
Live user history without a query uses Telegram history retrieval. A text query
uses Telegram search. Message and lookahead limits apply after date filtering,
so excluded boundary records cannot make a page falsely appear complete.
Live matching is delegated to [Telegram messages.search](https://core.telegram.org/method/messages.search);
the adapter does not impose a literal substring filter. Owner acceptance observed
inflected text and an edited message whose current text lacked the query stem.
The owner confirmed that the same hit appears in native Telegram search (#13);
the provider's internal matching reason is not established. Check returned text
before claiming a literal match. `incomplete=false` means this
response stream is exhausted, not that Telegram's search index is independently
proven complete or every current text contains the query.

Jev judgments retain the SDK's rubric and probabilities. For the classifier's
three-level Score questions, values range from 0 to 2, following the
[Score contract](https://docs.typesafe.ai/primitives/score); they are not percentages.
Noul values range from 0 to 1. Cached judgment usage describes the original API
response, not another charged request; `analysis_status=cached` identifies reuse.

| Tools | Use |
|---|---|
| server_status, profiles_list | owner/version, identities and capabilities; no secrets |
| folders_list | user folder names, explicit included/pinned/excluded IDs and dynamic rules |
| folder_members | evaluated user-folder membership with fixed snapshot, unavailable peers and optional kind filter |
| chats_list, chat_resolve | accessible dialogs / bot observed chats and exact identity resolution |
| chats_search | title/username discovery in dialogs, or capped Telegram public peer search (user only) |
| messages_get, messages_search | bounded live/index pages; message_ids expands reply context |
| messages_search_local | explicit selected-chat local FTS5 ranking/snippets with frozen original references and conservative index freshness |
| messages_search_global | bounded Telegram-wide user search or search of exact allowed peers, with UTC bounds and durable continuation buffers |
| context_get | central message, nearest messages on both sides and batched exact reply context |
| topics_list, topic_history, thread_get, comments_get | forum discovery and isolated topic/reply/discussion-root reads |
| messages_pinned | live user pins or pins observed in saved bot updates |
| inbox_get, inbox_ack | snapshot then explicitly acknowledge a known message checkpoint |
| digest_context | original evidence and coverage for agent-authored summaries |
| messages_classify | up to 20 explicit IDs, Jev opt-in/budget/cache/fallback |
| response_fields_select | static schema + owner task → optional field suggestion, deterministic by default, Jev only with explicit use_jev; `detail=compact` returns the same decision without the per-field reason map |
| sync_start | selected user chat, resumable durable job, default last 30 days |
| export_start | typed frozen evidence or legacy local index → private resumable JSONL/Markdown file |
| delivery_preview, delivery_execute | exact permitted plan → dialogue confirmation → hash-bound execution |
| message_operation_preview | typed message operation → immutable complete preview → existing delivery_execute |
| message_state | bounded drafts/schedules/buttons/send-as/inline results/per-reactor identities/read receipts in one exact chat |
| media_operation_preview | typed send_file/send_album/send_voice/send_sticker/send_gif/upload_file preview; existing delivery_execute confirms the exact bytes and target |
| media_info, media_download | one explicit observed attachment, bounded bytes and optional new owner-permitted destination |
| stickers_list, gifs_search | installed MTProto sets or named Bot API set; configured Telegram inline GIF provider with expiring send handles |
| photos_list, photo_open, photo_sheet | bounded avatar/message-photo references, inline rendered images and labelled thumbnail sheets |
| media_cleanup | delete only expired private media copies and handles for one profile |
| jobs_status, jobs_control | durable state; pause/resume/cancel before next step |
| activity_start | folder or explicit chats → durable latest-post comparison |
| unread_export_start | exact chats → fixed unread boundaries, resumable private JSONL/Markdown export without acknowledgment |
| messages_search_many_start, digest_context_many_start | bounded original evidence across a frozen selection and fixed UTC interval |
| jobs_results | frozen result pages, owned exact originals, or typed terminal observed aggregates, with full or opt-in compact coverage and partial errors |
| attachment_capabilities, attachments_read_start, attachments_cleanup | explicit selected files, local extraction and owned-file retention/cleanup |

`server_status` retains its existing version/owner fields and adds `build`:
`package_version`, `build_id`, `source_commit`, `build_type`. Build types are
`release`, `local`, `dev` or `unknown`; unavailable provenance is explicitly
`unknown`. The contract is read from the installed artifact, never Git in the
runtime working directory, and contains no credentials or private paths. See
[ADR 0005](adr/0005-build-provenance.md).

Without arguments, `server_status` inspects only the local owner. `health.local`
and `health.mcp` do not establish Telegram health; `health.telegram=not_checked`.
`scope` identifies the running owner's identities, connection snapshots, effective
chat grants and tool exposure. A cached `connected` adapter is not a fresh live
health check. Grants intersect the current read policy; exposure and backend
capabilities remain additional gates. `profiles_list` also exposes these `grants`.
Local doctor uses `scope.source=configured_local` and unknown connection state;
MCP status uses `running_owner`. Status never exposes file roots or model paths.

`server_status(profile_id="work", chat_id="100", timeout_seconds=5)` explicitly
opts into one selected user history probe. Both IDs are required. The profile,
canonical chat ID, read policy and `messages_get` exposure are checked before
connecting and again before reading. Bot profiles report `unsupported_capability`
because saved updates cannot establish live history health. The probe uses only
an exact peer already in native session metadata, with no dialog scan or username
resolution; an absent or mismatched peer reports `peer_unavailable`.

The deadline is the smaller of the requested 0.05–10 seconds and the owner's read
timeout, including connection/lock wait and read. There is at most one adapter
connection attempt and one `GetHistory(limit=1)` attempt, with no pagination or
automatic retry. The existing SDK uses `request_retries=0`; connection setup may
perform authorization/metadata RPCs. `logical_requests.connect/read` count
attempts, including failures before an RPC; `observed_rpc_requests=null` honestly
means the SDK's transport RPC count is not instrumented. These are not a measured
wire count or a count of unrelated owner background traffic.

`probe` reports only the selected IDs, status, source, returned count and separate
`timings_seconds.connect/read/local_processing`, never message text. Connect timing
includes owner adapter setup or reuse; read timing includes SDK decode/counting;
local processing builds the diagnostic result. Unattempted phases stay null.
`health.telegram=selected_read_ok` applies only to that bounded read, including an
empty successful response. A failure is still a successful status inspection:
inspect `probe.status=failed` and `probe.error` (`code`, `phase`, `next_action`),
not just the MCP envelope's `ok`. Timeout, revoked auth, ownership and reconnect
recovery use fixed safe actions; raw exceptions, RPC details and credentials are
withheld. Connection cleanup may outlive the response and retains ownership until
complete. Diagnostics never login, send, acknowledge, kill processes or replay
uncertain delivery. Copied results and model context remain outside server revocation.

## Field selection and projection

Message/evidence and chat readers accept optional `fields` (up to 64 unique flat
record names) or `preset`: `full`, `minimal`, `compact`, `digest`, `authors`,
`engagement`, `attachments`. Calls without a selection and `preset=full` retain
the previous complete JSON shape. Projection removes known optional record fields
from both structuredContent and text JSON after reading and coverage calculation;
original stored evidence remains complete. IDs, source links, original dates,
deleted/service markers and context IDs remain, as do every envelope error, cursor,
source, coverage/incomplete/warnings and snapshot/discussion field. Unknown response
fields are retained. Projected responses identify the applied selection in
`data.projection`; omitted metadata cannot establish zero counts or full coverage.

`response_fields_select(tool_name, request, fields?, preset?, use_jev=false, detail=full)`
returns a schema ID, selected/omitted/required fields, status and code-authored
reasons without reading Telegram. Apply its returned `fields` to the reader and
reuse them across cursor pages of the same tool/schema/intent; select again when
any of those changes. `detail=compact` returns the same fields, status, safeguards
and fallback reason in a smaller opt-in shape, derived after the full decision and
cache, without reclassifying content. Explicit fields or presets take precedence.
Optional Jev receives only a bounded owner task and static catalog, never retrieved
messages; disabled/unavailable or uncertain AI uses a deterministic fallback.
Recognized reading/summary intent protects text and requested URL entities from
inferred Jev omissions; explicit client fields/presets remain authoritative.
Selection never grants permissions or changes delivery. See
[field selection, presets and limits](response-fields.md).

Content readers (`messages_get`, `messages_search`, `messages_search_global`,
`context_get`, `inbox_get`, `digest_context`, `topic_history`, `thread_get`,
`comments_get`, `messages_pinned`, `messages_classify`, `jobs_results`) accept
opt-in `max_output_bytes` (1–2097152). It caps the **data** object serialized as
compact JSON with literal Unicode and UTF-8 encoding (`ensure_ascii=False`,
`separators=(",", ":")`), after current-policy redaction and explicit projection.
Identity, coverage, gaps, warnings, cursors, selected metadata, reference metadata
and `output_budget` itself all count. `output_budget.normalized_data_bytes` reports
that size. This is not a cap on the outer result, text/structured representations,
HTTP body, schemas or model tokens; those sizes must be measured separately.
Omitting the budget preserves existing responses and field/preset behavior.

Selected text, original text, rich blocks, transcripts, quotes, previews and
buttons share this budget. Shortened records have `excerpt.truncated=true`, the
changed `excerpt.fields`, `evidence_ref` and `source_version`. Content uses Unicode
string prefixes and list prefixes; source attribution and content warnings remain.
Entities/ranges associated with shortened content are omitted with
`excerpt.entities_notice`: Telegram spans use UTF-16, whereas the budget uses
UTF-8 bytes. Coverage describes the original read and is not rewritten to imply
less evidence was collected. If metadata plus the excerpt envelope cannot fit,
`output_budget_too_small` returns a normal error with no shortened success;
reduce records/optional fields or increase the budget.

A direct read freezes the returned originals only when its projected result needs
shortening: at most 100 unique message keys and 2 MiB of normalized original page
data, for 30 minutes, with at most 16 active direct freezes per profile.
`output_freeze_limit` requires a smaller page or reuse/expiry of an existing
reference. Failed budget calls do not consume this quota; active references are
not silently evicted. Direct freezes use private SQLite state, do not sync/index
history. `output_freeze_conflict` rejects multiple observed versions of one exact
key; narrow the page or omit reply context rather than substituting one original.
Direct freezes have no durable job original after expiry. Resolve them through
`jobs_results(profile_id, evidence_ref, message_keys)` **without job_id**.
Job references keep their `job_id`, durable originals and frozen scope. Budgeted
attachment/event/transcription pages also issue references, bounded by the earlier
of 30 minutes or their source retention expiry. Every view and continuation checks
current read scope, account generation, expiry and applicable job permissions.
Direct freezes containing cached STT also bind its source job's expiry and current
consent; discarding that source closes original retrieval through the direct reference.

For an oversized original, use `jobs_results` with one exact `message_keys` key
and `original_field=record|text|original_text|rich_text|transcript`.
`content.value` contains a chunk of the selected original's compact JSON;
concatenate chunks and JSON-decode once to reconstruct the exact normalized
record/field. Continue with `next_content_cursor` as `content_cursor`, retaining
the same reference, key and field; it is separate from a result-page `cursor`.
Each chunk has at most 32000 Unicode codepoints and can also use
`max_output_bytes` and projection. Offsets are explicitly Unicode codepoints,
not UTF-8 bytes or Telegram UTF-16 spans. Even a single content character must
fit alongside metadata, otherwise the call returns `output_budget_too_small`.
Resolution never refetches Telegram or replaces originals with the mutable index;
cached transcription enrichment cannot replace the frozen original. Private
dereferenced context still obeys current redaction policy, with visible notices.

The projection catalog also covers `message_state`, `media_info`,
`administration_read` and `account_read`: original message/draft evidence and
normalized profile/chat/member fields, including nested `latest_message`.
Immutable previews and image/control tools retain their fixed contracts.

Pages are bounded to 100 messages and a text budget. Bot history is local. Sources
are `telegram`, `local_index`, `bot_updates`; inbox sources are `telegram_unread`
and `local_unprocessed`. Coverage is conservative: local data may be incomplete or
stale. Deletions without an identifiable peer never delete guessed message IDs.
Acknowledging a user message affects Telegram read state through its ID; bot
acknowledgments are local. User `inbox_ack` requires `plan_id`, `plan_hash` and
`confirmed=true` from an exact matching `read_ack` operation preview; the old call
shape returns `confirmation_required` without changing Telegram. Partial inboxes
have no suggested ack_through value.
Bot acknowledgment requires the returned per-chat `snapshot_id`, valid for 15
minutes; edits arriving afterward remain unprocessed. Dialog pagination uses a
15-minute snapshot, so recency changes do not move chats between its pages.

## Folders and reading jobs

`folders_list` returns definitions. Use `folder_members(profile_id, folder_id,
kind?, limit?, cursor?)` or `chats_list(..., folder_id=..., kind=...)` to evaluate
them. Excluded peers take precedence; explicit/pinned peers override dynamic
type/read/mute/archive exclusions. Bots are a separate type from contacts;
megagroups are groups. Unread marks and mentions count as unread, and a muted
nonarchived dialog with unread mentions still matches. Shared filters use their
explicit/pinned membership, without inferred dynamic rules. Membership uses the
accessible dialogs including the archive; absent explicit peers and unknown rule
inputs appear in `unavailable` rather than being called empty. Folder IDs differ
from the archive's `folder_id=1`. Bots return `unsupported_capability`.
Pages retain a 15-minute fixed snapshot; cursors bind profile generation, folder
and kind. Definitions follow [Telegram's folder API](https://core.telegram.org/api/folders)
and evaluation follows [the official desktop client](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/data/data_chat_filters.cpp).

`chats_list` additionally accepts `unread_only`, `unmuted_only` and `archived`.
Filters apply after folder membership and bind the cursor's fixed snapshot.
Unknown mute/archive state does not match a requested value. For bots, unread
means locally pending incoming updates, without inferring Telegram unread state.

New evidence fields are optional: `sender_name`, `sender_username`,
`author_signature`, `forwarded_from`, `grouped_id`, `kind` (message/service),
`views`, `reactions`, `reply_count`, `pinned`, and `thread_root_id`. A publication
signature is separate from an account identity. Missing metadata stays null;
old SQLite JSON rows remain valid. Available SDK entities supply names without
per-message identity requests. Phone numbers, access hashes and credentials are
excluded. `topic_id` identifies forums; `thread_root_id` identifies any reply
thread. Bot evidence includes only fields present in observed updates.

`topics_list` returns a fixed snapshot, at most 1000 topics per discovery, with
explicit incomplete coverage if the budget is exhausted. Its optional `query`
matches topic titles case-insensitively across the observed pages and binds the
cursor; no match with incomplete coverage is not proof of absence. `topic_history` takes a
topic ID; `thread_get` takes `root_message_id`. `comments_get` takes the original
channel `message_id` and freezes the mapped discussion `chat_id`/`root_id` across
its cursor pages. It does not reuse a channel message ID as the discussion root.
Missing originals and absent comments are explicit. All message pages are bounded
to 100 items and 32000 text characters. Bots expose observed topic/thread/pin
updates with incomplete coverage and pins can remain stale after unseen unpin events.
They cannot discover historical channel comments; an observed automatic-forward
discussion root can be used only for saved replies. Manual forwards are not comment roots.
See [Telegram threads](https://core.telegram.org/api/threads),
[discussion mapping](https://core.telegram.org/api/discussion), and
[forums](https://core.telegram.org/api/forum).

Unavailable reply roots return `message_unavailable` or `topic_unavailable`;
inaccessible reply chats return `peer_unavailable`. Error details contain the
scoped `chat_id`, `root_message_id` and `original_status`. If replies were fetched
but the original lookup fails, the page retains those replies and its cursor,
reports the original status, and marks coverage incomplete with a warning.

Reading jobs accept exactly one of `folder_id` or unique `chat_ids` (1–200 chats).
`activity_start` optionally filters `kind` and returns `top` (1–100, default 5).
The latest genuine message includes media without text; service events are skipped
and edit times do not count as new activity. Results give message ID/date/link,
silence seconds and one frozen `comparison_at`; empty/unavailable chats are
separate. `max_requests` is 1–1000 (default 200). Work is sequential, deadline
bounded, checkpointed atomically, and uses at most three safe read retries with
FloodWait scheduling. Pause/cancel acts at step boundaries; queued reads recover
after daemon restart. Existing unknown delivery rules remain unchanged.
The request budget counts **logical batch attempts**, including failed attempts
and local bot batches; `coverage.requests` remains the compatible alias for
`coverage.logical_requests`. It is not a measured Telegram RPC count: connection,
peer lookup and SDK work can issue additional calls. `observed_rpc_requests=null`
means instrumentation is unavailable, rather than an invented zero or estimate.
Short or filtered SDK pages retain their raw continuation; they do not prove exhaustion.

All four reading starts accept optional `max_duration_seconds` (1-86400; omitted
means the existing request/message/character limits only). At durable job creation,
`started_at` and the absolute UTC `deadline_at` are frozen. Queue wait, pause,
downtime, reconnect/peer resolution, retries, FloodWait and local processing after
creation consume that same elapsed wall time. Selection/dialog preflight precedes
job creation and retains the existing bounded request timeout. Restart/resume never
moves the deadline; legacy persisted jobs without it keep their old limits.
The pending read timeout is the lesser of the configured per-read timeout and the
remaining total duration. Deadline checks run before another connection/read,
between accepted originals and export records, and before the next step, even if
FloodWait scheduled it later. Mandatory atomic checkpoint/finalization may finish
after the deadline; synchronous processing stops at the next record boundary.
Clock adjustments follow UTC wall time; reported elapsed time never decreases
below its last checkpoint. `coverage.elapsed_seconds` includes waiting/downtime
through that checkpoint, not CPU time or a continually ticking status field.
Expiry gives terminal `status=completed`, `coverage.budget_stop=true`,
`stopped_reason=total_deadline`, `incomplete=true`, and the retained originals and
per-chat gaps. It cannot be resumed; an explicit new job requires a new budget.

`messages_search_many_start` additionally requires a nonblank `query`;
`digest_context_many_start` collects histories for an agent-authored summary.
Both require timezone-aware `since` and `until` (start inclusive, end exclusive).
Limits: `max_messages` 1–10000 (default 1000), `max_characters` 1–1000000
(default 100000), and `max_requests` 1–1000 (default 200). These are shared
across all selected chats, not per-chat limits. `max_characters` counts collected
text characters; `max_output_bytes` separately limits a result page's UTF-8 bytes.
Original evidence is deduplicated
by profile/chat/message and retained with per-chat completion/error coverage.
Reaching a budget leaves explicit gaps. Bot jobs use saved updates and remain
incomplete. Jev is never enabled by these operations.

`unread_export_start(profile_id, chat_ids, format?, max_messages?, max_requests?,
max_bytes?)` freezes 1–200 exact selected chats and their unread bounds. User
exports collect incoming originals above the frozen read marker through the
frozen top message, before the fixed export time. Bot exports copy already
observed pending originals at start; later updates and local acknowledgments do
not change that material. The operation never acknowledges messages. Pause,
resume, cancel and restart use the existing reading queue and checkpoints.
`jobs_status` returns the private export path and per-chat coverage;
`jobs_results` pages the retained evidence. JSONL is the default; Markdown keeps
source identities and visible original text. Defaults are 1000 messages, 200
history requests and 20 MB output; maxima are 10000 messages, 10000 requests and
100 MB, with a fixed 1000000-character collection ceiling. Byte exhaustion,
unavailable chats and saved-update limitations remain explicit. Scope or account
changes prevent access to old artifacts; original indexed evidence is retained.

Use `jobs_status` for progress and `jobs_results(profile_id, job_id, limit?,
cursor?)` for source material. Each result cursor freezes the material, status
and coverage at its first read for 15 minutes, even while a job continues.
Start another first-page read to obtain newer results. Cursors cannot cross
jobs, profiles, generations or snapshots. Evidence/attachment status contains a
summary rather than all text. See [local attachment setup and limits](attachments.md).

Queue ticks select indexed active scheduling metadata, then load the current job
at its step boundary. During a tick, terminal evidence is loaded only for due
retention cleanup; requested terminal status/results remain available.
Listing `jobs_status` preserves ordered profile-scoped
summaries. The JSON job/snapshot storage stays compatible with older readers;
[the measured capacity gate](research/reading-capacity-gate.md) records its supported
workload and remaining checkpoint costs.

A frozen evidence or unread-export page also returns `evidence_ref`: an opaque
owned reference to that exact snapshot, separate from the pagination cursor, with
`source_version` (a stable identity of the frozen source revision) and
`reference_expires_at`. Passing both `cursor` and `evidence_ref` is rejected.
For the next page, keep `profile_id` and `job_id`, pass `next_cursor` as `cursor`,
and omit `evidence_ref` and `message_keys`. For exact originals, keep the job ID,
pass `evidence_ref` and `message_keys`, and omit `cursor`. A direct-page reference
omits `job_id`. `content_cursor` is separate and continues one original field,
not a result page.
The reference pins its snapshot for 30 minutes, longer than the cursor lifetime,
and stays bound to the owning profile, account generation and frozen chat
selection. Provide it with `message_keys` (1–100 exact `{chat_id, message_id}`
keys) to read those originals from the snapshot alone: the operation performs no
live Telegram reread and never substitutes the mutable local index for the
collected original, and any key outside the frozen selection fails with
`message_not_in_snapshot`. Without `message_keys` it returns the pinned snapshot's
coverage, scope and source version and no originals. Every resolve re-checks the
current read policy, account generation and mixed-scope revocation, so narrowing
the selection closes the whole scope: a reference is not a read grant and grants
nothing the profile could not already read. An expired reference is reported as
`reference_expired` and never deletes the job's durable originals; read a fresh
first page to obtain a new one. Replacing the account or changing the read policy
denies the old reference. By default only frozen reading-evidence jobs (`evidence`,
`unread_export`) expose originals this way; opt-in budgeted content references also
support attachment/event/transcription results within their retention and permissions.
Other job kinds are rejected. A job's current status alone does not gate its reference.

`jobs_results(..., coverage="compact")` opts into a coverage summary for frozen
`evidence`/`unread_export` message jobs, including local-search evidence. Full
coverage remains the default (`coverage="full"`). Other job kinds and direct-page
references reject compact coverage with `unsupported_job_results`: their items
may represent events, attachments or transcripts rather than collected messages.
The summary retains the source, requested period, frozen status, budgets/counters
(including unknown RPC counts), stop reason and all warnings. `coverage.scope`
contains the profile and frozen chat IDs; `coverage.counts` reports observed
messages, `total_messages=null` when collection completeness is unknown, completed
and unknown chat traversals, and unavailable/empty chat counts. These describe
collected scope, including saved-only bot observations, never global Telegram totals.
`gap_counts` groups unfinished chats by status/index status; stale/freshness/known-gap
chat counts and warnings remain visible. Per-chat coverage and the error,
unavailable and empty arrays move to details; message field selection stays separate.

Pass the returned `coverage.details` arguments to `jobs_results` for full details
from the same `evidence_ref` and `source_version`, with no originals or live refetch.
The explicit `view="coverage"` also freezes a reading job without returning message
bodies; it accepts no result cursor or message keys. Compact aggregates without a
reference pin that same revision automatically. Exact keys and original-content
chunks keep the same reference, policy/generation checks and 30-minute lifetime;
revoking any selected chat closes summary, details, aggregate and originals together.
Copied bytes and model context remain outside server revocation. Summary conversion
happens after coverage calculation and before field projection/content bounding;
`max_output_bytes` counts all summary metadata and warnings, and fails explicitly
if they cannot fit. Small scopes can cost more bytes due to reference/count metadata.

`jobs_results(..., view="aggregate", aggregate={...})` counts a terminal
reading-evidence or unread-export result (`completed`, `failed`, `cancelled`).
The default `view="messages"` keeps the existing page/reference behavior.
An aggregate has no message bodies or pagination; `items=[]`, `next_cursor=null`.
It accepts an existing `evidence_ref` only when that frozen snapshot is terminal,
with the same expiry, generation and read-policy checks. Cursor/message-key
combinations fail with `invalid_aggregate`; other job kinds fail with
`unsupported_job_results`, active snapshots with `aggregate_not_ready`.
The collector's existing identity checks exclude foreign profile/chat originals;
their `invalid_evidence` gaps remain visible in aggregate coverage.

The optional definition is allowlisted and rejects extra fields, including SQL:

```json
{"group_by":["chat","day"],"metrics":["count","incoming_count"],"timezone":"+04:00","top_k":10,"include_outgoing":true,"include_service":true}
```

`group_by` defaults to `[]` (totals only), accepts unique `chat`/`day` dimensions;
`metrics` defaults to both shown metrics and accepts either or both, uniquely.
`timezone` defaults to `UTC`; numeric offsets `±HH:MM` work without timezone data.
IANA names such as `Indian/Mauritius` and `America/New_York` use stdlib `ZoneInfo`;
the runtime dependency `tzdata` supplies its fallback when system timezone files
are absent, including Windows. IANA zones apply daylight-saving rules for each
publication date; fixed offsets do not. Invalid names/offsets return
`invalid_timezone`. Day buckets use original publication `date`, never edit time;
the collection's aware `[since,until)` interval remains unchanged.
Both inclusion flags default to true. `top_k` is optional, 1–1000; groups rank by
observed count descending, then numeric chat ID and day ascending. Truncation is
explicit in `groups_truncated` and never changes totals or collection coverage.

The response retains full stored coverage, errors, unavailable/empty scopes and
warnings. `source_identity` identifies profile/generation/job/kind; `selection`,
`period` (publication `[since,until)`) and source `query` describe the frozen input.
`source_version` matches the existing originals revision; `source_hash` is SHA-256
over that result, status, identity and frozen selection/period/query, independent
of the aggregate definition and stable across restart. These hashes are identities,
not new evidence handles. Aggregation changes no originals/checkpoint and issues
no history requests, synchronization or acknowledgments.

`aggregate.denominator="collected_evidence"`; `observed_count` and metric `count`
count collected records matching known filter facts. `total` describes that same
scope only when collection and filtering are complete; it is null for coverage
gaps or unknown filter membership and never asserts a global Telegram total.
A complete empty scope returns zero. Unknown direction makes `incoming_count`
null, while `known_incoming_count` and `unknown_incoming_count` remain explicit;
sender absence appears in `unknown_sender_count` without inferred authors.
Rows with undecidable outgoing/service exclusion are withheld from counts and
reported in `unknown_filter_count`, with `incomplete=true` and a warning.


To export that exact collected snapshot, call
`export_start(profile_id, source={kind:"frozen_evidence", job_id, evidence_ref},
format="jsonl"|"markdown")`. This source is exclusive with `chat_id`, `since`
and `until`; unknown source fields fail validation. It never widens selection,
requests sync permission, fetches history, acknowledges or sends. The legacy
`export_start(profile_id, chat_id, format?, since?, until?)` continues to export
the mutable local index with its existing incomplete-coverage warning.

Frozen JSONL begins with `{type:"manifest", manifest:{...}}`, followed by original
message records in snapshot order. Markdown stores the same records in JSON fences,
keeping exact text whitespace, entities and native blocks. `text_source` still
distinguishes reconstructed text from a verbatim original. The manifest includes
`source_revision`, `source_version`/`source_sha256` (the existing canonical frozen
source SHA-256, not the exported file hash), profile generation, fixed half-open
period, selection, query, source status and full coverage/errors/warnings. Export
completion does not make incomplete source coverage complete.

Use `jobs_status` for the completed private path, file byte count, SHA-256 and
manifest; no file content is returned inline. Verify the file SHA-256 before use.
Each step writes at most 100 records, usually at most 1 MB (one larger record may
occupy a step), with a 50 MB file and 500 MB private export-directory budget.
The job journals its snapshot lease, record offset, byte offset and prefix hash
in SQLite after flushing `.part`. `jobs_control` pause/resume survives restart;
uncheckpointed suffix bytes are truncated, and publication is an atomic rename.
Missing or modified checkpoint bytes fail without publishing a partial file.

The export lease ends at the source reference's expiry, at most 30 minutes after
the reference was created; starting or resuming never renews it. Current profile
generation, whole-scope read access and the export's exact read policy are checked
before/during export and every path access. Cancellation, expiry or revocation
durably denies the path and releases the journalled lease **before** attempting
physical removal, including active/paused/completed jobs. Restoring permissions
cannot resurrect revoked artifacts. Cleanup journals `status`, `remaining`
(`temporary`/`output`) and a fixed `export_cleanup_failed` error without private
paths. A busy file can remain on disk while every server resolve stays denied.
The denial's `error.details.cleanup` and profile job summaries expose this outcome.
Background cleanup selects at most 8 retained/pending exports per tick in rowid
round-robin order, with at most 2 unlink attempts per job; it survives restart and
continues unrelated queued work after removal failure.

Use `jobs_control(profile_id, job_id, action="cancel")` to remove a frozen export
or safely retry its cleanup, including failed/cancelled/completed exports and old
account generations. It returns the cleanup journal, never restarts export/history
or delivery, and retains a failed export's original error. Pause/resume retains
the usual terminal guard. This control accepts only an owned job ID, never a
filesystem path. Cleanup preserves durable source originals, reading checkpoints,
delivery plans/receipts and unknown outcomes. Obtain a fresh reference to export
again. Copies saved by the caller, already displayed text and issued model context
are outside the server's revocation boundary; server denial cannot erase them.

`teleloom digest-validate REVISION.json --export FROZEN.jsonl --sha256 FILE_SHA256`
is an offline caller workflow, separate from MCP tools. Pin the file hash from
the completed export's `jobs_status`; the revision repeats it without replacing
that independent check. The `teleloom-digest-revision-v1` manifest embeds the exact
frozen export manifest (scope, period, ref/version, generation and full coverage),
canonical claims with supporting/contradicting citations and retractions, and a
bounded chunk/reduce plan. Citation identities include profile/chat/message and
source version. Verbatim snippets must occur in original text; reconstructed text,
paraphrases and excerpts retain their distinct labels. Every exported record must
belong to exactly one chunk, and reductions retain whole canonical claims.

Success reports `validation=schema_tracing_quotes`, measured UTF-8 packet bounds,
`semantic_quality=NOT MEASURED` and `current_access=NOT CHECKED`. Invalid schema,
hash, tracing, coverage mismatch, quote or recipe bounds exit 1 with
`invalid_digest`. No owner configuration, credentials, Telegram or model is used.
Only `mode=historical_as_of` is supported; copied files/model context remain
outside server revocation, and current access/latest validity require the existing
server checks. External model use requires a separate caller upload policy,
budget and model/prompt attribution; the validator checks their recorded presence,
not approval or cost enforcement. See the [recipe, schema example and fictional
human-review fixture](../skills/teleloom-digest/references/revisions.md).

The caller Python workflow `teleloom.digest.refresh_revision(session, manifest,
export, sha256, checkpoint, *, event_job_id=None, reconcile_keys=None, scope=None)`
adds explicit incremental source validation without changing historical manifests.
It uses an existing connected MCP session: owned `jobs_results` reference checks
before/after processing establish `current_access=CHECKED_THIS_INVOCATION`; local
files or caller status strings alone always yield `NOT CHECKED` and no reuse.
Completed unfiltered event jobs must match the entire frozen scope. Their
`jobs_status.journal_epoch` reports the running owner's current observed-journal
epoch, while `payload.epoch` remains the frozen job's epoch. Bootstrap a completed
fresh delta and exact-key checks together; source-only checks leave continuity
unanchored. Missing anchor/current epoch, epoch mismatch or position mismatch makes
prior source checks unknown, including an empty first delta at sequence zero or a
completed old-epoch job retrieved after restart. Observed
edit/delete invalidates every supporting/contradicting citation and referencing
claim. Unchanged normalized originals can be reused through contiguous observed
progress; explicit `messages_get` exact-ID checks compare at most 20 keys per call
to this workflow. Missing/partial/non-Telegram originals remain unknown.

Checkpoint scope/period changes, known ACL/generation/expiry/exposure errors block
reuse until a new authorized revision starts with a separate checkpoint. Gaps
reset validated sources to unknown; explicit bounded reconciliation may recover
only checked unchanged keys. Coverage, unknown facts and gap history remain;
`telegram_completeness=UNKNOWN` and `latest_complete=false` never claim offline
Telegram completeness. Caller files/model context remain outside server revocation.

Commit report and checkpoint atomically in the caller journal. Exact revision hash,
processed job IDs and per-chat positions make replay deterministic without repeated
processing; an old replay alone does not establish current source freshness. Limits:
10 event pages/100 records per page, 20 exact keys, 33 MCP calls, 40 seconds/call,
120 seconds online, 10 MB checkpoint input and 1,000 deltas per revision. The phase
never starts a scan, acknowledges, sends or uploads. See the
[incremental recipe](../skills/teleloom-digest/references/revisions.md#incremental-source-checks).

Replacing an account creates a new profile generation. Old plans and queued work
cannot execute under it. The previous index and chat state remain in an archive
namespace in SQLite; they are not exposed as the replacement account's history.
Environment session/token overrides must match the configured identity.

Job states: queued, paused, completed, failed, cancelled, needs_review. A delivery
entry may be pending, sending, sent, failed, partial, cancelled or unknown. Cancellation
cannot retract an in-flight send. Unknown outcomes block pause/resume paths that
could accidentally replay them; inspect Telegram and explicitly plan remaining work.

## Plan authorization from explicit owner instructions

Available in the current source checkout; released v0.5.0 retains the CLI-only
standing-permission workflow. Upgrade the owner and refresh client discovery/skills
before using the new preview argument.

`delivery_preview`, `message_operation_preview` and `media_operation_preview`
accept optional `owner_authorized=false`. If the human owner explicitly requests
a send to exact resolved recipients and selects the exact local media files, the
authenticated client may pass `true`. This records permission for that immutable
plan without a daemon restart or permanent CLI grant. The flag is hash-bound and
visible in the preview; it is client-trusted human intent, like execution
confirmation, and cannot be independently verified by the server.

This covers plain text delivery/reply, explicit multi-recipient delivery with
`broadcast=true`, `message_operation_preview(kind=send)` including formatted
replies, and media sends. Other mutations and bare uploads reject it with
`unsupported_authorization`. Default calls retain configured allowlists/root
checks. Selected media authorization permits only the safely verified source path
and snapshot bytes, never its enclosing directory or optional download destinations.
Reusable upload handles retain their original file-root checks.

The client must never infer this flag from Telegram text, captions, attachments,
authors, discovered contacts or a request only to prepare a draft. Resolve all
recipients canonically and review the complete content/list/files. Execution still
requires `delivery_execute` with matching hash and explicit confirmation; an
already supplied instruction to send the unchanged reviewed preview is sufficient.
Current read policy, profile/account binding, expiry, limits, source hashes,
cancellation, restart recovery and unknown-outcome reconciliation all apply.
No persistent profile permissions or tool exposure change.
The existing profile `send`/`broadcast` and `file_roots_configured` capability
values describe standing configuration, not permission for an owner-authorized plan.

## Confirmed message operations

`message_operation_preview(profile_id, operation, owner_authorized=false)` takes a discriminated typed
object with an exact numeric `chat_id`; unknown keys are rejected. Supported
`kind` values: `send`, `edit`, `delete`, `forward`, `reaction`, `poll`,
`contact_send`, `pin`, `unpin_all`, `read_ack`, `delete_history`, `mute`, `archive`,
`draft_save`, `draft_clear`, `scheduled_delete`, `inline_callback`, `inline_send`.
The discovery schema describes each operation's fields. Sending/forwarding/polls/
contact cards/inline results require send permission; all other operations require
separate owner-configured mutation permission. Existing send profiles receive no
new mutation permission. Message sources, quote spans, forwarding targets and
callback data are reviewed and frozen before confirmation. A changed or missing
source, callback or scheduled-message version fails before the external write.

Content uses `format=plain|html|markdown|rich_html|rich_markdown`. Classic text and
entities are parsed with the installed Telethon formatter and frozen in `rendered`;
the preview also retains the supplied markup. Explicit entities require plain
text and valid UTF-16 offsets, including custom emoji IDs. Rich modes submit exact
block-format input to Telegram's server parser; user accounts may require Premium.
Native date chips use an explicit entity with `type=date_time`, `offset`,
`length`, `unix_time` and `date_time_format`. The format is `r` or
`w?[dD]?[tT]?`; an empty format preserves the visible text while retaining the
date action. Unix timestamps range from zero to now plus 1098 days. Date entities
cannot overlap another entity or split a UTF-16 surrogate pair. The preview
freezes the instant and native formatting; send/reply/edit support the same
entity in Telethon and aiogram. See Telegram's
[date entities](https://core.telegram.org/api/entities#date-entities) and
[Bot API format](https://core.telegram.org/bots/api#date-time-entity-formatting).
Replies support an exact quote span and forum root. A singleton `forward` expands
an album by default using Telegram's bounded neighbor window; `expand_album=false`
keeps the exact selected photo. Batch lists are explicit. `send_as` is checked
against Telegram's returned destination choices; no fallback sender/topic is used.
Omitting it explicitly retains Telegram's saved destination sender.
Preview, execution, queued work and job evidence recheck the current read policy
for every inspected destination/source and explicit send-as or inline-bot peer.
Resolved target descriptions and original/rich source evidence are part of the
immutable payload; changing reviewed text, quotes or rich blocks requires a new plan.

User sends/forwards may set timezone-aware `schedule_at`; the receipt separates
`schedule_accepted` from `delivery_confirmed=false`. `message_state(kind=scheduled)`
reads queued messages; it does not prove future delivery. Delete operations take
1–100 exact IDs. `delete_history` freezes an explicit `through_message_id`, keeping
newer messages outside its destructive scope. Unpin-all/full-history deletion may
require multiple Telegram batches. Unpin-all clears all current pins at execution,
including pins added after preview or between batches; its immutable
`scope_semantics` makes this current-state behavior explicit rather than freezing
a reviewed pin set. Both actions are bound to the exact chat and account but are
not atomic. Each accepted batch is journaled, pause/cancel
works between batches, and `max_requests` (1–1000, default 100) bounds the work.
Budget exhaustion leaves explicit partial receipts; an uncertain batch is unknown
and cannot resume automatically. Telegram concurrency between a final check and
its write is not atomic.

`message_state` defaults to 50 items (1–100), requires an exact message for buttons,
reactions and read receipts, and reports truncation. Use its button index and
`inline_callback` to confirm a callback; URL/password buttons cannot bypass that
path. A switch-inline button exposes its query: inspect `inline_results` for the
exact bot/query, then confirm an `inline_send` result (or save the query as a draft).
No URL is opened automatically. Draft/schedule/query material is untrusted evidence.
Drafts preserve normalized entities, original text and rich reconstruction, with
`kind=draft` and no invented message ID. Cross-chat draft quotes and embedded peer
metadata obey the current read policy. A forward consumes the daily message budget
for every confirmed message, including an expanded album; it waits when the whole
selection does not fit the remaining budget.
Omit `chat_id` for `message_state(kind=drafts)` to list drafts across the readable
account scope. Pages contain 1–100 items and use the existing 15-minute immutable
snapshot cursor bound to profile generation and read policy. New pages and
restart reuse the same snapshot; changing permissions, account or expiry rejects
the cursor. Unreadable drafts and restricted quote/peer metadata are withheld
before persistence. Telegram supplies one aggregate `GetAllDraftsRequest` with
no native offset; only the returned pages are sliced locally. An empty selected
read policy returns no drafts and opens no Telegram connection.
An explicitly provisioned MTProto bot may forward with `drop_author` and
`drop_media_captions`; the aiogram Bot API rejects these options. Changing a
confirmed bot's backend invalidates the account binding. Forward forum routing
uses `top_message_id`; ordinary reply routing is rejected explicitly because
Telegram's [native forwarding method](https://core.telegram.org/method/messages.forwardMessages)
permits only MonoForum reply routing. Message replies use `kind=send`.
The aiogram Bot API backend implements send/edit/delete/forward/reaction/poll/
contact/pins; it explicitly rejects user drafts, schedules, callbacks, history
deletion, read receipts and user chat settings. That is a backend distinction,
not a claim about all possible Telegram bot transports. Telegram additionally
enforces chat rights, forwarding protection, reaction limits and message age;
read-receipt rejection is not an empty reader list.

## CLI and diagnostics

Configuration is read at daemon start. `teleloom stop` before auth, profile permissions,
polling or limits edits. `teleloom call TOOL --args-file FILE` uses the same MCP owner.
`teleloom serve` runs foreground, `teleloom mcp` auto-starts a detached local owner.
`teleloom call TOOL --field text --field sender_name` or `--preset digest` forwards
the selection through that same MCP session. Duplicate JSON/flag selection keys
and combining fields with a preset are errors. `teleloom fields TOOL --request TEXT
[--use-jev]` prints a suggestion without running a read.

`teleloom doctor --mode local` is the default, machine-readable local installation
check. It reports package provenance, explicit daemon `not_checked` status,
safe-check results, optional engine availability and recovery actions without
starting the daemon.
`teleloom doctor --mode mcp` inspects an already running owner via safe MCP status
and discovery, adding the discovered tools and key parameters such as
`fields`/`preset`. It does not autostart an owner. Compare the local and running
build information after an upgrade. `--live` remains a compatibility alias for
MCP connectivity and its legacy local profile snapshot only. Default local/MCP
diagnosis makes no Telegram read or AI call. `teleloom doctor --profile work --chat
100 [--timeout-seconds 5]` explicitly selects MCP mode and the bounded probe above;
it still requires an already running owner. MCP inspection has a 20-second deadline
and a 1-second local health request; Telegram cleanup may continue under the owner.
The JSON contains `build`, `package` (version/origin consistency), `daemon`,
`tools` (source/count/schemas), `scope`, `health`, `failure`, `optional_engines`,
`checks` and `recovery_actions`, plus `probe` when explicitly requested. Owner/MCP
failures identify their phase and safe next action. Hidden readers yield a null
projection check instead of a false package failure; hidden writes are valid exposure.
Local discovery uses an isolated temporary runtime, with
`tools.source=installed_package`; MCP discovery uses `running_mcp`. A running
owner's contract is under `daemon.build`. Engine availability reports local
installation checks; transcription model availability remains `not_checked`.

`teleloom evaluate` exercises `response_fields_select` against a versioned RU/EN
static corpus, with raw scores reported separately from final selection.
`teleloom benchmark` accounts for selection and read requests/results, both JSON
representations and external-call counts on fictional 1/20/100-message pages.
Both default to offline replay/fake through MCP and real temporary local state.
An explicit `--live --max-requests N --max-characters N --timeout-seconds N`
opts into bounded Jev calls with static tasks/schemas only. Reports identify
their mode and available usage/latency; JSON bytes are not tokens. See
[evaluation commands and report contracts](evaluation.md).

Read operations have a 30-second default deadline and return retryable
`read_timeout` errors when Telegram stalls. `read_timeout_seconds` in local
`config.json` controls this budget (up to 40 seconds); edit it with the daemon
stopped. The stdio bridge uses an additional 15-second transport budget and opens
a fresh upstream session per call, so its next call can recover after daemon
shutdown. Transport errors return `daemon_timeout` or `daemon_unavailable`.
Interrupted calls are never automatically replayed, including delivery and inbox
acknowledgment; inspect job status before retrying a delivery.
Initial connection stalls return retryable `connection_timeout`. Interrupted
connection startup keeps the profile owned until teardown completes
(`profile_closing`); failed teardown blocks reuse until daemon restart
(`cleanup_failed`). Optional classification preserves fetched evidence if the
remaining analysis budget expires.

Environment overrides:
- TELELOOM_DATA_DIR: application state location.
- TELELOOM_MCP_TOKEN: local bearer token instead of OS keyring.
- TELELOOM_PROFILE_API_ID / API_HASH / SESSION / BOT_TOKEN / PROXY: replace PROFILE
  with the uppercase profile name (letters/digits/underscore).
- TYPESAFE_API_KEY (or TELELOOM_TYPESAFE_API_KEY): Jev key.

Profiles use lowercase names starting with a letter, up to 48 characters. Proxy
URIs are socks5://, socks4:// or http:// with host/port; store credentials privately.
No automatic .env loading. New user sessions persist in a secure OS keyring.

Jev produces independent relevance Score, urgency Score, topic Choice and
actionability Noul per message. The whole bounded state is shared within a request.
Cached results include actual model and usage; rubric/content/model changes invalidate
the key and a 24-hour TTL bounds aliases such as jev-latest. Application budgets
limit requests and input characters, not provider billing guarantees.

## Incoming events

Incoming events use `events_wait_start(profile_id, chat_ids, mode="new"|"settled",
timeout_seconds=30, debounce_seconds=1, max_events=100, after_sequence=null,
retention_hours=24, filter=null, cursor=null)` and the existing `jobs_status`, `jobs_results`, `jobs_control`.
The owner must first allow each exact chat with `teleloom profile allow PROFILE CHAT
--scope event`. Read policy still applies. Bot feeds also require explicit polling;
existing webhooks are never removed. The daemon collects no new feed by default.
The journal retains at most 1000 events per profile, for `event_retention_hours`
(1..168, default 24). Waits select incoming new/edit events and exact-chat delete
notices, with canonical source identities, a monotonic `sequence` and untrusted
text. The journal uses local receipt time for debounce. Delete notices provide no
original sender, direction or date: their date is local receipt time and message
defaults are not evidence about the deleted source. New/edit events omit outgoing
messages; deletion updates without an exact chat are omitted.
`filter` is a typed object with optional exact canonical `sender_id`, positive
`topic_id`, positive `mention_user_id`, `kinds` (`new`, `edit`, `delete`), and
UTC-aware `observed_since`/`observed_until`. Criteria combine with AND; kinds
combine with OR. IDs are facts, not names/signatures; a sender filter in a group
never opens the author's private chat. The interval includes its start and excludes
its end using local receipt time. Each returned event has `observed_at`;
`publication_at` and `edited_at` describe the source, not when it was observed.
Delete `publication_at` is null and `unknown_facts` identifies unavailable source
facts; its legacy `date` remains a synthetic local timestamp.

Mention selection matches only explicit Telegram text-mention entities carrying
the selected user ID (MTProto or Bot API). An unresolved username mention is
unknown; bare text/name matching never proves identity. Missing sender/topic facts
(including an unobserved General-topic classification) and delete mention facts
are unknown. A known mismatch excludes the event. Otherwise an unknown criterion
skips it, increments `coverage.skipped_unknown` and per-criterion `unknown_facts`,
and sets `incomplete=true`. `skipped_nonmatching` counts known exclusions;
`inspected` counts consumed journal records, not all Telegram updates. A filtered
empty page with unknown facts cannot prove absence. Unknowns are consumed: changing
filters requires a fresh replay/reconciliation, not reuse of that cursor.

The scan covers the retained journal (at most 1000 records), applying the event
budget to matches rather than raw candidates. Settled debounce considers matching
events per chat; unrelated events cannot keep that selected stream active.
`coverage.next_cursor` is the opaque continuation for `events_wait_start`, distinct
from the top-level `jobs_results.next_cursor` used to page one batch. Consume every
batch page before advancing. The event cursor preserves per-chat positions and
binds exact chats/filter/profile identity/generation/current read policy. Reuse
requires the same selection/filter and omitting `after_sequence`; budgets and wait
mode may change. It survives owner restart and explicitly reports that restart as
a gap. It expires with its originating wait's result retention (1..168 hours).
Current read policy, event opt-in, bot polling and workflow exposure are checked
on start, worker steps and result reads; the cursor grants no permission.

The default starts after the current journal position;
explicit `after_sequence` replays retained events. SDK duplicates are coalesced.
Settled waits apply the quiet interval independently to each chat, so an active
chat does not delay a quiet one. `coverage.deferred_chat_ids` identifies active
chats left pending, with `incomplete=true`; `next_sequence_by_chat` supplies
positions for subsequent waits on individual chats. The shared `next_sequence`
never skips deferred events and can replay already returned events across chats;
hosts can deduplicate by sequence. A timeout
returns retained partial events with explicit coverage. Paused waits keep their
original deadline. Restart/eviction gaps remain explicit, never complete offline
coverage. Cancel/expiry remove job results and frozen result cursors.

Explicitly provisioned MTProto bots with polling enabled collect readable native
updates into the existing local inbox and pending state, even without sync grants.
The event journal commits before inbox originals and their local watermark; a
replayed SDK update is coalesced. Reviewed local acknowledgments preserve edits
received after the snapshot and make no Telegram read acknowledgment. Generation
replacement archives the prior inbox; read revocation and disabled polling stop
collection. Bot history coverage remains saved updates, not complete history.

Hosts poll these local jobs and continue with `coverage.next_cursor` (or the legacy
scalar `coverage.next_sequence` for explicit replay). Re-reading
results is safe; it neither acknowledges Telegram nor delivers to arbitrary
callbacks. UI notifications, agent wakeups and any host routing require host
integration and independent owner authorization. There is no unsolicited webhook
or outbound host callback. MCP job-start annotations report local side effects.

For an executable host-independent pull sequence and the runtime watch/reply
workflow, see the [bounded event recipe](../skills/teleloom-inbox/references/events.md).
It bounds polls/events/pages/context, preserves per-chat sequence dedupe and
unresolved gaps, and keeps reply preview/confirmation separate. Foreground MCP
supports local pull; future wakeups/notifications need verified host capabilities
and a separate owner-authorized host configuration. Installing skills or opening
stdio does not establish those capabilities. The recipe's Python block is exercised
by `tests/test_event_workflows.py` with fake SDK ingress and no delivery execution.

## Transcription

`transcription_capabilities(profile_id)` describes user/bot/provider restrictions.
`transcription_start(profile_id, chat_id, message_id, provider="local"|"telegram"|
"openai"|"groq", allow_external_upload=false, max_calls=1, max_bytes=10000000,
max_characters=32000, timeout_seconds=30, retention_hours=24)` selects one exact
audio/video message; consume the job through the same jobs workflow. The owner
allows transcription with `--scope transcription`. Configure models/endpoints and
daily external call/byte limits through `teleloom profile transcription PROFILE`.
Persisted jobs recheck tool exposure before work and resume. Read-only exposure
pauses external uploads; selected exposure requires `transcription_start`.
Account generation and model/endpoint revision are checked again after awaited
source reads/downloads, before an external effect.
Local Whisper requires a complete existing absolute model directory and the
transcription extra. It runs in a persistent isolated process, reuses the model,
and kills that process on timeout/cancel before restarting for the next request.
Other document/OCR extractors keep their disposable process semantics.

Telegram uses the user-only `messages.transcribeAudio` request and matching
`updateTranscribedAudio` updates. Premium, weekly trial count/duration and group
boost restrictions are server-enforced; trial counters and known rejections are
reported. Bots can use explicitly enabled local/external providers for saved
attachments, with saved-update coverage. There is no fallback provider or model
download. [Telegram API contract](https://core.telegram.org/api/transcribe).

OpenAI-compatible and Groq providers upload AUDIO only. They require a separate
owner `--scope transcription_external` chat grant, call consent
`allow_external_upload=true`, positive daily call/byte budgets and credentials
`TELELOOM_PROFILE_OPENAI_TRANSCRIPTION_KEY` / `TELELOOM_PROFILE_GROQ_TRANSCRIPTION_KEY`
(or the same OS keyring names). Endpoints/models are owner configuration, never
MCP-supplied credentials. HTTPS or explicit loopback HTTP is supported; redirects
and automatic retries are disabled. Uploads are limited to 25 MB and one request.
The durable receipt reserves calls/bytes before uploading; timeout, cancellation,
disconnect or crash can leave `unknown`/`needs_review`. Such uploads still count
against budgets, and repeating the same selection returns that receipt without
replay. Daily call/byte limits do not guarantee a provider's monetary bill.
Read-only exposure permits local/Telegram jobs and refuses external uploads.
[OpenAI endpoint](https://developers.openai.com/api/docs/guides/speech-to-text),
[Groq endpoint/models](https://console.groq.com/docs/speech-to-text).

Completed transcripts remain separate, clearly untrusted `transcript` enrichment
on ordinary full reads; original text/evidence is untouched. Cache identity binds
profile generation, message key/date/edit/media version, engine/version and model
revision/endpoint, with retention bounded to 1..168 hours. Concurrent identical
starts coalesce into one job. Reads only retrieve matching cache entries; they
never transcribe or upload automatically. Revocation and account replacement deny
old cache/results/cursors. `max_calls=0` permits cache hits and rejects misses.
## Read policy and tool exposure

Existing profiles default to `read_mode=all`. To restrict conversations while
the owner is stopped, use `teleloom profile read-policy PROFILE --mode selected`
and `teleloom profile allow PROFILE CHAT_ID --scope read`. An empty selected list
denies all conversations. This does not enable sending or AI analysis. History,
search, folders, inbox, threads, index and reading jobs share this policy;
revoked jobs remain stored but their evidence cannot be retrieved until allowed.

`teleloom config exposure --mode read-only` exposes readers and bounded local read
workflows. `--mode selected --tool messages_get --tool profiles_list` exposes
exactly those named tools, including dispatch. Unknown names fail owner startup.
Restart the owner and reconnect clients after changing either configuration.

One Telegram authentication session can have one connection owner across local
data directories. Entity/update metadata survives restart in private SQLite;
authentication keys remain in OS credentials or explicit environment overrides.


## Contacts, aliases and folder management

`contacts_list` reads safe address-book records, IDs (`view=ids`) or export rows
(`view=export`). `contacts_search` and `contacts_direct` expose bounded matches;
`contact_chats` reads one exact user and common chats, and `contact_interactions`
returns original incoming/outgoing message evidence. `contacts_blocked` reads
blocked peers. Pages use fixed profile/account/read-policy-bound snapshots,
1-100 returned rows, explicit source and incomplete flags. Search and common/
blocked source scans are bounded; incomplete state cannot approve a mutation.
Stored SDK phone numbers and access hashes never enter these results.

`contact_aliases_list`, `contact_alias_set` and `contact_alias_delete` maintain
local profile/account-isolated aliases. Resolution is exact and case-insensitive;
fuzzy suggestions never select a target. Replacing another target requires
`replace=true`. Aliases grant no read, send or management permission.

While the daemon is stopped, `teleloom profile management PROFILE contacts --enable`
or `folders --enable` grants only that named management scope; `--disable` revokes
it. Contacts and folder SDK operations are user-only under Telegram's official
method restrictions. Local aliases also work for bot profiles. Other management
scope names are `groups` and `account`; each is independent of read/send/AI grants.

`contacts_preview` accepts discriminated operations `contacts_add` (exact user ID,
@username or explicit owner-provided E.164 phone), `contacts_delete` (1-100 exact
user IDs), `contacts_block`, `contacts_unblock`, and `contacts_import` (1-100 unique
owner-provided phones). Only that explicit sensitive input appears in its immutable
approval artifact. Import uses one bounded native request and returns per-input
position/status receipts; retry-required/unimported rows are terminal partial
results for inspection. Sending a contact card uses `message_operation_preview`
with `kind=contact_send` and the destination's send permission.

`folders_snapshot` returns system/private/shared definitions, complete explicit
included/pinned/excluded peer lists, title text and UTF-16 entities, all private
rules, emoticon, color, title animation flag, shared invite flag, order and opaque
revisions. Read policy may hide peers and mark the snapshot incomplete. This is
a definition snapshot; use `folder_members` for evaluated rule membership.
`folder_limits` reads the current account tier and positive limits from Telegram
app config. Unavailable server limits remain unknown rather than invented.

`folder_preview` accepts `folder_create`, `folder_update` (complete private-folder
replacement), `folder_delete`, `folder_reorder` (every custom private/shared ID
exactly once), `folder_add` and `folder_remove` (private/shared explicit membership).
System IDs 0/1 cannot be mutated. Titles fit 12 UTF-16 units; entities fit complete
characters. Membership edits preserve all other metadata. Removing an explicit
peer can still leave it included by a dynamic rule. Existing as well as proposed
peers must satisfy read policy, including replacement/delete previews.

Both preview families bind the exact account generation, scope, actual peer-read
IDs, state revision and full operation payload to the common confirmation hash.
`delivery_execute` requires the exact plan/hash and explicit confirmation; the
existing delivery journal tracks acknowledged, partial, failed and unknown outcomes.
Worker execution rechecks scope/read policy/revision before journaling and again
immediately before its native mutation. Telegram offers no atomic folder compare-
and-swap: the last local revision check cannot exclude a simultaneous remote edit.
Uncertain outcomes remain available for reconciliation across restart without
automatic replay. These workflows are covered offline with real MCP and SDK-shaped
responses; live Telegram/client acceptance is separate.
