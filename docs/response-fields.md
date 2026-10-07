# Select response fields

There are three independent ways to reduce a read; do not confuse them:

1. **Owner tool exposure** (an owner CLI configuration) decides which tools exist
   in discovery and dispatch at all. It is not a per-call choice and does not
   select record fields.
2. **Direct `fields`/`preset`** on a supported reader is the client's authoritative
   per-call choice; known fields need no suggestion call.
3. **Inferred selection** via `response_fields_select` turns an owner task into a
   field suggestion. It only suggests; the client still forwards the returned
   `fields` to the reader. There is no automatic per-call selection and no new
   dispatcher.

Use a preset or explicit fields when a reading task needs fewer record fields.
For example, `preset="digest"` retains original text and source context while
omitting engagement counts. Projection reduces both MCP structuredContent and its
text JSON. It runs after the normal bounded read and coverage calculation; stored
messages and frozen job evidence stay complete.

Calls without `fields` or `preset` return the complete previous JSON shape.
`preset="full"` also preserves that shape exactly. Projected responses add
`data.projection` with the applied `fields` and `preset`.

Supported readers are `messages_get`, `messages_search`, `messages_search_global`,
`context_get`, `chats_search`, `digest_context`,
`topic_history`, `thread_get`, `comments_get`, `messages_pinned`,
`messages_classify`, `inbox_get`, `jobs_results`, `chats_list`, `folder_members`
and `chat_resolve`, plus `message_state`, `media_info`, `administration_read` and
`account_read`. These family readers project recognized original message/draft
records (including a single `item` or nested `latest_message`) and the normalized
profile/chat/member fields in their static catalog. Drafts retain their original
chat/date/kind without inventing a sent message ID. Metadata records retain exact
identity, membership role and `untrusted`; unknown integrity fields remain visible.
Structural `latest_message` and member `user` containers remain so selecting their
message/profile fields cannot silently remove the containing evidence.
Use `response_fields_select` to inspect the supported fields for each reader.
For durable evidence and attachment jobs, apply the selection
to `jobs_results`; job starts, status/control and delivery tools do not project.

Supply exactly one of:

- `fields`: up to 64 unique flat record names, for example `["text", "sender_name"]`.
  An empty list keeps the required source/integrity fields alone.
- `preset`: one of the following names. Each preset is intersected with the tool's
  catalog and always includes its required fields.

| Preset | Optional record fields retained when available |
| --- | --- |
| `full` | Every field; preserves complete-response compatibility |
| `minimal` | None |
| `compact` | Text, sender name, attachment metadata, unread count, archive state |
| `digest` | Text, sender name, author signature, attachment metadata, edit date, unread count |
| `authors` | Text, sender name/username, author signature, forwarded-source metadata |
| `engagement` | Text, views, reactions, reply count, pinned state |
| `attachments` | Text, attachment metadata, downloaded bytes, extracted character count |

Unknown fields, duplicate names, an unknown preset or both alternatives produce an
error before reading. Names describe whole record fields; dotted paths and nested
entity filtering are not supported. Unknown fields in returned records remain
visible for forward compatibility.

Original message keys (`profile_id`, `chat_id`, `id`), `date`, `link`, `kind`,
`deleted`, `sender_id`, reply/topic/thread/album IDs, `text_source` and
`original_text` cannot be removed. Reconstructed text stays explicitly identified.
Chat records
keep `id`, `title`, `kind` and `username`. Attachment records keep their original
message identity, source, extraction method, chunk indices, truncation/untrusted
markers and retention deadline. Activity comparisons keep the latest post's
identity/date/link and silence value. Null values remain honest unknowns.

All envelopes remain: errors, `next_cursor`, `source`, `coverage`, `incomplete`,
warnings, snapshots, requested periods and discussion mapping. Continue cursors and
report gaps using those fields. An omitted reaction count does not mean zero
reactions; a shorter JSON response does not establish complete coverage. Projection
does not widen a read scope, acknowledge messages or authorize delivery.

## Suggest fields from the owner task

`response_fields_select(tool_name, request, fields?, preset?, use_jev=false, detail=full)`
uses an authoritative static catalog, without reading Telegram. With the default
`detail=full` it returns:

- `tool_name` and `schema_id`, identifying the applicable record catalog.
- `fields`, `omitted` and `required`, showing the decision and protected fields.
- `status`, a summary `explanation` and code-authored per-field `reasons`.

Forward the returned `fields` into the supported reader to apply the suggestion.
Reuse them across cursor pages of the same tool/schema/task; select again when
the tool, the record schema or the normalized intent changes. Cached judgments are
bound to that tool/schema/intent identity. Known fields or a preset need no
inferred-selection call.
The selection is not a plan token, an authorization or generated summary. Explicit
fields/presets win even if `use_jev=true` is also supplied.

`detail=compact` returns the same decision in a smaller opt-in shape, derived after
the full selection and cache lookup. It never reclassifies content, changes the
cache key, spends budget or calls an external service. It keeps `tool_name`,
`schema_id`, the effective `fields`, `status`, a short `explanation`, the applied
content `safeguards` list and a concrete `fallback_reason` (null when the status is
not a fallback). It drops the per-field `reasons` and `omitted` map. `full` remains
the default and the cache always stores the full decision, so a later `detail=full`
call reads the same full result.

By default, deterministic multilingual task matching selects an appropriate preset
and reports `disabled`. For a digest, use its text and sources to write the summary
yourself. A missing or ambiguous task match keeps a deterministic fallback rather
than requiring AI availability.
The fallback recognizes common English/Russian requests for summaries, including
"саммари" and "суммаризация", reaction/view/reply counts and exclusions such as
"without reactions, views or comments" or "реакции, просмотры и число комментариев
не нужны". This is bounded keyword matching; explicit fields/presets
provide exact control when a task's phrasing is outside those rules.

On a content-bearing reader, inferred selection conservatively retains `text`
by default, even if Jev confidently judges it unnecessary. When that task asks
for links, it also retains `entities`, since a URL can be hidden behind a text
label. It also retains `rich_text` for URLs inside native rich blocks. This
code-owned content safeguard applies to suggestions, including
fallback and cached suggestions; it never overrides explicit client fields or
presets. A simple standalone metadata-only request such as "only reaction counts"
may omit text. Mixed, negated or ambiguous tasks keep content. An explicit
minimal selection may omit text. Unknown reading phrasings keep content
conservatively; use explicit fields for exact requirements.

Jev requires explicit `use_jev=true`. It receives only the owner task (at most 2000
characters) and static field descriptions. Do not paste Telegram texts or extracted
attachments into that task; field selection never fetches or forwards result data.
One batched Noul request names each optional field, repeats its static definition,
references its schema path and supplies yes/no criteria about task usefulness.
It evaluates optional fields with zero SDK retries and a deadline of at most five
seconds (or the shorter configured read deadline). The
existing owner credentials, model, daily call limit and per-request character
limit apply; no new chat-content permission is granted or changed.
The [async SDK client](https://docs.typesafe.ai/sdk/python/api/clients/async)
supports the explicit `RetryPolicy(max_retries=0)` used here. At most 64 optional
field questions are allowed, and the encoded schema/task/questions must fit the
owner's `jev_max_characters` limit.

A [Noul](https://docs.typesafe.ai/primitives/noul) is a yes/no probability, with
no separate confidence value. This application retains optional fields at values
of at least 0.7 and omits them at values of at most 0.3, subject to the content
safeguard. Between those thresholds, it preserves the deterministic fallback's
decision. Overriding a Jev omission reports `uncertain` with a code-authored reason.
These are code-owned display rules, not factual guarantees or calibrated measures
of a field's importance.

Statuses are `explicit` (fields or a preset), `disabled`, `unavailable`,
`budget_exceeded`, `evaluated`, `uncertain` and `cached`. `unavailable` covers
missing credentials/SDK, service failure, invalid output or timeout. Uncertain
judgments retain deterministic fallback fields. Reasons are authored by code,
not generated prose or a factual guarantee about the messages.

The cache holds at most 128 entries for 24 hours. Its key hashes the tool, the
record schema, normalized intent, model and selection-policy version, so reuse is
limited to the same tool/schema/intent. Entries do not store raw tasks, Telegram
messages, profile IDs or secrets. Cached reuse does not incur another API request;
selection budgets are shared with the existing owner AI budget. Policy version v4
invalidates entries cached before tool-bound reuse and compact detail.

## CLI

The CLI uses the same MCP owner and validation as a client:

```text
teleloom fields digest_context --request "Сделай дайджест за 48 часов"
teleloom fields messages_get --request "Сделай дайджест" --detail compact
teleloom fields messages_get --request "Show authors and forwarded sources" --use-jev
teleloom fields messages_get --request "Read selected messages" --field text --field sender_name
teleloom call messages_get --args '{"profile_id":"personal","chat_id":"100"}' --preset digest
teleloom call messages_get --args-file read.json --field text --field sender_name
```

`--field` is repeatable. `--field`/`--preset` may also be expressed in the JSON
arguments; providing the same selection key in JSON and as a flag is an error,
rather than a silent override. Fields and presets cannot be combined. Existing
`--args` and `--args-file` behavior is retained. The `fields` command prints a
suggestion; it does not automatically run a read.

Reconnect MCP clients after an upgrade to refresh cached discovery. See
[acceptance](acceptance.md) for source-bound live observations; fake-API tests
do not establish general live model quality.
