# gxg-approval

Approval Service ([спека](../docs/specs/approval-service-v1.md)). M7: модель данных, роли БД, переходы T1–T18 + T12a, движок политики и квоты, процессы `api`, `mcp`, `sweeper`, шифрование payload. M8: процесс `executor`, JWKS и ротация ключа. Web UI и бот (M9) — отдельные deployable со своими lockfile (AS-29): [`ui/`](ui/README.md), [`notifier/`](notifier/README.md).

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
| M9: БД для ui и бота | `migrations/versions/0004_ui_notifier.py` | Коды привязки Telegram и passkey (таблиц нет в AS-27, `pending_owner_decision`); `gxg_link_code` (ui), `gxg_link_claim` (notifier); `gxg_ui` без `INSERT outbox` и прямой записи `approver_links`. T1 замораживает набор approver'ов в `allowed_approvers`, кладёт канонические args в preview-конверт (EFF-33) и поля AS-19 в строку `notify` |
| Шифрование | `crypto.py` | Sealed box (libsodium) на tenant: ключ `args` — только у executor, `preview` — только у ui, у `api` — открытые ключи (AS-28) |
| `executor` (M8) | `executor.py` | Outbox (`execute`, `reconcile`) `FOR UPDATE SKIP LOCKED` → T7 `gxg_reserve` → execution token (kit `exec_token.sign`) → `POST /internal/v1/execute/<tool>` коннектора с service JWT, таймаут = `dispatch_timeout_s` (AS-13) → `gxg_complete`. Ответ не 200 или обрыв: execution ещё `reserved` → T12a (попытка засчитана), уже `dispatching` → T13. `outcome_unknown` → `reconcile` коннектора; `unknown` → повтор через время, уже прошедшее с dispatch (5 с … 1 ч), после 24 ч — `ALERT` и ручной разбор (T17, T18) |
| Ключи executor'а (M8) | `keys.py`, `config.JsonFile` | `gxg-approval keys rotate` / `keys remove --kid K` / `keys release` (ниже). `api` перечитывает JWKS-файл при изменении; `dispatch` проверяет токен по текущему файлу |
| AS-10 | `migrations/versions/0002_executor.py` | `gxg_release_reserved()`: при удалении скомпрометированного ключа все `reserved` исполнения → `released`, intent → `approved` (ход T10 без ожидания lease) |

## Развёртывание

1. DBA один раз на кластер: `psql -f deploy/roles.sql`, затем `CREATE DATABASE gxg_approval OWNER gxg_owner`, `ALTER ROLE <роль> LOGIN PASSWORD …` — пароль из secret store, у каждого процесса свой.
2. `gxg-approval migrate` от `gxg_owner` (DSN в `GXG_DB_DSN_FILE`).
3. Процессы: `gxg-approval api|sweeper|executor` — каждый со своей ролью. Пример — `deploy/compose.yaml`; переменные и секреты — в `config.py`. Секрет никогда не значение переменной, только файл `*_FILE`.
4. `mcp` пока отказывается стартовать: production-`IdentityVerifier` (ADR-0001, вариант C) появляется в M5. Без него identity агента не проверить (SEC-03).

Готовность: `/readyz` у `api` = БД доступна и bundle валиден и свежий (GR-12, GR-13). `executor` порта не имеет: без активного ключа подписи или валидного bundle он не стартует; probes воркеров — бэклог M7 (M10/M14).

## Executor (M8)

Переменные: `GXG_DB_DSN_FILE` (роль `gxg_executor`), `GXG_SERVICE_ID` (id в реестре, по умолчанию `executor`), `GXG_SVC_KEY_FILE` (ключ service JWT к коннекторам), `GXG_EXEC_SIGNING_KEYS_FILE` (ключи execution token), `GXG_PAYLOAD_PRIVATE_KEYS_FILE` (`{"<tenant>": "<base64 X25519>"}`, только `args`), bundle (`GXG_BUNDLE_*`), `GXG_POLL_S`. Экземпляров может быть сколько угодно: координация только через БД (EFF-16).

Kill switch, включённый после reserve, срабатывает на `dispatch` (423, T12a); пока он включён, T7 отказывает, а строка outbox ждёт с backoff 5 с · 2ⁿ (≤ 5 мин).

### Ротация ключа (AS-10, SEC-10: не реже раза в 90 дней)

Файлы (`deploy/compose.yaml`): ключи executor'а — `deploy/exec-keys/signing-keys.json` (каталог 0700, монтируется только в `executor`, AS-28; каталог, а не одиночный secret-файл: атомарную замену файла executor видит без рестарта), JWKS — `deploy/config/exec-jwks.json` (публичный, у `api`). `rotate` и `remove` запускает оператор **на хосте** с `GXG_EXEC_SIGNING_KEYS_FILE=deploy/exec-keys/signing-keys.json GXG_EXEC_JWKS_FILE=deploy/config/exec-jwks.json`; `release` — **в контейнере executor'а** (его роль БД): `docker compose exec executor gxg-approval keys release`.

| Шаг | Команда | Что происходит |
| --- | --- | --- |
| 1 | хост: `gxg-approval keys rotate` | Новый ключ сразу в JWKS, в файле executor'а — с `not_before = now + 15 мин`. `api` отдаёт его без рестарта, коннекторы видят ≤ 10 мин (кеш AS-09) |
| 2 | — | Через 15 мин executor подписывает новым ключом (выбор по `not_before`, файл перечитывается на ходу) |
| 3 | через 24 ч, хост: `gxg-approval keys remove --kid <старый>` | Старый ключ уходит из JWKS и файла executor'а |
| Компрометация | хост: `gxg-approval keys remove --kid <kid>`, затем `docker compose exec executor gxg-approval keys release` | `dispatch` отказывает токенам ключа сразу, коннекторы — ≤ 10 мин; все `reserved` исполнения освобождаются (T10 без ожидания lease). Если активного ключа не осталось, executor не резервирует (лог `WARNING executor: no active signing key …`): строки outbox ждут с backoff, попытки не тратятся, intents остаются `approved` до появления ключа (`rotate`, +15 мин) или до конца окна исполнения (T8). Поэтому держите опубликованный запасной ключ (шаг 1 заранее) |

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
- `uv run pytest approval/tests_db` — настоящий PostgreSQL 16 в Docker. `test_executor.py` (M8, AC-M8-01…12, 14): `api`, коннектор `demo` и executors по HTTP на Unix-сокетах (`effects_stack.py`), `demo` для AC-M8-07 — отдельным процессом, который тест убивает. Контейнер без сети, тесты ходят в него через Unix-сокет (правило «нет TCP в тестах», AC-M0-01). `GXG_TEST_PG_SOCKET_DIR=<dir>` — использовать уже запущенный сервер.
- Мутации (DoD-4): `scripts/ci/mutmut.sh` (поднимает PostgreSQL на время прогона), конфиг — `[tool.mutmut]` в `pyproject.toml`.
