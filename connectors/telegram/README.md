# Коннектор `telegram` (M11, только чтение)

Фасад MCP к доменным портам существующего Telegram-сервиса из `gxg_agents_v2` ([service-integration-v1 «Telegram»](../../docs/specs/service-integration-v1.md#telegram)). Сессией Telegram владеет только сервис: коннектор не открывает её, не знает её пути и строки и не тянет Telegram-библиотек (AC-M11-02).

| Tool | Действие grant | Что делает |
| --- | --- | --- |
| `telegram.messages.search` | `read` | Полнотекстовый поиск по архиву разрешённых чатов. Результат — [search-result](../../schemas/search-result.schema.json) |
| `telegram.thread.read` | `read` | История одного чата, новые сверху; `before_message_id` листает назад |
| `telegram.messages.collect` | `collect` | Сбор одного чата по cursor (старые сверху) для CollectSource Signals. `next_cursor` возвращается без изменений |

## Порядок проверок

identity (`X-GxG-Identity` от ContextForge, SEC-03) → схема аргументов → grants: названный чат без grant'а — `not_found` **до** запроса к сервису (GR-17, AC-M11-03); поиск сужается до `allowed()`, каждый элемент проверяется повторно (GR-16) → порт сервиса. Scope Connection в сервисе остаётся внешней границей: grants её только сужают.

- **RID чата** — `telegram:<connection>/chat/<chat_ref>`. `chat_ref` — непрозрачная ссылка сервиса, привязанная к Connection (CON-TG-09). Peer ID в RID, ответы и логи не попадает (MCC-04, SEC-18).
- **Ошибки сервиса:** `flood_wait` → `rate_limited` с `retry_after_s` = задержка Telegram, округлённая вверх (AC-M11-04); `busy`, `auth_required`, `provider_error` → `temporarily_unavailable`; `chat_forbidden`, `peer_inaccessible` → `not_found`. Таймаут tool'а (20 с) → `temporarily_unavailable`.
- **Тексты** — недоверенные данные (SEC-11): управляющие и format-символы (bidi, zero-width) экранируются как `\u{XXXX}`, у `thread.read` и `collect` есть `untrusted: true`, текст режется до 4000 символов с `truncated`.
- **Полнота:** чат ещё не синхронизирован или с разрывом → `complete: false`, `partial_reasons: ["other"]`. Сервис отдаёт не больше 100 результатов поиска: на последней странице `truncated`.
- **Аудит:** одна JSON-строка на вызов: `request_id`, tool, tenant, principal, `grant_id`, исход, класс ошибки сервиса, `authorization_header` (пришёл ли заголовок `Authorization`: на стенде всегда `false`). Без запроса, текстов, peer ID и токенов (AC-M11-05).

## Порт сервиса

[`service.py`](src/telegram_connector/service.py): `TelegramService` — `scope_chats`, `search`, `read_thread`, `collect`, `ready`. Контракт `search` повторяет `PostgresTelegramArchiveRepository.search` сервиса; `read_thread` и `collect` — целевой контракт `telegram.thread.read` и позиция архива `(archived_us, message_id)`.

**Сетевого адаптера нет:** у сервиса пока нет endpoint для чтения архива. Единственная реализация — [`fake.py`](src/telegram_connector/fake.py) (fixtures, только `GXG_PROFILE=test`). Без него коннектор не стартует. Решение владельца — [`reports/M11-ESCALATION.md`](../../reports/M11-ESCALATION.md).

## Конфигурация

Переменные — в docstring [`server.py`](src/telegram_connector/server.py). Секретов нет: публичные ключи, пути и id. `TELEGRAM_CONNECTIONS=t-alpha=tg-main:svc-alpha` — Connection сервиса для проверенного tenant'а (MCC-03).

## Тесты

```bash
uv run pytest connectors/telegram/tests          # в процессе, fake-сервис (входят в корневой pytest)
stand/run.sh                                     # через ContextForge: stand/tests/test_telegram.py
cd connectors/telegram && uv run mutmut run      # DoD-4: tools.py, server.py
```

## Эксплуатация

| Ситуация | Что делать |
| --- | --- |
| `startup_refused`, `TELEGRAM_SERVICE` | Ожидаемо вне тестового профиля: адаптера к сервису ещё нет |
| Вызовы → `rate_limited` | Flood-wait Telegram у сервиса. Повторять не раньше `retry_after_s` |
| Вызовы → `not_found` для нужного чата | Нет grant'а (`policies/tenants/<tenant>.yaml`, RID с `chat_ref`), grant истёк или чат вне scope Connection сервиса |
| `/readyz` = 503 | Нет валидного bundle или сервис недоступен; причина — строка `not_ready` в логе |
