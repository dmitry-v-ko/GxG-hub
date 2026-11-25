# gxg-approval-notifier

Notify-бот ([approval-service-v1 «Notify-бот»](../../docs/specs/approval-service-v1.md#notify-бот), EFF-13, EFF-14,
AS-18…AS-20, AP-08), веха M9. Отдельный образ и lockfile (AS-29): только `httpx` и `psycopg`.

- **Outbox → Telegram.** Строки `notify` пишет T1 (`api`) с полями AS-19: tenant, `preview.title`,
  `preview.target.label`, requester, срок, список approver'ов. Значений `fields`, `changes`, сумм и args в строке нет,
  а роль `gxg_notifier` не читает `intents`. Каждая строка уходит один раз всем привязанным approver'ам и помечается
  доставленной. Ошибка отправки только логируется: бот — подсказка, источник — список в UI (EFF-14).
- **AP-08.** На чат не больше 5 отдельных сообщений за 10 минут, дальше — одна сводка со ссылкой на список.
  shortcut: окно в памяти процесса. Telegram даёт одному токену одного потребителя `getUpdates`, поэтому экземпляр один.
- **Long polling.** `getUpdates` с `allowed_updates=["message"]`. Команды: `/start` (подсказка) и `/link <6 цифр>` в
  личном чате → `gxg_link_claim`. Код одноразовый, живёт 10 минут; 5 неверных кодов за 10 минут блокируют отправителя.
  Остальное игнорируется. Кнопки — только URL, `callback_data` нет (AS-20, EX-15).

## Конфигурация

`GXG_DB_DSN_FILE` (роль `gxg_notifier`), `GXG_TELEGRAM_TOKEN_FILE` — секреты-файлы; `GXG_UI_PUBLIC_URL`,
`GXG_TELEGRAM_API_URL` (по умолчанию `https://api.telegram.org`), `GXG_NOTIFY_INTERVAL_S` (2). Входящих соединений нет,
порт не слушается; health-эндпоинта нет: живость видна по логам `outbox`/`getUpdates` (как у `sweeper`, backlog).

## Runbook

| Симптом | Что делать |
| --- | --- |
| Токен бота мог попасть в логи (сборки до M9 цикла 2 писали URL `…/bot<token>/…` через httpx на INFO) | Отозвать токен у BotFather, выдать новый, заменить секрет `telegram_bot_token`, удалить старые логи из хранилища |
| `/link` не отвечает | Логи `getUpdates: …`; один экземпляр на токен (второй получает 409 от Telegram) |

## Тесты

`uv run pytest approval/notifier/tests_db`: PostgreSQL и fake Bot API (`testing/fake_bot_api.py`, контракт Bot API для
`getMe`, `getUpdates`, `sendMessage`; в тестах — `httpx.MockTransport`, в профиле `test` compose — HTTP-сервер).
Настоящий Telegram в тестах не используется.

## M10: webhooks (AS-21…AS-23, EX-20)

Тот же процесс доставляет события `outbox` с темой `webhook` подписчикам tenant'а из bundle (`platform.webhooks`). Событие пишет триггер на каждую смену статуса.

- **Подпись** — `GxG-Signature`, функция kit `webhooks.sign`. Получатель проверяет её `webhooks.verify`.
- **Порядок** — head-of-line по подписчику.
- **Повторы** — backoff от 5 с до 1 ч. Через 24 ч событие становится `dead` (алерт `GxgWebhookDead`).
- **Секреты** — `GXG_WEBHOOK_SECRETS_DIR`. Bundle задаётся через `GXG_BUNDLE_*`.
