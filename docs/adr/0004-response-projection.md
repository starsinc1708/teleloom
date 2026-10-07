# ADR 0004: schema-bound response field projection

Accepted scope: issue #28; current public contract is [response fields](../response-fields.md).
Owners need less JSON for focused tasks, such as
a 48-hour digest that does not need reaction counts. Optional selection must not
change the evidence used to calculate coverage or become an authorization path.

Projection therefore runs after bounded reading, pagination and coverage
calculation. The same projected result generates MCP structuredContent and text
JSON, so the wire payload is reduced in both representations. Original SQLite
records and frozen job evidence remain intact. Calls without a selection, and
`preset=full`, retain the previous complete shape without projection metadata.

Code owns the field catalog, required fields and traversal of supported response
records. Only known optional record fields may be removed. Original IDs, source
links, publication dates, deleted/service markers and context references remain.
Envelope errors, cursors, source and coverage metadata remain verbatim. Unknown
response fields remain visible so a future integrity field cannot be accidentally
hidden by an older catalog. Unknown requested fields fail before reading.

The client may supply fields or a preset directly. A separate
`response_fields_select` tool suggests fields from the owner task and catalog; it
does not read Telegram or create a delivery plan. The client applies suggestions
by forwarding `fields` to a supported reader. There is no implicit selection on
existing calls and no probability that controls a recipient, permission or send.

The opt-in output byte budget runs after projection. It shortens selected content
into marked excerpts while preserving the selected fields, identities and coverage;
it is a separate presentation operation, not an inferred field selection. The
normalized compact UTF-8 data, including its reference/excerpt metadata, must fit
or the call fails explicitly. Frozen originals remain retrievable under ADR 0008.
Calls without a byte budget retain the projection contract above.

Jev is an explicit optional display aid. Its state contains only a bounded owner
task and static field descriptions, never fetched messages or projected results.
Code asks independent Noul questions in one bounded request, authors the returned
reasons and retains deterministic fallback fields for uncertain judgments. Missing
credentials, unavailable service, invalid output, timeout or exhausted budget
produce a truthful fallback status. Explicit fields and presets take precedence.

An owner-authorized live demonstration found a confident false omission of `text`
for a Russian summary request. Inferred suggestions on content-bearing readers
therefore conservatively protect `text` regardless of optional Jev judgments;
requested links also protect `entities` for hidden URLs. An overridden omission
has an `uncertain` status and a code-authored safeguard reason. Explicit client
fields and presets still win. Simple standalone metadata-only requests can omit
text; mixed, negated or ambiguous tasks and unknown reading phrasings keep
content. Questions name the field and its definition instead
of relying on question IDs or an array index alone. Policy v3 invalidated judgments
cached before the content safeguard; policy v4 later bound cache identity to the
tool. These rules do not establish live model quality or full coverage.

The existing owner stores credentials, request budgets and a bounded 24-hour cache.
Cache identity includes the tool, schema, normalized intent, model and selection
policy; raw tasks, Telegram data, profile IDs and secrets are not stored in cache
entries. Reuse is therefore limited to the same tool/schema/intent and a different
tool or schema requires a new selection. Policy v4 invalidates entries cached
before tool-bound reuse.

The selector's opt-in `detail=compact` returns the same decision without the
per-field reason map. It is derived after the full selection and cache lookup, so it
does not reclassify content, change the cache key, spend budget or call an external
service; a later `detail=full` read returns the same cached full judgment. Selection
does not grant chat-content AI permission or change delivery or inbox acknowledgment
semantics. A smaller response cannot prove fuller coverage.
