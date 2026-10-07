# Digest и масштаб: claims, revisions и bounded work

**Статус:** реализованный контракт первого публичного выпуска. Историческая постановка проблемы ниже объясняет решение; текущие параметры — в [reference](../reference.md), границы проверки — в [acceptance](../acceptance.md).

T16: реализован [offline caller workflow](../../skills/teleloom-digest/references/revisions.md)
для frozen export, revision manifest и bounded chunk/reduce с проверкой tracing и
original quotes через `teleloom digest-validate`. Human semantic quality — **NOT
MEASURED**. T17 добавляет caller workflow `refresh_revision`: actual current
access, anchored observed deltas и explicit bounded exact-key reconciliation.
Первый неподкреплённый delta или смена текущего journal epoch запрещают reuse до
перепроверки; full Telegram completeness остаётся unknown.

## Problem Statement

Повторные digests перечитывают период; неподкреплённые summaries теряют противоречия и устаревают. Whole job JSON/checkpoint copies и terminal decode требуют capacity proof.

## Solution

Caller-owned claim manifests поверх frozen evidence; incremental invalidation по observed revisions; explicit work deadlines; capacity gate перед expand–contract storage.

## User Stories

1. Как автор digest, я хочу claims/day/topic с источниками и contradictions.
2. Как reviewer, я хочу отличать original quote от paraphrase.
3. Как автор, я хочу hierarchical synthesis, сохраняющую retractions и gaps.
4. Как owner, я хочу отсутствие скрытого server AI upload.
5. Как автор повторного digest, я хочу инвалидировать claims при edit/delete/revocation.
6. Как author, я хочу явно historical-as-of или latest, а не их смешение.
7. Как operator, я хочу bounded reread при downtime gap.
8. Как owner, я хочу total work deadline, durable progress и terminal stopped reason.
9. Как operator, я хочу одинаковое deadline поведение после restart/FloodWait.
10. Как operator, я хочу понятное различие logical requests и реально измеренных RPCs.
11. Как operator, я хочу active-only queue work и preserved terminal history.
12. Как maintainer, я хочу evidence/SLO gate перед новой storage schema и migration.

## Implementation Decisions

- Caller делает summaries; manifest включает period/selection, source snapshot/revisions, claims/sources/contradictions, coverage и model/prompt provenance если AI применялся.
- Цитата проверяется по original, paraphrase помечается; semantic quality требует human fixture review.
- Latest reuse требует current access/generation/source validation. Caller-managed manifest не является server-enforced revocation; уже отданный model context не отзывной.
- Observed events дают delta, но не доказывают offline completeness. Gap требует bounded reread или stale/incomplete, не full-cache-as-truth.
- Не добавляется серверный LLM/cache pipeline. Owned derived retrieval скрывает весь mixed scope при revocation согласно ADR 0006.
- Reading jobs получают explicit work deadline с durable state и точкой подсчёта requests. Defaults совместимы; clock/FloodWait не обходятся; terminal budget stop не masquerade resume.
- Queue profiling/active metadata reuse исключает unnecessary terminal evidence decode, сохраняя jobs_status/results semantics.
- Capacity gate измеряет supported 1000/10000-message jobs, checkpoint/storage/snapshot costs и terminal backlog. Numeric target и bottleneck принимаются до migration.
- Storage conditional: expand compatible row form, resumable cohort migration, contract только после cursor/recovery compatibility window. Receipt/unknown-delivery state не мигрирует ради read speed.

## Testing Decisions

Observable digest fixture с decision/contradiction/retraction и colliding cross-chat IDs: traceable claims, source-version edit/delete/ACL invalidation, gap marking. Work fake-clock/FloodWait/restart checks через starts/status/results/control. Capacity benchmark — current supported limits, одинаковый trace boundary, no claimed token savings. Storage только после separate accepted gate, с old-package/old-cursor preservation checks.

## Out of Scope

Default AI summarization, embeddings, cross-tenant cache, automatic replay, concurrency framework, storage rewrite до profiling. Conditional T22–T24 не блокируют основной полезный release без принятого capacity gate.

## Further Notes

Contracts: [ADR 0010](../adr/0010-caller-owned-digest-claims.md), conditional [ADR 0011](../adr/0011-conditional-evidence-storage.md) and [capacity gate](../research/reading-capacity-gate.md).
