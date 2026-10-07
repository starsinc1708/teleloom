# Owner workflows: подключение, права и восстановление

**Статус:** реализованный контракт первого публичного выпуска. Историческая постановка проблемы ниже объясняет решение; текущие параметры — в [reference](../reference.md), границы проверки — в [acceptance](../acceptance.md).

## Problem Statement

Новый пользователь путает fresh install с upgrade, local doctor с Telegram health и reconnect с новой auth. Несколько клиентов и широкий catalog усложняют поиск причины отказа.

## Solution

Один first-success путь, короткая recovery card и ограниченная диагностика выбранной identity. Existing policies и provider capabilities объясняются как часть сценария.

## User Stories

1. Как новый owner, я хочу выбрать fresh путь до запуска setup commands.
2. Как existing owner, я хочу использовать сохранённый package/state и supported upgrade flow.
3. Как owner, я хочу явно читать два выбранных чата и видеть отдельные send/mutation/AI/sync/event grants.
4. Как operator, я хочу увидеть installed/running build и реальный discovery.
5. Как operator, я хочу отличать проверку MCP от выбранного Telegram read.
6. Как multi-client пользователь, я хочу второй bridge к тому же owner.
7. Как пользователь нового protocol mode, я хочу поддержанный каталог либо ясную ошибку.
8. Как owner, я хочу явный local replacement revoked session и описание generation/grant последствий.
9. Как voice пользователь, я хочу provider availability и осознанный local/native/cloud выбор.
10. Как читатель фото, я хочу source identity и extraction/inline image limits отдельно от captions.

## Implementation Decisions

- Fresh/existing paths используют текущую init/auth/owner/release модель; secrets/OTP/2FA остаются локальными.
- Profiles/discovery и owner-side effective scope дают объяснение разрешений; MCP не расширяет policy.
- Protocol ticket проверяет pin и реально поддержанные transports/revisions. SDK major upgrade требует нового compatibility решения; unsupported opt-in объявляется явно.
- Опциональная Telegram диагностика только для exact readable profile/chat: bounded connection/read timings, количество реально наблюдаемых calls и redacted error codes.
- Local/MCP diagnosis не утверждает external Telegram health. Timeout не вызывает auto-login, broad process kill или повторную delivery.
- Media walkthrough reuse текущих providers, jobs/results и selected source; model download/upload/fallback явные.

## Testing Decisions

Публичные CLI doctor и MCP discovery/bounded-read fixtures: два bridges/один owner, forbidden profile/chat, timeout/disconnect cleanup и отсутствие секретов. Prior art — существующие launch, owner lifecycle, policy и deployment workflows. Documentation-only slices: diff/link review; recipe не является live PASS.

## Out of Scope

Новый session pool, hosted setup, web admin UI, automatic reauth, broad process killing, произвольный callback и незапрошенный paid AI.

## Further Notes

Current usage: [setup](../getting-started.md), [clients](../clients.md), [read policy ADR](../adr/0006-read-policy-and-session-metadata.md).
