---
status: accepted
---
# ADR 0010: caller-owned digest claims вместо скрытого server LLM

Implemented in the public baseline. Current behavior is documented in the
[reference](../reference.md); verification limits are in [acceptance](../acceptance.md).

Calling agent уже пишет digest; выбираем проверяемый claim/revision manifest поверх
owned originals, включая supporting/contradicting sources и incomplete coverage.
Summaries остаются paraphrases; quotes проверяются по original.
Latest reuse требует source/access/generation validation; observed-event gap означает
stale/incomplete либо bounded reread, а не доказанную актуальность.

Server LLM summary/cache сейчас отклонён: добавляет consent, provider/model/cost и
retention обязанности без подтверждённого запроса. Внешний upload остаётся явным и
не наследует read grant. Project-owned derived retrieval следует ADR 0006.
Caller files, уже показанный текст и model memory сервер ретроактивно удалить не может;
workflow обязан описать эту trust boundary и прекращать reuse после revocation.

Контракт и human semantic-quality seam: [digest spec](../specs/digest-and-scale.md).
