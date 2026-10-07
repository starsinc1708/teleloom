---
status: proposed
activation: capacity-gate-required
---
# ADR 0011: conditional immutable evidence rows через expand–contract

Whole job JSON может иметь дорогие checkpoint/snapshot copies; это ещё не основание
мигрировать storage. Если capacity gate выявит bottleneck при agreed supported load
и зафиксированном target, предпочтение — owned immutable evidence rows с ordinal/revision
и атомарным checkpoint. Mutable message index не заменяет frozen originals.

До gate сохраняем текущий JSON1/paging. После принятия решения: compatible expand,
resumable cohort migration, затем contract только после сохранения old cursors,
originals, release/recovery compatibility и завершения compatibility window.
Не затрагивать durable receipts/unknown outcomes, не восстанавливать старую SQLite.
Dual-write/retention costs измеряются; ошибки migration сохраняют читаемый old form.

Альтернатива немедленной rewrite отклонена: высокая цена restart/ACL/provenance regressions.
Условные T22–T24 требуют отдельного принятия после T19 и не входят в baseline release
до такого решения. Контракт: [digest and scale](../specs/digest-and-scale.md).

Capacity gate (2026-10-07): [fixed workload and measured evidence](../research/reading-capacity-gate.md).
For 1000/10000-original jobs and up to 100 terminal 1000-original archives, indexed
active scheduling removes repeated archive decoding and satisfies the predeclared
local tick, MCP response and Python memory targets. Whole-job checkpoints remain
measured overhead, but do not breach this gate. Keep this ADR conditional; conditional storage tasks
require a separately accepted breached target under a concrete larger workload.

2026-10-07: conditional storage tasks closed as **not planned** after verifying that gate evidence
and the current active-queue public regression. [Reactivation conditions](../roadmap.md#conditional-storage).
This ADR stays proposed/conditional; no new storage, migration or cutover is accepted.
