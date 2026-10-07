# ADR 0006: profile read policy, exposure and session metadata

Legacy profiles retain explicit `read_mode=all`. Owners can select a per-profile
conversation allowlist through the stopped-owner CLI. This grants neither send,
sync nor AI permission. Live readers, indexes, job creation/workers/results and
cached cursors check the same policy. Revoking any conversation in a frozen
reading job denies that job's evidence as a whole; originals remain stored so
restoring permission can recover them. A group conversation's authors are part
of that conversation's evidence; they do not become allowed private chats.

Selected reads withhold dereferenced cross-chat reply quotes, forwarded source
metadata and embedded peer metadata outside the current policy. Presentation
applies this before field projection, including job results and JSONL exports;
stored originals remain intact. Coverage and warnings identify withheld context.
External Bot API replies retain their external-source marker and exact known
message identity. An unavailable source chat cannot prove read permission, so
selected presentation withholds its quote and context never guesses a local ID.
Export artifacts bind the read policy used to create their bytes. A changed
policy denies the old path and requires a new export from the preserved index.

Tool exposure is owner-wide: all, read-only or selected. Excluded tools are not
registered, so discovery and dispatch agree. Read-only allows bounded reading
jobs and their local control/cleanup but excludes external writes and AI uploads.
Changing configuration requires owner restart and client reconnect; there is no
MCP policy escalation tool.
An explicitly selected diagnostic read also requires `messages_get` exposure and
the same current read policy before connecting and before reading. It never uses
status exposure to bypass a hidden reader or expands exact saved peer resolution
into dialog discovery. Safe status grants describe these gates without private
file/model paths; copied artifacts and model context remain outside server revocation.
An exposure change also pauses pending external delivery and denies resume while
delivery execution is excluded. Local pause/cancel and receipt inspection remain
available; restoring exposure permits an explicit resume of known pending work.

Data-directory ownership alone cannot prevent the same OS-stored Telegram session
being used by two installations. A separate OS-released per-auth-key lock in the
user's application-data directory spans data directories. The lock name contains
only a digest. A failed disconnect retains ownership until cleanup/process exit.

Native StringSession keeps the authentication key in memory/OS credentials, but
does not persist entity/update state. We extend that memory session using native
entity resolution and update-state methods, saving only numeric peer/access-hash
and update-counter metadata in private local SQLite, scoped to generation and
credential digest. It contains no auth key, usernames, phone numbers or messages
and is never an MCP result. This trades extra local metadata for reliable ID
resolution and restart update cursors without a plaintext `.session` database.
Metadata is capped at 10000 peer and update identities; evicted peers require
Telegram resolution again. Offline deletion coverage remains explicitly unknown.

Sources: [Telethon session storage](https://docs.telethon.dev/en/stable/concepts/sessions.html)
and [native session methods](https://docs.telethon.dev/en/stable/modules/sessions.html).
