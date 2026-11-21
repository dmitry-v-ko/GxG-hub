# gxg-approval

Approval Service ([спека](../docs/specs/approval-service-v1.md)), веха M7: модель данных, роли БД, переходы T1–T18 + T12a, движок политики и квоты, процессы `api`, `mcp`, `sweeper`, шифрование payload. Executor, UI и бот — M8, M9.

## Как устроено

| Часть | Где | Что |
| --- | --- | --- |
| Схема, роли, переходы | `migrations/versions/0001_core.py`, `deploy/roles.sql` | DDL из спеки. Каждый переход, кроме T1, — функция `SECURITY DEFINER`; роль процесса получает `EXECUTE` только на свои функции (AS-27). Ни у одной роли нет `UPDATE`/`DELETE` на `audit_events` и `DELETE` где-либо (AS-02) |
| Таблица переходов | `transitions.py` | Единственный источник: ставится в `transition_rules`; функция перехода отказывает (`conflict`) во всём, чего нет в таблице. Статус без исходящих переходов терминален |
| T1, чтение, отмена, dispatch | `intents.py` | Проверки по bundle (AS-25, AS-26, AS-08), пересчёт хеша (EFF-05), затем под `pg_advisory_xact_lock(tenant)`: идемпотентность (EFF-08), kill switch (AS-05), квоты (AP-05), политика и лимиты auto (AP-01…AP-03) |
| Политика | `policy.py` | AP-01…AP-04, чистая функция |
| Квоты | `quotas.py` | AP-05, AP-06. Алерт AP-07 — правило Prometheus на `gxg_quota_rejections_total` (ниже) |
| `api` (8090) | `api.py` | `schemas/approval-service.openapi.yaml`. Service JWT по реестру bundle, допуск по `kind` (AS-06) |
| `mcp` (8091) | `mcp.py` | `effects.status`, `effects.cancel` через `api` с `on_behalf_of` (AS-11a). Своей роли БД не использует |
| `sweeper` | `sweeper.py` | `gxg_sweep()` раз в 5 с: T5, T8, T10, T14 (`SKIP LOCKED`) |
| Шифрование | `crypto.py` | Sealed box (libsodium) на tenant: ключ `args` — только у executor, `preview` — только у ui, у `api` — открытые ключи (AS-28) |

## Развёртывание

1. DBA один раз на кластер: `psql -f deploy/roles.sql`, затем `CREATE DATABASE gxg_approval OWNER gxg_owner`, `ALTER ROLE <роль> LOGIN PASSWORD …` — пароль из secret store, у каждого процесса свой.
2. `gxg-approval migrate` от `gxg_owner` (DSN в `GXG_DB_DSN_FILE`).
3. Процессы: `gxg-approval api|sweeper` — каждый со своей ролью. Пример — `deploy/compose.yaml`; переменные и секреты — в `config.py`. Секрет никогда не значение переменной, только файл `*_FILE`.
4. `mcp` пока отказывается стартовать: production-`IdentityVerifier` (ADR-0001, вариант C) появляется в M5. Без него identity агента не проверить (SEC-03).

Готовность: `/readyz` у `api` = БД доступна и bundle валиден и свежий (GR-12, GR-13).

## Алерты

| Условие | Правило |
| --- | --- |
| AP-07: зацикленный или обманутый агент | `increase(gxg_quota_rejections_total[1h]) > 3` по `{tenant, requester}` |
| AP-02: `auto_approve` для `high_risk` проигнорировано, пустое пересечение approver'ов | лог `ALERT policy …` (WARNING) |
| Sweeper не может работать | лог `sweeper: database error` |

## Kill switch

`switches` пишет только `gxg_admin` (`admin` API — M10). Проверка — внутри транзакций T1, T7, T9 (AS-05): `global`, `tenant:<id>`, `tool:<tenant>:<tool_key>`. Строка с `blocked = false` снимает блокировку.

## Тесты

- `uv run pytest approval/tests` — без БД: политика (AC-M7-05), квоты, OpenAPI (AC-M7-01), шифрование, compose (AC-M7-13).
- `uv run pytest approval/tests_db` — настоящий PostgreSQL 16 в Docker. Контейнер без сети, тесты ходят в него через Unix-сокет (правило «нет TCP в тестах», AC-M0-01). `GXG_TEST_PG_SOCKET_DIR=<dir>` — использовать уже запущенный сервер.
- Мутации (DoD-4): `scripts/ci/mutmut.sh` (поднимает PostgreSQL на время прогона), конфиг — `[tool.mutmut]` в `pyproject.toml`.
