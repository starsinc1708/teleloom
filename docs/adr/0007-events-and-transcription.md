# ADR 0007: scoped incoming journal and explicit transcription

Incoming events reuse the owner's SDK ingress and a bounded local SQLite journal,
enabled only for exact owner-selected readable chats. Jobs freeze chat selection,
sequence, account generation, deadline and retention. Consumers pull results;
the daemon performs no arbitrary host callback or Telegram acknowledgement.
Restart/retention gaps are evidence gaps, not empty complete periods.
Quiet intervals are per chat. A shared continuation cannot skip active chats;
per-chat positions allow independent continuation without replaying quiet peers.
Typed journal selection uses exact observed sender/topic/kind/mention facts and
local receipt-time intervals. Unknown metadata is consumed with explicit skipped
coverage, never guessed into a match or a complete absence. The match budget is
applied after scanning the retained journal. Durable opaque continuation binds
profile identity/generation, exact chats, filter and read policy, retains per-chat
positions and preserves the prior owner epoch so restart is visible as a gap.

Native bot updates reuse the existing inbox/pending tables and local watermark,
with the opt-in event committed before originals and watermark advancement.
Local reviewed acknowledgments apply to both bot backends; native bots never
send user-account read acknowledgments.

Transcription is explicit work on one selected source. Ordinary reads only enrich
from matching completed cache; originals remain untouched. Provider, model revision,
source edit/media version and generation bind cache identity. The existing owned
attachment root and durable queue provide file, timeout, cancellation and retention
limits. Whisper reuses a complete existing model in a persistent subprocess; killing
the process on timeout/cancel retains the isolation guarantee of ADR 0003.

Owner exact-chat consent and a call upload flag are both required for external AUDIO
uploads. No message/caption text is submitted. Budgets and a sending receipt are
committed before the HTTP request. Unknown uploads remain visible and cannot replay,
including after cancellation/restart. HTTP redirects and paid automatic retries are
disabled; provider billing remains outside a call/byte budget guarantee.

Telegram's user-only transcription also exposes Premium/trial restrictions and uses
matching raw transcription updates rather than creating a second SDK connection.
No provider fallback or model download is implicit.
