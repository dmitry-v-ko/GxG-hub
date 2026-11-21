# M11 — цикл 2: план (ponytail full)

Оценка цикла 1 — 5.0 ([критик](../critic/M11-cycle-1.json)). Закрытый чек-лист — `required_improvements` критика (1–7). Плюс `new_scope` — решение владельца 2026-11-21 ([ADR-0004](../../docs/adr/0004-telegram-connector-service.md), `origin/claude/admiring-planck-vzys0x@643ea3e`): коннектор — отдельный сервис; читает архив `telegram_archive` существующего сервиса ролью PostgreSQL только на `SELECT`, по схеме миграций gxg_agents_v2 на закреплённом commit; fake-порт заменяется адаптером к базе архива; в тестах — локальный PostgreSQL со схемой и синтетическими fixtures; контрактный тест схемы и тест «запись этой ролью отклоняется»; live-операции не публикуются; реальный тестовый аккаунт не нужен; RID = `telegram:<connection>/chat/<chat_ref>`; DoD-1 — зелёный локальный `check-all.sh` (`ci_local`).

## Чек-лист цикла 1 → что делается

| # | Пункт критика | Решение в цикле 2 |
| --- | --- | --- |
| 1 | Вернуть docs M4/M5 | Сделано (`d5eb98f`): трёхстороннее слияние `fb14af1` (M5) и `643ea3e` (решения владельца) от общей базы `989fe9c`; конфликты — объединение (backlog) и новая редакция (AS-29/AS-30). Строка M11 в backlog возвращена |
| 2 | Порт к сервису или решение владельца по AC-M11-01/02 | Решение владельца — ADR-0004. Адаптер `ArchiveService` к базе `telegram_archive`; AC-M11-01 — через ContextForge на локальной базе архива (стенд), без Telegram |
| 3 | Контракт порта по реальному коду | Запросы повторяют `PostgresTelegramArchiveRepository.search` (ru+en `websearch_to_tsquery`, `ts_rank`, порядок, `limit + 1`, полнота по `chats.state/gap`). Scope — строки `telegram_archive.chats` Connection'а: архив по триггеру `purge_out_of_scope` (CON-TG-06) содержит только чаты scope (явные и участников папок), чат вне scope удаляется в той же транзакции, что и изменение scope. Тест: удаление чата из `metadata.scope.chats` → `not_found` во всех трёх tools. Маска grant'а + чат вне scope → `not_found` во всех трёх tools. Сбор — по чату, курсор `(archived_us, message_id)` как у Chat folders collection; ключ дедупликации для M12 — `chat_ref:message_id` (peer ID наружу не выходит) — `pending_owner_decision` #4 |
| 4 | AC-M11-02 не счётчиком-константой | Структурно (ADR-0004: «по построению»): в `uv.lock` нет Telegram-клиентов; у контейнера нет TG-переменных; роль архива не может писать (INSERT/UPDATE/DELETE, `apply_chat_batch`) — тест на базе; на стенде у коннектора нет выхода наружу (сеть `internal`), проба соединения с внешним хостом из контейнера не проходит |
| 5 | Доказательства | `M5.json` — из `fb14af1` (сделано); `M11.json` — прогон на чистом HEAD; поле `ci_local` с выводом `check-all.sh`; сбой CI на GitHub (billing) записан |
| 6 | SC-06, MC-01, SC-01 по имени, SC-08 для telegram через ContextForge | В `stand/tests/test_telegram.py` |
| 7 | Minor F7–F10 | F8 (срез до экранирования) и F9 (пометка untrusted в описании поиска) — исправить; F10 (fake в образе) — fake удаляется; F7 (копия из demo) — в backlog |

## Решения по ходу

- **Flood-wait (AC-M11-04) при чтении архива.** Коннектор не говорит с Telegram. Flood-wait провайдера сервис записывает в `telegram_archive.connection_state.retry_after` (не укорачивая). Пока он не прошёл, архив Connection'а не обновляется: `collect` (Signals, курсор) → `rate_limited`, `retry_after_s` = `ceil(retry_after - now())` — задержка Telegram (± 1 с); `search` и `thread.read` отдают архив с `complete: false`, `partial_reasons: ["rate_limited"]`. Чтение архива не блокируется ради уже сохранённых данных.
- **Счётчик запросов к порту (AC-M11-03).** На стенде — со стороны базы: `log_connections` архива, число подключений роли коннектора. В процессе — счётчик на границе адаптера. Адаптер открывает одно подключение на вызов (`shortcut`: пул — при нагрузке).
- **Права роли.** `gxg_telegram_reader`: `LOGIN NOINHERIT BYPASSRLS`, `default_transaction_read_only`, `statement_timeout`, `USAGE` схемы и column-level `SELECT` только нужных колонок `chats`, `messages`, `connection_state` (без `author_id`, `embedding`). `BYPASSRLS` нужен: в миграциях RLS `FORCE` без политик; политика стала бы изменением базы gxg_agents_v2 — `pending_owner_decision` #5 (кто выдаёт роль в production).
- **Тестовый PostgreSQL в процессе.** Свой fixture: `initdb` + `pg_ctl` из `/usr/lib/postgresql/16/bin` (или `TELEGRAM_PG_BIN`), только unix-сокет (корневой pytest запрещает TCP), шаблонная база с миграциями и fixtures, по базе на тест (`CREATE DATABASE … TEMPLATE`). pytest-postgresql не подходит: клиент ходит по TCP.

## Лестница

| Часть | Ступень | Почему |
| --- | --- | --- |
| Драйвер PostgreSQL | 4: `psycopg` 3 (зрелый, async) | — |
| SQL поиска | 2: запрос сервиса (`archive-repository.ts:299-337`) | Тот же контракт |
| Схема БД в тестах | 2: миграции gxg_agents_v2 дословно (`connectors/telegram/archive/`, `SOURCE.md` с commit и sha256) | Контракт схемы |
| Сверка с upstream | 6: `archive/check_upstream.sh` (`git show <commit>:<path>` в клоне, `sha256sum -c`) | Готового инструмента сверки «vendored ↔ upstream» нет |
| Тестовый PostgreSQL | 6: fixture ~30 строк | См. выше |
| Стенд | 3: образ `pgvector/pgvector:0.8.1-pg16` по digest, `docker-entrypoint-initdb.d` | — |

## Что сознательно не делается

- Live-операции (live tail, live fallback, `propose`) — ADR-0004, до M13.
- Пул подключений — `shortcut`.
- Вынос общего конвейера в kit — backlog (F7).
