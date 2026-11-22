# Коннектор `telegram` (M11, только чтение)

Отдельный сервис ([ADR-0004](../../docs/adr/0004-telegram-connector-service.md)): MCP за ContextForge по [Connector Contract](../../docs/specs/mcp-connector-contract-v1.md). Читает архив `telegram_archive`, который наполняет archive-sync существующего Telegram-сервиса из `gxg_agents_v2`. С Telegram не говорит: сессии, её строки и Telegram-библиотек у него нет (AC-M11-02). Live-операции (live tail, live fallback, `propose`) не публикуются до отдельного решения.

| Tool | Действие grant | Что делает |
| --- | --- | --- |
| `telegram.messages.search` | `read` | Полнотекстовый поиск (русский и английский) по архиву разрешённых чатов. Результат — [search-result](../../schemas/search-result.schema.json) |
| `telegram.thread.read` | `read` | История одного чата из архива, новые сверху; `before_message_id` листает назад |
| `telegram.messages.collect` | `collect` | Сбор одного чата по cursor (позиция архива, старые сверху) для CollectSource Signals. `next_cursor` возвращается без изменений |

## Порядок проверок

identity (`X-GxG-Identity` от ContextForge, SEC-03) → схема аргументов → grants: названный чат без grant'а — `not_found` **до** запроса к архиву (GR-17, AC-M11-03); поиск сужается до `allowed()`, каждый элемент проверяется повторно (GR-16) → архив. Scope Connection — внешняя граница: в архиве лежат только чаты scope (триггер `purge_out_of_scope` сервиса удаляет чат в той же транзакции, что и изменение scope, CON-TG-06). Чат вне архива — тот же `not_found`, и при маске grant'а.

- **RID чата** — `telegram:<connection>/chat/<chat_ref>`; `chat_ref` — стабильный ID чата в архиве (CON-TG-09). Peer ID в ответы и логи не попадает (MCC-04, SEC-18): запросы используют его только для соединения таблиц.
- **Ошибки:** архив недоступен или не ответил (в том числе `statement_timeout` роли, 15 с, и таймаут tool'а, 20 с) → `temporarily_unavailable` (read, повтор безопасен); чат вне архива → `not_found`. В аудит пишется только класс исключения.
- **Flood-wait (AC-M11-04).** Сервис записывает flood-wait Telegram в `connection_state.retry_after` и не синхронизирует Connection до этого момента. `collect` → `rate_limited`, `retry_after_s` = оставшаяся задержка Telegram (округление вверх). `search` и `thread.read` отвечают из архива с `complete: false`, `partial_reasons: ["rate_limited"]`.
- **Тексты** — недоверенные данные (SEC-11): обрезка (400 символов в поиске, 4000 в `thread.read`/`collect`, `truncated`), затем экранирование управляющих и format-символов как `\u{XXXX}`; `untrusted: true`.
- **Полнота:** чат не синхронизирован или с разрывом → `partial_reasons: ["other"]`. Архив отдаёт не больше 100 результатов поиска и 128 чатов на запрос: дальше — `truncated`.
- **Аудит:** одна JSON-строка на вызов: `request_id`, tool, tenant, principal, `grant_id`, исход, класс ошибки, `authorization_header`. Без запроса, текстов, peer ID, DSN и токенов (AC-M11-05).

## База архива

- [`archive/`](archive): дословные миграции `telegram_archive` из gxg_agents_v2 на закреплённом commit ([`SOURCE.md`](archive/SOURCE.md), сверка — `archive/check_upstream.sh <клон>`, новые миграции upstream — `--drift`, оба в check-all и CI), роль [`reader_role.sql`](archive/reader_role.sql), синтетические fixtures [`seed.py`](archive/seed.py) для тестов и стенда.
- Роль `gxg_telegram_reader`: `SELECT` только нужных колонок `chats`, `messages`, `connection_state` (без `author_id` и `embedding`), транзакции только на чтение, `statement_timeout` 15 с, `BYPASSRLS` (в миграциях RLS принудительный и без политик). Пароль — секрет коннектора (SEC-16), задаётся `ALTER ROLE … PASSWORD` из secret store.
- Схема архива — контракт: тест колонок и прав (`test_contract_*`) и хешей (`test_vendored_schema_is_the_pinned_upstream`).

## Конфигурация

Переменные — в docstring [`server.py`](src/telegram_connector/server.py). Секрет один: `TELEGRAM_ARCHIVE_DSN` (`postgresql://gxg_telegram_reader:…@host/db`). `TELEGRAM_CONNECTIONS=t-alpha=tg-main:svc-alpha` — Connection архива для проверенного tenant'а (MCC-03).

## Тесты

```bash
uv run pytest connectors/telegram/tests        # в процессе, настоящий PostgreSQL 16 + pgvector на unix-сокете
stand/run.sh                                   # через ContextForge: stand/tests/test_telegram.py
cd connectors/telegram && uv run mutmut run    # DoD-4: tools.py, server.py, archive.py
```

Нужен PostgreSQL 16 с pgvector (`apt-get install postgresql-16 postgresql-16-pgvector`; другой путь — `TELEGRAM_PG_BIN`).

## Эксплуатация

| Ситуация | Что делать |
| --- | --- |
| `startup_refused`, `TELEGRAM_ARCHIVE_DSN` | Не задан или не `postgresql://`. Значение в лог не пишется |
| `/readyz` = 503, `not_ready`: `telegram archive is not available` | База архива недоступна или роль не входит; проверить сеть и пароль роли |
| Вызовы `collect` → `rate_limited` | Flood-wait у сервиса: повторять не раньше `retry_after_s` |
| Вызовы → `not_found` для нужного чата | Нет grant'а (`policies/tenants/<tenant>.yaml`, RID с `chat_ref`), grant истёк, чат вне scope Connection или ещё не синхронизирован archive-sync (его нет в архиве) |
| Обновление gxg_agents_v2 | Поднять commit в `archive/SOURCE.md`, скопировать миграции, обновить хеши, прогнать тесты коннектора |
