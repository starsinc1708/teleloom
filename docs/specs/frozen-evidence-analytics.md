# Frozen evidence: aggregates, references и export

**Статус:** реализованный контракт первого публичного выпуска. Историческая постановка проблемы ниже объясняет решение; текущие параметры — в [reference](../reference.md), границы проверки — в [acceptance](../acceptance.md).
с offline public acceptance. Logical export denial/pin release сохраняются до
filesystem IO; busy-file cleanup journalled, bounded и retryable без replay.
Native OS lock/live acceptance не выполнялись. Реализованный контракт — в [reference](../reference.md).
Общие invariants и release policy — в [implementation standards](../agents/implementation-standards.md).

## Problem Statement

Модель получает весь массив originals для простых счётчиков; coverage повторяется на страницах, точечный live context может уже отличаться от collected evidence, job export требует лишнего scan.

## Solution

Typed aggregate над existing frozen results, затем owned immutable snapshots для разных views, exact-key originals и resumable streaming export.

## User Stories

1. Как аналитик, я хочу count/incoming_count по chat/day без всех message bodies.
2. Как аналитик, я хочу видимое определение denominator и filters.
3. Как аналитик, я хочу publication date, explicit timezone и half-open период.
4. Как аналитик, я хочу total=null при неизвестном полном объёме.
5. Как читатель, я хочу original exact key/version из того же snapshot.
6. Как читатель, я хочу summary coverage с подробностями errors/gaps.
7. Как reviewer, я хочу проверить representative references, понимая их иллюстративность.
8. Как exporter, я хочу те же originals без новых Telegram calls и sync grant.
9. Как exporter, я хочу restart-safe private JSONL/Markdown и source manifest.
10. Как owner, я хочу current policy и generation check на каждом view/path.
11. Как owner, я хочу expire/cleanup производные с понятной границей уже скопированных файлов.

## Implementation Decisions

- Первый jobs_results aggregate поддерживает terminal reading-evidence jobs; current JSON1 либо bounded stdlib pass достаточен. Full coverage сохраняется; version/hash result не выдаётся за новый handle.
- Typed allowlist: count/incoming_count, group chat/day, deterministic top-K, typed outgoing/service inclusion; arbitrary SQL/MATCH отсутствует. Unknown metadata остаётся unknown.
- Aggregate определение и publication-time [since,until) возвращаются; observed counts не подразумевают полный Telegram search index или local history.
- Opaque snapshot reference отделён от pagination cursor, привязан к profile/generation/frozen selection/revision/expiry и повторно проверяет access.
- Default pages совместимы. New views messages/coverage/aggregate используют один frozen snapshot; exact keys ограничены его составом.
- Compact coverage содержит все incomplete flags, totals known/unknown, stopped reasons и gap category counts. Details optional, предупреждение о gap обязательное.
- export_start принимает exclusive evidence source либо прежний index source, never arbitrary destination; .part/checkpoint/final manifest reuse текущий exporter.
- Export bytes привязаны к source revision и policy; changed policy закрывает old path. Expiry/pinning work bounded и journalled, no orphaned readable derivatives.
- Private originals остаются durable согласно текущему job contract; snapshots/chunks/exports имеют bounded lifecycle. Server checks не отзывают уже скопированные bytes у caller.

## Testing Decisions

Public three-chat complete/failed/pending fixture: count и definition, exact originals after fake edit, unchanged source/coverage, two views one snapshot, foreign profile/expired/revoked denied. JSONL multi-chunk pause/restart: уникальные keys, same bytes/originals, zero new history/ack. Cross-view revocation/cleanup проверяется отдельным integration slice.

## Out of Scope

Storage migration как prerequisite, arbitrary agent SQL, inferred author names, silent sync/AI grants, inline whole export body по умолчанию.

## Further Notes

Contracts: [ADR 0008](../adr/0008-owned-evidence-references.md), [ADR 0009](../adr/0009-owner-local-analytics.md) and the [current reference](../reference.md).
