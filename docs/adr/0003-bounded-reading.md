# ADR 0003: bounded reading evidence and local attachments

Accepted bounded-reading contract; included in the first public release.
The daemon remains the only runtime
connection owner. New reads neither acknowledge messages nor authorize delivery.

Folder definitions cannot prove membership. Evaluate the accessible dialog state,
including archives, once and preserve explicit unknown/unavailable members. Bind
pagination to a 15-minute snapshot and profile generation. Read jobs copy that
selection into their persisted record so later folder edits cannot change scope.

Activity and multi-chat collection use the existing durable queue. A small
sequential read step persists originals and progress together; retries consume
the request budget and honor FloodWait. Errors stay local to a chat. Optional total
duration freezes an absolute UTC
deadline at job creation; wait, pause, downtime, reconnect and local processing
consume it. Atomic finalization preserves covered originals after expiry; exhausted
jobs are terminal. Batch attempts are logical work, with uninstrumented SDK RPC
counts left unknown. See [the public contract](../reference.md#folders-and-reading-jobs). One
comparison timestamp and original post dates avoid counting edits as activity.
Evidence cursors freeze material and coverage even when a job is still running.
No new AI permission is inferred; summaries are written by the calling agent.

Selected attachment jobs reuse the same adapter and queue. Generated private
paths ignore remote filenames. Disposable local extraction processes provide a
timeout boundary for document, OCR and transcription engines. Optional engines
report installation requirements; transcription accepts only complete existing
local models. No automatic model download or external AI upload occurs.
Retention and explicit cleanup remove both owned files and extracted text.
Bot coverage always distinguishes saved updates from Telegram user history.
