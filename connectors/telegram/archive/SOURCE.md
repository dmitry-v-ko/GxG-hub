# Схема архива Telegram — контракт с gxg_agents_v2

Коннектор читает архив `telegram_archive`, который наполняет archive-sync существующего сервиса ([ADR-0004](../../../docs/adr/0004-telegram-connector-service.md)). Схема архива — контракт между сервисами. Здесь — дословные копии миграций на закреплённом commit; тесты и стенд строят базу только из них.

- Репозиторий: `dmitry-v-ko/gxg_agents_v2`, commit `f1c4d6d9e7bd5202fc703dabf3b0fba3fc732f0f`.
- `20261105010000_telegram_archive.sql`, `20261106010000_telegram_archive_semantic.sql` — `supabase/migrations/`, без изменений.
- `00_capability_hub_connections.sql` — дословная выдержка `CREATE TABLE capability_hub.connections` из `supabase/migrations/20260930010000_capability_hub_replacement.sql` и две схемы, на которые ссылаются миграции архива. Только для тестов и стенда: в настоящей базе они есть.
- `reader_role.sql` — роль коннектора (наша, не из gxg_agents_v2).
- `seed.py` — синтетические fixtures (тесты и стенд), не из gxg_agents_v2.

Сверка с upstream (нужен клон gxg_agents_v2):

- `check_upstream.sh <клон>` — копии здесь равны миграциям на закреплённом commit. Хеши ниже проверяет и тест `test_vendored_schema_is_the_pinned_upstream` (по файлам здесь).
- `check_upstream.sh --drift <клон> [<ref>]` — **уведомление об обновлении (ADR-0004):** падает, если в `supabase/migrations` после закреплённого commit (до `origin/HEAD` после `git fetch`) появилась миграция, затрагивающая `telegram_archive` или `capability_hub.connections`. На неё опирается граница scope: архив держит только чаты scope благодаря триггеру очистки сервиса (CON-TG-06).

Обе проверки запускают `scripts/ci/check-all.sh` (клон — `GXG_AGENTS_V2`, по умолчанию `../gxg_agents_v2`; без клона check-all падает, пропуск только явный: `UPSTREAM_DRIFT=skip`) и job `telegram-upstream` в `.github/workflows/ci.yml`, в том числе по расписанию раз в 6 ч. Job нужен секрет `GXG_AGENTS_V2_TOKEN` (токен только на чтение `contents` репозитория gxg_agents_v2); без него job красный.

```text
700a5c8eadaddc5d77429031e70afa4c6b494a94061a3309a01ded0023d9c3d0  20261105010000_telegram_archive.sql
99cb0019407b79d70d0ccc59f04fae655325aefcc4ecfb8371cd282c5b22456e  20261106010000_telegram_archive_semantic.sql
```

Обновление: поднять commit, скопировать миграции архива заново, обновить хеши, прогнать тесты коннектора — контрактный тест схемы покажет, какие колонки адаптера пропали или поменялись.
