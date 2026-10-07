---
status: accepted
---
# ADR 0008: owned immutable evidence references

Implemented in the public baseline. Current behavior is documented in the
[reference](../reference.md); verification limits are in [acceptance](../acceptance.md).

Новым aggregate/details/excerpt/export workflows нужен общий original, а нынешний
pagination cursor не является general snapshot handle. Выбираем opaque owned reference
с frozen selection/revision, profile generation, expiry и current policy checks.
Exact keys не запускают live reread и не используют изменяемый index как historical original.

Это расширяет существующий snapshot/state seam, сохраняя прежние pages.
Revocation части mixed selection закрывает весь производный scope, как ADR 0006.
Краткий coverage обязан показывать gaps; details не единственное место предупреждения.
Длительная export pinning bounded/journalled; private reference не credential и не grant.
TTL/cleanup производных не стирает durable originals. Уже скопированные bytes и ранее
отданный model context находятся вне server revocation boundary.

T11 расширяет этот reference seam на opt-in excerpts. Direct reads замораживают
только возвращённую страницу при необходимости сокращения, с пределами 100 keys,
2 MiB normalized originals, 16 активных snapshots/profile и TTL 30 минут. Это
bounded private state, не durable reading job и не sync grant. Budgeted attachment,
event и transcription snapshots не переживают retention исходного job и повторяют
его permission checks.
Direct cached STT также связан с исходным job: его consent, cleanup и expiry
проверяются повторно, а TTL freeze ограничен retention этого source.
Exact field/record continuation отдаёт chunks immutable normalized JSON после
current-policy redaction, без live refetch или нового STT.
UTF-8 output budget отделён от Unicode chunk offsets и UTF-16 entity spans;
сокращённые ranges omitted с notice. Default pages и projection остаются прежними.

Альтернатива live refetch отклонена из-за edit/delete drift; caller-visible raw paths и
переиспользование cursor без отдельного контракта создают неоднозначную access/expiry модель.
Контракт: [frozen evidence spec](../specs/frozen-evidence-analytics.md).
