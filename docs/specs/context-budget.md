# Контекст: compact selection и bounded content

**Статус:** реализованный контракт первого публичного выпуска. Историческая постановка проблемы ниже объясняет решение; текущие параметры — в [reference](../reference.md), границы проверки — в [acceptance](../acceptance.md).

## Problem Statement

Projection уже есть, но full field explanations повторяются, selector вызывается перед каждой страницей, schemas и long transcripts занимают контекст сверх original-text cap.

## Solution

Использовать selected exposure и select-once; opt-in compact selection; output budgets с доступными originals и честными metadata.

## User Stories

1. Как owner, я хочу выбрать tools под задачу без нового dispatcher.
2. Как агент, я хочу прямые fields, когда schema известен.
3. Как агент, я хочу reuse selection только в том же tool/schema/task.
4. Как reviewer, я хочу detailed reasons по запросу.
5. Как агент, я хочу видеть fallback reason и uncertainty в compact result.
6. Как автор digest, я хочу сохранить original text и URL entities при inferred выборе.
7. Как читатель transcript, я хочу output cap всего selected content.
8. Как читатель, я хочу восстановить полный original из owned reference.
9. Как клиент, я хочу прежний full response по умолчанию.
10. Как исследователь, я хочу разделять payload bytes, schemas, round trips и model tokens.

## Implementation Decisions

- response_fields_select(detail=compact|full) оставляет full default; компактность применяется после текущего full selection/cache, не меняя decision/key/budget.
- Compact сохраняет tool/schema/fields/status, краткое explanation, safeguard list при его применении и конкретную fallback reason; explicit fields всегда authoritative.
- Fields reuse invalidates при tool/schema/intent изменении; никакой скрытой read или Jev/classification.
- max_output_bytes означает compact UTF-8 normalized data, с обязательными identity/coverage/warnings включёнными; wire/text/structured sizes измеряются отдельно.
- Если обязательная metadata не помещается, явная output_budget_too_small. Errors не превращаются в усечённый success.
- Полный original не режется в хранилище. Excerpt отдельный, с truncation/source version и owned continuation/reference. Для opt-in direct reads reference freeze должен быть bounded; no ambiguous refetch-as-original.
- Entity offsets остаются относительными к original; excerpt entity ranges пересчитываются либо omitted с notice. UTF-8 bytes не UTF-16 spans.
- T11/#70 implements opt-in budgets and frozen original JSON continuation; the public limits and retrieval workflow are in [reference](../reference.md). Remaining planned items retain their own acceptance gates.
- Selected exposure остаётся owner action с restart/reconnect; current MCP text+structured compatibility сохраняется.

## Testing Decisions

Public selector full/compact на disabled/cached/uncertain/fallback/budget cases даёт одинаковые effective fields, schema и status. Ten-page MCP workflow имеет один select call. Long Unicode + transcript fixture проверяет budget, exact original retrieval, policy/generation denial и no upload. Расширить существующий evaluator минимально; tokenizer boundary указывать только если он реально измерен.

## Out of Scope

Default response replacement, обещание 2× экономии, dynamic dispatcher, новая tokenizer dependency ради процента, OCR/STT cache без profiling.

## Further Notes

Contracts: [ADR 0004](../adr/0004-response-projection.md) and [ADR 0008](../adr/0008-owned-evidence-references.md). See [response fields](../response-fields.md) for current usage.
