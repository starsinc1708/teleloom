# Domain glossary

- **Profile**: an owner-named Telegram identity, either user account or bot.
- **Read policy**: owner-selected conversations a profile may expose to agents;
  allowing a group does not authorize reading its authors' private conversations.
- **Tool exposure**: the owner-selected public operations discoverable and callable
  by an agent.
- **Chat**: a dialog, group, channel, or private conversation visible to a profile.
- **Folder**: a user account's Telegram dialog filter, with explicit included,
  pinned and excluded peers and optional dynamic membership rules.
- **Message key**: profile ID, chat ID, and message ID; message IDs alone are not global.
- **Original text**: the verbatim Telegram text or caption, before reconstruction or cleaning.
- **Reconstructed text**: a readable rendering derived from original rich blocks,
  distinct from a verbatim source quote.
- **Inbox snapshot**: Telegram unread state for a user, local unprocessed updates for a bot.
- **Checkpoint**: durable progress committed with the messages it covers.
- **Delivery plan**: immutable preview of the identity, content, and exact recipients.
- **Plan authorization**: trusted-client recording of an explicit human instruction
  permitting only the previewed send and selected files; permanent grants stay unchanged.
- **Job**: a persisted synchronization, export, or delivery operation.
- **Unknown delivery**: a send may have succeeded; automatic replay is unsafe.
- **Confirmed operation**: an exact external action described by an immutable
  owner-reviewed plan, including its target, content and account identity.
- **Accepted schedule**: Telegram has stored a scheduled message; its future
  delivery has not yet been established.
- **Draft**: a profile's pending composition in one chat, without a sent message ID.
  Account-wide draft pages freeze only readable compositions under the current
  profile generation and read policy.
- **Date chip**: a native Telegram entity over a UTF-16 text span, with a frozen
  Unix instant and explicit localized date/time format.
- **File root**: an absolute directory the owner explicitly permits for media
  source reads and optional destination writes; links and traversal do not expand it.
- **File snapshot**: immutable private bytes, SHA-256, size and source identity
  captured before a media preview, bound to a profile generation and expiry.
- **Media handle**: an opaque, expiring profile-bound reference to a completed
  Telegram upload or provider GIF result; possessing one does not authorize delivery.
- **AI permission**: owner-controlled permission to submit a chat's content to Jev.
- **Folder snapshot**: evaluated, profile-generation-bound membership frozen for
  pagination or a read job, including unavailable peers and rule-state gaps.
- **Reading evidence job**: resumable local operation over selected chats with
  immutable original message evidence, a fixed period and explicit coverage budgets.
- **Discussion root**: the message in a linked discussion chat corresponding to a
  channel post; its ID and chat may differ from the original publication.
- **Attachment read**: explicitly selected message files downloaded into a private
  job directory and extracted locally within byte, text, time and retention limits.
- **Response projection**: removal of known optional record fields from a tool's
  returned JSON after reading and coverage calculation; stored evidence is unchanged.
- **Field catalog**: code-defined names, descriptions and required source identities
  for a supported tool's response records, identified by a stable schema hash.
- **Field selection**: explicit fields, a named preset or an intent-based suggestion
  from the catalog; it changes presentation, never permissions or completeness.
- **Content safeguard**: code-owned retention of text and requested URL entities
  by default in inferred suggestions on content-bearing readers; clearly metadata
  tasks may omit text, and explicit client selections remain authoritative.
- **Evidence snapshot**: a frozen selection of original evidence and coverage at
  one source revision; later edits do not silently replace its originals.
- **Evidence reference**: an opaque owner/profile-bound reference to an evidence
  snapshot or its exact original records; it is not a read permission.
- **Observed aggregate**: a count or grouping over collected evidence with its
  declared scope and coverage; it need not equal a complete Telegram total.
- **Excerpt**: shortened content derived from an original, with its source
  reference and an explicit truncation indication.
- **Digest claim**: a statement backed by source evidence, with quoted text
  distinguished from paraphrase and contrary evidence retained.
- **Digest revision manifest**: a caller-owned record linking digest claims to
  source versions, selection, period and coverage for validation and reuse.
