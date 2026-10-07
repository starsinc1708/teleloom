<p align="center"><img src="docs/assets/teleloom.png" alt="Teleloom — Telegram × MCP; 67 инструментов и 6 skills; хромовый самолётик с переплетённым шлейфом" width="720"></p>

# Teleloom

**Telegram для ваших агентов.** Набор Telegram-инструментов, шесть portable agent
skills, MCP-сервер и CLI. [English](README.md).

Teleloom позволяет искать сообщения, разбирать переписку, составлять сводки с
источниками, работать с документами и готовить действия для вашего подтверждения.
Подключите свой аккаунт или бота к привычному агентскому клиенту; локальный owner
хранит состояние и управляет соединениями.

## Что можно делать

| Задача | Возможности |
|---|---|
| Разобрать переписку | История, поиск по сообщениям и авторам, ответы, темы, непрочитанные |
| Исследовать много чатов | Ограниченные задания сбора, originals, счётчики, excerpts и экспорт |
| Получить проверяемую сводку | Ссылки на источники, проверка claim/revision и явные пробелы покрытия |
| Работать с вложениями | Фото, документы/PDF, optional OCR и транскрибация с явным доступом |
| Подготовить действие | Preview и подтверждение сообщений, ответов, медиа и поддержанных изменений |
| Снизить объём выдачи | Повторное использование fields, компактные ответы и доступ к точным originals |

**67 MCP-инструментов и шесть skills:** connect, read, inbox, digest, send, broadcast.
[Полный reference](docs/reference.md) описывает параметры и ограничения.

## Быстрый старт

Нужны Git, [uv](https://docs.astral.sh/uv/) и Python 3.12–3.14. Для личного аккаунта
получите свои API ID/hash на [my.telegram.org](https://my.telegram.org). Вводите
данные локально, вне переписки с агентом.

```powershell
git clone --branch v0.5.0 --single-branch https://github.com/starsinc1708/teleloom.git
Set-Location teleloom
uv sync --frozen
uv run teleloom init
uv run teleloom auth user --profile personal --ui browser
uv run teleloom profile list
uv run teleloom config client --client codex
uv run teleloom skills install --client codex
```

На телефоне: **Telegram → Настройки → Устройства → Подключить устройство**.
Отсканируйте QR. Сохранение сессии требует рабочего OS credential store.
Добавьте напечатанный fragment в конфигурацию клиента и переподключите MCP.
Другие targets: `claude`, `opencode` (v2), `opencode-v1`, `hermes`, `pi`.

[Полная установка на русском](docs/install-ru.md) · [Форматы клиентов](docs/clients.md).

## Первая задача для агента

> Проверь статус сервера и покажи профили с их правами. Предложи выбрать профиль
> и один доступный чат. Прочитай последние 20 сообщений, не отмечая их прочитанными.
> Сделай краткую сводку со ссылками и явно укажи пропуски. Ничего не отправляй,
> настройки и разрешения не меняй.

[Другие задачи и промпты](docs/workflows.md) · [Инструкция агенту](docs/agent-guide.md).

## Доступ и контроль

Новый профиль по умолчанию читает все доступные аккаунту чаты. Ограничьте его
selected policy и отдельными правами; настройка описана в руководстве.
Отправки и внешние изменения требуют permissions и подтверждения неизменяемого
preview. Неопределённая доставка сохраняется как `unknown` и не повторяется
автоматически. Чтение не означает acknowledgment.

Данные сохраняются локально, credentials — в OS keyring или заданном environment.
Выбранный вами агентский провайдер получает запрошенные через MCP данные;
локальный сервер не делает внешнюю модель локальной. История бота ограничена
собранными наблюдениями. [Модель безопасности](SECURITY.md).

## Разработка

Первый публичный выпуск — **v0.5.0**, с чистой Git-историей. Package, module, CLI,
MCP-сервер и локальный namespace называются `teleloom`; переменные — `TELELOOM_*`.
Установка доступна из репозитория и [release artifacts](https://github.com/starsinc1708/teleloom/releases).
Публикация в PyPI не заявляется.

[Документация](docs/README.md) · [Issues](https://github.com/starsinc1708/teleloom/issues) ·
[CONTRIBUTING](CONTRIBUTING.md) · [AGENTS.md](AGENTS.md) · [Changelog](CHANGELOG.md).
MIT. Независимый проект, не связанный с Telegram.
