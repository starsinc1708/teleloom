# Поиск и события: точные фильтры и понятный source

**Статус:** реализованный контракт первого публичного выпуска. Историческая постановка проблемы ниже объясняет решение; текущие параметры — в [reference](../reference.md), границы проверки — в [acceptance](../acceptance.md).

## Problem Statement

Quiet sender в группе не находится end-to-end; local multi-chat FTS path отсутствует. Wait primitives без filters и host workflow не превращаются в полезного ассистента.

## Solution

Exact sender one-chat поиск; opt-in local indexed multi-chat ranking; typed observed-event filters и pull recipe, сохраняя source/coverage.

## User Stories

1. Как участник группы, я хочу искать exact sender_id в readable chat.
2. Как читатель, я хочу date bounds, cursor и filtered limit без skipped matching records.
3. Как bot owner, я хочу search только saved updates с explicit limitation.
4. Как аналитик, я хочу source=index по выбранным чатам.
5. Как читатель, я хочу stable ranking/snippet и полный original по reference.
6. Как reviewer, я хочу literal-AND semantics, freshness и incomplete index видимыми.
7. Как operator, я хочу observed events exact sender/topic/kind в заданном interval.
8. Как operator, я хочу mention-to-selected-identity filter, без guessed text mentions.
9. Как operator, я хочу unknown metadata и retention/restart gaps видимыми.
10. Как host, я хочу poll/continuation/sequence dedupe и отдельный confirmed reply.

## Implementation Decisions

- sender_id exact canonical Telegram identity; resolution не читает private history автора. User-native from_user либо bounded filtering с correct pagination; index/bot saved rows эквивалентны в их объявленном source scope.
- Первый sender slice one-chat messages_search. Global/topic/multi-chat расширение явно отдельное; отсутствие поддержки возвращает unsupported, не игнорирует filter.
- Multi-chat source=index использует existing FTS5 и current policy before query; no implicit sync. Literal AND и bounded filters, rank/date/chat/message stable ties.
- Local hit имеет original identity/version, bounded separate snippet и snapshot evidence ref; index freshness/gaps не превращаются в complete zero.
- Events wait filters работают над opt-in observed journal, не новым SDK connection. Interval — observed event time; original publication date отдельное.
- Missing sender/topic/mention facts идут в explicit skipped/unknown coverage, не fabricated match или complete absence; delete events могут не иметь этих facts.
- Pull host recipe использует per-chat continuation/sequence dedupe, quiet/deferred/gap handling. Host wakeup — capability выбранного host, не обещание daemon.
- Любой ответ на событие остаётся отдельным preview/confirmation. Outbound callbacks отсутствуют.

## Testing Decisions

Public MCP fixtures: quiet author outside newest-N, cross-chat colliding IDs, index edits/deletes, forbidden chat not queried, relevance tie stable, bot saved-only. Wait fixture: sender/topic/mentions, replayed sequence, unknown delete metadata, restart/retention gap, no ack/delivery. Golden query set измеряет lexical retrieval, не универсальную semantic recall.

## Out of Scope

Vector DB/reranker по умолчанию, unrestricted global sender scan, автоматический background sync, произвольные callbacks или autonomous sends.

## Further Notes

Contracts: [read policy](../adr/0006-read-policy-and-session-metadata.md), [events](../adr/0007-events-and-transcription.md) and [agent usage](../agent-guide.md).
