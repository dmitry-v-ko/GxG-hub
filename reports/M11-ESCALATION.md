# M11 — вопросы владельцу

Веха M11 «Telegram read-only», ветка `claude/m11-telegram-read`. Доступ к `dmitry-v-ko/gxg_agents_v2` получен (только чтение, `f1c4d6d`); в нём ничего не менялось.

## Решены (владелец, 2026-11-21, [ADR-0004](../docs/adr/0004-telegram-connector-service.md))

| # | Вопрос цикла 1 | Решение |
| --- | --- | --- |
| 1 | Сетевой порт существующего сервиса | Порта не будет: коннектор — отдельный сервис, читает архив `telegram_archive` ролью PostgreSQL только на `SELECT` по схеме миграций gxg_agents_v2. Сделано в цикле 2 (`connectors/telegram/src/telegram_connector/archive.py`) |
| 2 | AC-M11-01 на тестовом аккаунте | Не нужен: коннектор не говорит с Telegram; AC-M11-01 — через ContextForge на локальной базе архива |
| 3 | RID чата | `telegram:<connection>/chat/<chat_ref>`; примеры в спеках обновлены |
| — | DoD-1 без минут GitHub Actions | Зелёный локальный `scripts/ci/check-all.sh` на проверяемом commit, вывод — `ci_local` в отчёте |

## Ждут владельца (работу не блокируют, `pending_owner_decision`)

| # | Вопрос | Вариант по умолчанию |
| --- | --- | --- |
| 4 | **Ключ дедупликации для M12.** Существующий сбор Chat folders отдаёт `externalId = <peer_id>:<message_id>`; коннектор не выдаёт peer ID (MCC-04) и отдаёт `id = <chat_ref>:<message_id>`, сбор — по чату, а не по папке. Переход Signals на Hub изменит ключ `SourceInboxItem` для Telegram | M12 сопоставляет ключи один раз (таблица `chat_ref → peer` в Signals из той же базы архива) или принимает новый ключ с переходом; решение — в задании M12 |
| 5 | **Кто выдаёт роль в production.** `gxg_telegram_reader` нужен `BYPASSRLS` (RLS в миграциях принудительный и без политик), это право выдаёт суперпользователь базы Supabase. Альтернатива — политика `SELECT` для роли, но это миграция в gxg_agents_v2 | Оператор базы выполняет `connectors/telegram/archive/reader_role.sql` и задаёт пароль из secret store; миграций в gxg_agents_v2 не нужно |
