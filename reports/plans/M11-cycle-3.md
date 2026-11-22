# M11 цикл 3 — план

Закрытый чек-лист: `required_improvements` из [`reports/critic/M11-cycle-2.json`](../critic/M11-cycle-2.json) (оценка 8.0). Новых задач вне него нет.

| # | Пункт критика | Что делаю (ponytail: самое простое, что закрывает) |
| --- | --- | --- |
| 1 | F1: порядок поиска не полный | `c.chat_ref` последним ключом `ORDER BY` в `ArchiveService.search`; тест: два чата с равными `(rank, sent_at, message_id)`, листание страницами 1/3/7 — каждый элемент ровно один раз |
| 2 | F2: нет уведомления об обновлении схемы (ADR-0004) | `check_upstream.sh --drift <клон> [ref]`: `git fetch`, дифф `supabase/migrations` от закреплённого commit до `origin/HEAD`; упоминание `telegram_archive` или `capability_hub.connections` → exit 1 со списком файлов. `check-all.sh` запускает его (клон — `GXG_AGENTS_V2`, по умолчанию соседний каталог; без клона — отказ, явный пропуск `UPSTREAM_DRIFT=skip`). Тест скрипта на временном git-репозитории. Описание в `archive/SOURCE.md` |
| 3 | F4, F5: решения не в спеке | Строки в `service-integration-v1` (Telegram): сбор по чату, cursor `(archived_us, message_id)`, ключ `chat_ref:message_id`, сопоставление с `peer_id:message_id` — M12; `collect` → `rate_limited`, пока синхронизация Connection отложена |
| 4 | F7: `ci_local` из частичных прогонов | Один полный `check-all.sh` на финальной HEAD; `M11.json` и `ci_local` из него |
| 5 | F3, F6, F8 в backlog | Строки M11 в `docs/backlog.md` (F6 = уже F7) |

## Не делаю

Пул подключений, `not_synced` для чата scope без строки в архиве (роль не читает scope Connection) — backlog.

## DoD-6

| Компонент | Готовое решение | Вывод |
| --- | --- | --- |
| Уведомление об обновлении схемы | git submodule + Dependabot/Renovate | Submodule тянет весь репозиторий gxg_agents_v2 в Hub и приватный доступ CI; `git diff` в существующем скрипте — 10 строк. Свой скрипт |
| Полный порядок | `ORDER BY` с уникальным ключом (PostgreSQL) | Используется |
