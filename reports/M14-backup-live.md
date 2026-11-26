# M14: бэкап и восстановление approval-db на Railway (AC-M14-01, AC-M14-02), 2026-11-26

Решение владельца 2026-11-26: «Да, включай» (подтверждено пользователем в сессии). Работа только в проекте `gxg-hub-pilot`; боевая approval-db не менялась, кроме роли бэкапа (`gxg_backup`, CONNECT и членство в `pg_read_all_data`).

## Что включено
- Сервис `approval-backup` (kind cron, `17 3 * * *` UTC), `deploy/railway/services.yaml`; образ `deploy/railway/approval-backup.Dockerfile`.
- Команда: `approval_backup.py backup --dir /var/backups/gxg-approval && approval_backup.py check --dir …`: `pg_dump -Fc` → AES-256-GCM → файл `gxg-approval-<UTC>.dump.enc` (0600), удаление копий старше 35 дней (RET-04), затем проверка AC-M14-02.
- Хранение: Railway Volume сервиса `approval-backup`, `/var/backups/gxg-approval`.
- Роль `gxg_backup` (roles.sql + `db-init`): LOGIN, `pg_read_all_data`, CONNECT, NOSUPERUSER/NOBYPASSRLS, без владения объектами; не `gxg_owner`. Пароль генерируется в Railway (AS-28 `db_password`).
- Ключ `GXG_BACKUP_KEY`: 32 байта, сгенерирован в Railway (AS-28 `backup_key`, только у этого сервиса), не печатался и не читался. Открытый пункт пилота записан в runbook: ключ и бэкапы на одном сервисе.
- Тесты: `tests/deploy` (94 passed) — спека сервиса, AS-28, роль, запрет restore на боевой сервер.

## Первый запуск и проверка (AC-M14-02)
- 08:40:07Z: `backup: gxg-approval-20261126T084007Z.dump.enc`, затем `PASS: 1 backups within 35 days` (старше 35 дней нет).
- Предыдущие попытки (08:24Z, 08:30Z) падали и файла не оставили: `User does not have CONNECT privilege` (GRANT CONNECT добавлен в `db_init.py`) и предупреждение libpq о `~/.postgresql` (`HOME=/tmp`). Файл-обрывок не создаётся (`.part` → rename).
- Для разового запуска cron-сервис временно ставился на `*/5 * * * *` и возвращён на ежедневный через `railway_deploy.py` (спека). `--verify` после очистки: см. итог в сессии.

## Восстановление (AC-M14-01)
- Временный Postgres-сервис `approval-restore-db` (postgres-ssl:16, свой Volume) создан и **удалён** после учений. Ничего из боевых сервисов (executor, api, ui…) к нему не подключалось: доступ к нему был только у `approval-backup` через временные переменные `DRILL_PG*` (удалены).
- Запуск: `scripts/ops/approval_restore_drill.py` из образа `approval-backup` (ключ и копия на его Volume): roles.sql → `CREATE DATABASE approval_restored OWNER gxg_owner` → `approval_backup.restore` (дешифровка, pg_restore, RET-01 purge) → сверка со всеми строками боевой базы, созданными до момента бэкапа (read-only роль `gxg_backup`).
- Бэкап 2026-11-26T08:40:07Z, восстановление за 0.8 с; `purged=0` (RET-01 отработал, ничего старше 30 дней нет), состояния intents после restore: `succeeded=3` (совпадают с боевой базой: 3 × succeeded).

| таблица | строк (боевая) | строк (восстановленная) | md5 id | итог |
|---|---|---|---|---|
| intents | 3 | 3 | 861782ed2f7b | совпало |
| decisions | 3 | 3 | c523e5401430 | совпало |
| executions | 3 | 3 | 139d14ab5336 | совпало |
| audit_events | 17 | 17 | 7f73cd96fa4f | совпало |

- Повторов провайдера нет: executor к восстановленной базе не подключался, провайдерских вызовов в учениях не было; терминальные intents executor не трогает (runbook §6).
- Не проверялось: полный сценарий «остановка эффектов, запуск api→sweeper→executor на восстановленной базе» (в пилоте не нужен: восстановленная база была временной).

## Открытые пункты
1. Ключ шифрования и бэкапы на одном сервисе/Volume (компромисс пилота; в runbook).
2. Копии бэкапов вне Railway нет; потеря Volume = потеря бэкапов.
3. PITR базы Railway-Postgres этим скриптом не заменяется.
