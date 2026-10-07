---
status: accepted
---
# ADR 0009: typed analytics у владельца данных

Implemented in the public baseline. Current behavior is documented in the
[reference](../reference.md); verification limits are in [acceptance](../acceptance.md).

Счётчики и local retrieval считаются до выдачи модели над разрешённым collected scope.
Выбираем typed allowlisted aggregate/filter definitions поверх existing SQLite/FTS
или bounded stdlib pass. Произвольный SQL/MATCH не является public interface.
Первый aggregate использует existing terminal frozen job и full coverage без migration.

Observed count не full total: unknown totals null, source/freshness/gaps и denominator
явные. Стабильные refs возвращают originals; representative examples не доказывают
семантику всей группы. Source=index не включает sync и не получает Telegram semantics.
Vector store/remote analytics отклонены сейчас: нет измеренного качества/операционного
выигрыша, зато появляются новый data boundary и consent/cost obligations.

Это decision о месте вычислений и долгоживущем typed contract, не выборе Python файла.
Контракты: [evidence](../specs/frozen-evidence-analytics.md), [search](../specs/search-and-event-workflows.md).
