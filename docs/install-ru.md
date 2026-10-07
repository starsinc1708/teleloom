# Установка и настройка Teleloom

Teleloom — набор Telegram-инструментов, portable agent skills и MCP-сервер с CLI `teleloom`.
[English guide](getting-started.md) · [Готовые задачи и промпты](workflows.md)

Для **новой установки** выполните шаги ниже в Windows PowerShell.
Для **уже настроенного Teleloom owner** сохраняйте установленный пакет, profiles,
permissions, jobs и receipts; [обновление](#проверка-сборки-и-обновление) — отдельный шаг при необходимости.
В terminal с теми же environment/`TELELOOM_DATA_DIR`, что у client config, выберите
его установленный `python.exe` (для CLI — sibling `python.exe` к `pythonw.exe`):

```powershell
$teleloomPython = Read-Host 'Абсолютный путь к установленному owner python.exe'
& $teleloomPython -m teleloom profile list
& $teleloomPython -m teleloom doctor --mode local
```

Namespace приложения и OS keyring — `teleloom`, переменные окружения — `TELELOOM_*`.
Установка с другим namespace требует отдельной явной миграции; существующий
owner и его данные не заменяйте автоматически.

Если profile уже настроен, переходите к [первому чтению](#5-проверьте-identity-и-одно-чтение).
Повторные init/auth не нужны. В дальнейших fresh примерах заменяйте prefix
`uv run teleloom` на `& $teleloomPython -m teleloom` для installed owner.
Если требуется upgrade, сначала выполните supported
[release pipeline](release-pipeline.md#selecting-and-preserving-an-owner), затем тот же read путь.

## 1. Установите исходники и инструменты

Нужны Git, uv и Python 3.12–3.14. Репозиторий публичный.
Если Git/uv отсутствуют:

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
```

Откройте новый PowerShell в выбранном постоянном родительском каталоге:

```powershell
git clone --branch v0.5.0 --single-branch https://github.com/starsinc1708/teleloom.git
Set-Location teleloom
uv sync --frozen
uv run teleloom init
uv run teleloom doctor --mode local
$teleloomPython = (Resolve-Path '.venv/Scripts/python.exe').Path
```

uv создаёт `.venv` с locked dependencies. Optional Jev/PDF/OCR/STT устанавливаются
по необходимости, не для первого подключения. Tag фиксирует исходники; источник
editable установки имеет `build_type=dev`, release wheel — отдельный artifact.

Если копия уже существует, проверьте `git status --short` и сохраните свои
изменения. Повторный clone или переключение работающего owner на editable не нужны.
Сессии и токены хранятся в OS credential store, данные Windows — по умолчанию
`%LOCALAPPDATA%\teleloom`. `.env` автоматически не читается.

## 2. Войдите локально

Для user account создайте своё приложение на [my.telegram.org](https://my.telegram.org)
и получите API ID/hash. Вводите их лично в скрытые CLI-запросы:

```powershell
uv run teleloom auth user --profile personal --ui browser
uv run teleloom profile list
```

На телефоне: **Telegram → Настройки → Устройства → Подключить устройство**.
Отсканируйте QR; при наличии 2FA введите пароль на временной loopback странице.
QR обновляется автоматически, login ограничен десятью минутами; отмена — кнопка
или Ctrl+C. Secrets, QR и login codes остаются вне агентского диалога.

Альтернативы:

```powershell
uv run teleloom auth user --profile personal --ui terminal
uv run teleloom auth user --profile personal --method code
```

Второй аккаунт получает своё имя profile, например `work`.
Повторная auth с `--replace` меняет generation и сбрасывает permissions;
это отдельное явное решение владельца. Auth и configuration требуют stopped owner.

## 3. Выберите чаты и инструменты

По умолчанию профиль имеет `read_mode=all`: доступные этому аккаунту чаты.
Чтобы ограничить agent выбранными разговорами, получите canonical numeric chat ID
через bounded `chat_resolve`/CLI до ограничения либо укажите уже известный ID.
Название или автор сообщения не разрешает private chat автора.

```powershell
$chatId = Read-Host 'Точный chat_id выбранного чата'
uv run teleloom stop
uv run teleloom profile allow --scope read -- personal $chatId
uv run teleloom profile read-policy personal --mode selected
uv run teleloom config exposure --mode read-only
```

`--` перед positional arguments позволяет отрицательный ID.
Пустой selected allowlist запрещает все chats. Tool exposure независимо от read
policy: `all`, `read-only` или `selected --tool NAME`. Read-only оставляет
bounded чтение и local jobs, ограничивая внешние изменения.
Permissions `send`, `mutation`, `sync`, `broadcast`, `jev`, `event`,
`transcription` и `transcription_external` выбираются отдельно; first read
не требует ни одного из этих grants. После изменений перезапустите owner и переподключите MCP.

## 4. Подключите клиент

```powershell
uv run teleloom config client --client codex
uv run teleloom skills install --client codex
```

Перенесите весь напечатанный fragment в client config, сохранив остальные настройки.
Для Codex — `%USERPROFILE%\.codex\config.toml` или configured `CODEX_HOME`;
fragment добавляется после global TOML keys. При существующей секции
`mcp_servers.teleloom` замените её, без дубликата.

Строки после `[mcp_servers.teleloom.env]` относятся к этой таблице до следующего
заголовка: global `model`, `notify` и другие keys должны находиться перед таблицами.
Пустая строка таблицу не закрывает. На Windows command использует installed
`pythonw.exe` при его наличии; он запускает bridge без отдельного окна терминала.

Шесть skills устанавливаются в `%USERPROFILE%\.agents\skills`.
Installer сохраняет существующие файлы; намеренное `--force` используйте после
просмотра diff/backup. Перезапустите или reconnect клиент для нового discovery.
[Форматы Claude/OpenCode 2/Hermes/Pi и другие hosts](clients.md) описаны отдельно.
Model name (DeepSeek/GLM) выбирается в MCP-capable host, не вместо него.

## 5. Проверьте identity и одно чтение

Этот шаг общий для fresh и existing owner. Используйте `$teleloomPython`, выбранный
выше, и сохранённое owner environment. Сначала проверьте discovery и profiles:

```powershell
& $teleloomPython -m teleloom call server_status
& $teleloomPython -m teleloom call profiles_list
& $teleloomPython -m teleloom doctor --mode local
& $teleloomPython -m teleloom doctor --mode mcp
```

`server_status`/client bridge может запустить owner; `doctor --mode mcp`
инспектирует только уже работающий owner. Local mode не запускает его.
Оба doctor modes не читают Telegram и не вызывают AI; legacy `--live` означает
MCP connectivity, а не live Telegram/provider quality.

В `profiles_list` выберите exact `id`, проверьте сохранённую `identity`, `kind`,
`backend`, `read_policy`, отдельные permission lists и `tool_exposure`.
`connected=false` до первого Telegram вызова само по себе не требует auth.
Текущую identity проверяет `account_read` с `operation={"kind":"me"}`.
Далее выведите до 10 readable chats и выберите один canonical ID из результата:

```powershell
$profileId = Read-Host 'Точный profile_id из profiles_list'
$argumentsPath = [IO.Path]::GetTempFileName()
try {
    $arguments = @{ profile_id = $profileId; operation = @{ kind = 'me' } } | ConvertTo-Json
    [IO.File]::WriteAllText($argumentsPath, $arguments, [Text.UTF8Encoding]::new($false))
    & $teleloomPython -m teleloom call account_read --args-file $argumentsPath
    $arguments = @{ profile_id = $profileId; limit = 10 } | ConvertTo-Json
    [IO.File]::WriteAllText($argumentsPath, $arguments, [Text.UTF8Encoding]::new($false))
    & $teleloomPython -m teleloom call chats_list --args-file $argumentsPath
    $chatId = Read-Host 'Один canonical chat_id выбранного readable чата'
    $arguments = @{ profile_id = $profileId; target = $chatId } | ConvertTo-Json
    [IO.File]::WriteAllText($argumentsPath, $arguments, [Text.UTF8Encoding]::new($false))
    & $teleloomPython -m teleloom call chat_resolve --args-file $argumentsPath
    $arguments = @{ profile_id = $profileId; chat_id = $chatId; limit = 10; source = 'live' } | ConvertTo-Json
    [IO.File]::WriteAllText($argumentsPath, $arguments, [Text.UTF8Encoding]::new($false))
    & $teleloomPython -m teleloom call messages_get --args-file $argumentsPath
} finally {
    Remove-Item -LiteralPath $argumentsPath -ErrorAction SilentlyContinue
}
```

Продолжайте только после `ok=true` и проверки выбранной identity/chat.
First success — один page до 10 сообщений, с original IDs/text, `source`,
`coverage`, `incomplete`, warnings и возможным `next_cursor`; весь history он
не обещает. Для bot обоих backends history означает saved observations,
а `source=live` не превращает его в Telegram user history. Exact native lookup
у MTProto bot — отдельная capability, не полное историческое покрытие.

При `read_not_allowed` сохраните exact profile/chat: owner может отдельно
разрешить только этот ID через `profile allow --scope read -- PROFILE CHAT_ID`
после stop, затем reconnect. Selected policy сохраняется. Пустой allowlist или
список chats не доказывает отсутствие разговоров. Если tool скрыт exposure,
owner выбирает нужные read tools локально; смена source/cursor не обходит policy.

Пример запроса агенту:

> Используй teleloom-read с профилем personal. Прочитай последние 10 сообщений
> разрешённого чата CHAT_ID, покажи original text, sources и coverage.
> Ничего не отправляй и не отмечай прочитанным.

Для digest агент пишет summary из bounded originals; missing replies, bot saved
updates и неполное coverage должны быть явно обозначены.
Чтение не выполняет acknowledgment.

## Дополнительные возможности

Все permission/config commands выполняются после `teleloom stop`; после них
reconnect MCP. Полные public schemas и backend ограничения — в [reference](reference.md).

| Задача | Явный следующий шаг |
|---|---|
| Индекс и export | `teleloom profile allow --scope sync -- personal CHAT_ID`; `sync_start` → status → index export. Export покрывает local snapshot |
| Тестовая отправка | Scope `send` точному recipient; выбранное exposure должно включать preview/execute. Полный immutable preview и отдельное owner confirmation обязательны |
| Broadcast | Дополнительно scope `broadcast` каждому target; unknown outcome не replay |
| Изменения сообщения/группы/аккаунта | Отдельные mutation/management permissions; [administration](administration.md) |
| Фото и media send | Exact file root и send grant; [media](media.md) |
| PDF/OCR/local audio | Выбранные extras/engines/models; [attachments](attachments.md) |
| Jev | Только при необходимости: fresh/dev `uv sync --extra jev`, private key, scope `jev` и budget; сводку всё равно пишет caller |
| Audio providers | `transcription_capabilities`, owner settings и отдельные grants; [transcription](reference.md#transcription) |
| Events | Отдельный `event` grant точному readable chat, collection opt-in и host pull; [events](reference.md#incoming-events) |

Для Bot API:

```powershell
uv run teleloom stop
uv run teleloom auth bot --profile helper
uv run teleloom profile polling helper --enable
```

Токен вводится скрыто, API ID/hash для Bot API не нужны. Выберите read policy
бота отдельно. Bot API читает collected updates, не личный Telegram unread/history;
MTProto bot backend явно provisioned отдельно. Чужой webhook/poller автоматически
не отключается.

## Проверка сборки и обновление

Сравнивайте `package_version`, `build_id`, `source_commit`, `build_type`
локального package и running owner; одинаковая version не доказывает одинаковый
artifact. `unknown` означает отсутствие проверяемого provenance.

Установленный noneditable owner обновляется через [release pipeline](release-pipeline.md)
из проверенного wheel с выбранными extras и сохранением состояния.
Tooling environment — отдельное: `uv run`/`uv sync` внутри owner environment
может вернуть editable пакет. Используйте exact installed interpreter;
готовый artifact не требует новой сборки или полного повторного smoke.
Существующий [upgrade fixture](../tests/test_release_owner.py) проверяет сохранение
config/client, identity/generation/capabilities, permissions, historical jobs и
unknown receipts, обновление только выбранных skills и обязательный native reconnect.
Он использует temporary owner; это не live upgrade вашей установки.

После upgrade намеренно reconnect клиент и проверьте running build/discovery.
CLI/HTTP успех не подменяет native client observation.
[Build policy](adr/0005-build-provenance.md) и [фактическая acceptance](acceptance.md).

## Если что-то не работает

| Симптом | Следующий шаг |
|---|---|
| `uv`/Git недоступны | Новый terminal, проверить PATH и доступ к GitHub |
| `credentials_unavailable` | Восстановить OS credential backend; session не сохраняется plaintext |
| `owner_busy`/`session_in_use` | Определить exact owner этой session, остановить его перед auth/config; не убивать все Python процессы |
| Порт занят | При stopped owner выбрать свободный `teleloom init --port PORT` |
| Пустое selected чтение | Проверить exact profile/chat ID и read allowlist; не расширять grants автоматически |
| MCP не запускается | Exact interpreter/data dir, regenerate fragment если путь изменился, reconnect |
| GUI открывает terminal | Сгенерировать fragment с sibling `pythonw.exe` и reconnect |
| TOML env/notify error | Перенести global keys до таблиц; сохранить одну MCP section |
| QR или revoked auth | Terminal/code fallback; replacement явно сбрасывает grants |
| `read_timeout`/`peer_unavailable` | Проверить identity/network, bounded read recovery; uncertain write проверять по receipt до любого нового плана |
| Bot не видит событие | Polling, чужой webhook/poller, membership/privacy и saved-update coverage |
| Engine/provider отсутствует | Capabilities и точная local настройка; нет автоматического download/upload/fallback |

Для private proxy используйте локальный hidden `python -m keyring set teleloom PROFILE:proxy`;
credential values и keyring `get` не выводятся в агентский диалог.
Поддерживаемые overrides и deadlines — в [reference](reference.md#cli-and-diagnostics).
