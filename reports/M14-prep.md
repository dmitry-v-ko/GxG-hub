# M14: подготовка без живого пилота

Ветка `claude/m14-hardening` (от `claude/m13-deploy`). Живых операций Railway не было, секретов не читали и не коммитили, критерии и пороги не менялись. PR не создан, в `main` ничего не вливалось.

## Что сделано

| № | Что | Где |
| --- | --- | --- |
| 1 | `railway_deploy.py --audit`: только запросы, все окружения проекта; нет публичных доменов, custom domains, TCP Proxy, shared-переменных; имена секретов каждого сервиса = заявленным (AS-28); сервис вне spec — FAIL | `scripts/deploy/railway_deploy.py`, `tests/deploy/test_railway_audit.py` |
| 2 | Бэкап и восстановление БД Approval: `pg_dump -Fc` → AES-256-GCM, удаление старше 35 дней по имени файла, `check` для AC-M14-02, восстановление в пустую БД и очистка RET-01 сразу после | `scripts/ops/approval_backup.py`, `approval/tests_db/test_backup.py`, `tests/deploy/test_approval_backup.py`, `docs/runbooks/approval-backup.md` |
| 3 | Cron `bundle-reissue` (каждые 6 ч, не реже 12): описан в `planned`, не включён; образ `bundle-reissue.Dockerfile`; тест dry-run и один прогон `pilot_bundle.py --deploy` на mock | `deploy/railway/services.yaml`, `tests/deploy/test_bundle_reissue.py`, `docs/runbooks/bundle-reissue.md` |
| 4 | AC-M14-09: поддельный `Tailscale-User-Login` без токена → 401/403 для `api`, `ui`, `admin`, коннектора telegram, ContextForge (стенд). AC-M14-03: тест production-конфига SSRF | `approval/tests_db/test_tailnet_identity.py`, `approval/ui/tests_db/test_edges.py`, `approval/tests/test_admin_web.py`, `stand/tests/test_sc.py`, `tests/deploy/test_ssrf_config.py` |
| 5 | Runbook'и учений: блокировка эффектов ≤ 5 мин, отзыв токена агента, перевыпуск bundle; пустой шаблон протокола | `docs/runbooks/drill-*.md`, `reports/drills/TEMPLATE.md` |
| 6 | Backlog: `check_upstream.sh` (M11 F1–F3 и minor, 4 строки backlog помечены); `max_attempts` добавлен в `approval-policy-v1.md` (код уже был) | `connectors/telegram/archive/check_upstream.sh`, тесты в `connectors/telegram/tests/test_telegram.py`, `docs/specs/approval-policy-v1.md`, `docs/backlog.md` |

Попутно: `pilot_bundle.py` берёт версию bundle из `RAILWAY_GIT_COMMIT_SHA`, если нет `.git`; `test_headers_exact` в ui приведён к решению про сессию 30 дней (после него cookie на GET не выставляется).

## Не сделано / расхождения с заданием

- **`RULES.md` в репозитории нет** (ни в этой ветке, ни в `claude/m13-deploy`). Опирался на `docs/implementation-plan-v1.md` (M14, «Облегчённый процесс»).
- **`UAID_ALLOWED_DOMAINS`**: такого имени нет ни в коде, ни в `docs/backlog.md`, ни в отчётах. Не делал, чтобы не выдумывать. Нужно определение (возможно, это относится к `gxg_agents_v2`).
- **`max_attempts`** в backlog отдельной строкой нет; нашёл в `reports/INTEGRATION-READY.md` / `M10-PENDING-OWNER.md` №7: поле в коде есть, в спеке его не было. Добавил в спеку (1…3, по умолчанию 3, берётся наименьшее, фиксируется при T1). Решение владельца всё ещё `pending_owner_decision`.

## Статус по критериям

| AC | Статус | Проверка (без стенда) | Осталось для живого стенда |
| --- | --- | --- | --- |
| AC-M14-01 | код и тест готовы, на локальном Postgres 16 `passed` | `GXG_TEST_PG_SOCKET_DIR=<сокет> uv run pytest approval/tests_db/test_backup.py` | восстановление реального бэкапа на чистый стенд Railway, сверка с провайдером (runbook §6) |
| AC-M14-02 | код и тест готовы | тот же тест (RET-01 после восстановления), `uv run pytest tests/deploy/test_approval_backup.py`; в хранилище: `approval_backup.py check --dir …` | проверка настоящего хранилища бэкапов, расписание, правило удаления в нём |
| AC-M14-03 | частично: конфиг production проверен; отказы SC-07 уже были на стенде | `uv run pytest tests/deploy/test_ssrf_config.py`; стенд: `stand/tests/test_sc.py -k sc07` | прогон SC-07 на стенде/Railway (Docker здесь нет). Шире разрешён `10.128.0.0/9` + `fd12::/16`: только сеть Railway, регистрирует только админ |
| AC-M14-04 | не затронут | — | порт-скан снаружи, `funnel status` |
| AC-M14-05 | runbook'и готовы, учений нет | — | **ручная проверка**: учения дежурным, протокол в `reports/drills/` |
| AC-M14-06 | не затронут | — | полный прогон Conformance Matrix, `stage-F.json` |
| AC-M14-07 | не затронут | — | решение оператора |
| AC-M14-08 | не затронут | — | ACL Tailscale и тесты (`policies/network/tailscale-acl.hujson` в репозитории нет) |
| AC-M14-09 | тесты `passed` для api, ui, admin, telegram; тест ContextForge написан, **не запускался** (нет Docker) | `GXG_TEST_PG_SOCKET_DIR=… uv run pytest approval/tests_db/test_tailnet_identity.py approval/ui/tests_db/test_edges.py -k tailscale approval/tests/test_admin_web.py -k tailscale`; стенд: `-k ops07` | то же из tailnet на живых узлах |
| AC-M14-10 | не затронут | — | проверка с `tag:agent` и `group:operators` |
| AC-M14-11 | `--audit` готов, `passed` на фикстурах | `uv run pytest tests/deploy/test_railway_audit.py`; живьём: `RAILWAY_TOKEN=… python scripts/deploy/railway_deploy.py --audit` | запуск на `gxg-hub-pilot`. Ожидаемо FAIL по сервисам вне spec, если они есть; `OPENROUTER_API_KEY` как shared проходит с NOTE (исключение владельца, как в `--verify`): по строгому AC его нужно убрать |

## Полный прогон

`UPSTREAM_DRIFT=skip GXG_TEST_PG_SOCKET_DIR=… scripts/ci/check-all.sh` (Postgres 16 локально, pgvector из apt, Docker нет) остановился на шаге `pytest`. Корень: **1421 passed, 11 failed, 10 errors**; все сбои в `tools/gxg-policy/tests`: `gxg-policy lint/validate policies/` не принимает `policies/pilot/*` (`unknown-file: expected tenants/, approval/ or platform/`). Это каталог из M13, мой diff `policies/` и `tools/` не трогает. Отдельно:

| Набор | Результат |
| --- | --- |
| `approval/tests_db` | 457 passed |
| `connectors/telegram` | 110 passed |
| `packages/gxg-connector-kit/tests` | 473 passed |
| `tests/deploy` | 89 passed |
| `approval/ui/tests_db`, `notifier/tests_db` | 142 passed, **2 failed** (`test_oidc::test_authorization_request`: scope `openid email` против ожидаемого `openid`; `test_screens::test_security_headers_and_cookie`) — оба из M13, в моём diff `approval/ui/src` нет |
| `schema_lock`, `railway_deploy --dry-run`, `ruff`, `mypy --strict` по затронутым | зелёные |

Не запускались: шаги `check-all` после упавшего `pytest` (`gxg-policy validate/lint`, `bundle_smoke`, gitleaks), `approval/deploy/tests` (образы, нужен Docker), E2E ui в браузере, `check_upstream.sh` на живом клоне `gxg_agents_v2` (тест на синтетическом репозитории зелёный). Эти сбои нужно решить отдельно (политики пилота и тесты ui — у M13).

## ponytail-review

Что делает изменение: добавляет режим аудита Railway, бэкап/восстановление БД, описание cron перевыпуска bundle, тесты на поддельные заголовки Tailscale, runbook'и и правки backlog. Рабочий код живёт в оффлайн-скриптах и тестах.

**Исправлено по ходу:**
1. `restore` при повреждённом файле оставлял `pg_restore` без ожидания и половину базы. Теперь процесс убивается и ждётся, в runbook написано пересоздать базу.

**Должно быть решено владельцем (не код):**
2. ~~**Токен для cron перевыпуска.**~~ Закрыто решением владельца 2026-11-24, см. «Дополнение». Прежний текст: `RAILWAY_TOKEN` проекта видит переменные всех сервисов: это ломает разделение секретов AS-28 на уровне платформы. Пока cron не включён, риска нет. Варианты в `docs/runbooks/bundle-reissue.md` (`pending_owner_decision`). Строка `railway_token` в таблице AS-28 есть только в коде, в спеку её надо внести при включении.
3. ~~`--audit` и `OPENROUTER_API_KEY`~~ — закрыто решением владельца 2026-11-24, см. «Дополнение».

**Не проверено:** `bundle-reissue.Dockerfile` не собирался (Docker нет); поле `cronSchedule` Railway API проверено только на mock; stand-тест `test_ops07_*` и правка в SC-06 написаны, но не запускались.

Verdict: ship после решения владельца по пунктам 2 и 3. Lean: -0 строк.

## Дополнение (после отчёта оркестратора, 2026-11-24)

**Решения владельца выполнены:**
- Bundle перевыпускает оркестратор каждые 10 ч; cron `bundle-reissue` не включается, описание в `planned` оставлено вариантом на будущее (`docs/runbooks/bundle-reissue.md`, `services.yaml`).
- Исключение `OPENROUTER_API_KEY` удалено из `--verify` и `--audit`: любая shared-переменная — FAIL (тесты обновлены).
- `UAID_ALLOWED_DOMAINS` пропущен по указанию оркестратора.

**Починено из M13:**
- `gxg-policy lint/validate policies/` отвергал `policies/pilot/*`. Выбран вариант с меньшим diff: загрузчик пропускает вложенный корень политик (каталог, где есть `tenants/`, `approval/` или `platform/`); `policies/pilot` проверяется своим запуском (`validate`/`lint policies/pilot` добавлены в `check-all.sh` и `ci.yml`). Пути в `pilot_bundle.py`, `railway_deploy.py` и образе не менялись; `gxg-policy` 128 passed, опечатка в имени каталога по-прежнему даёт `unknown-file` (тест).
- ui `test_oidc::test_authorization_request`: код верен (scope `openid email` нужен для `GXG_UI_OPERATORS`, коммит 050dafd), исправлено ожидание. `test_screens::test_security_headers_and_cookie`: код верен (после сессии 30 дней GET не выставляет cookie), флаги проверяются на `Set-Cookie` от `/logout`.

**Полный `check-all.sh` (Postgres 16 + pgvector локально, `UPSTREAM_DRIFT=skip`):**

| Шаг | Результат |
| --- | --- |
| lock, ruff, mypy, корневой `pytest` | 1443 passed |
| `approval/tests_db` | 457 passed |
| `approval/ui/tests_db`, `notifier/tests_db` | 144 passed |
| kit (+покрытие), demo + `check-connectors`, telegram | 473 / проход / 115 passed |
| `schema_lock`, `prove_schema_guard` (66/66), `railway_deploy --dry-run`, `gxg-policy` по `policies/` и `policies/pilot`, `bundle_smoke` | ok |
| gitleaks `tree` и `range` | no leaks |
| **`approval/deploy/tests`** | **не запущено: нужен Docker** (3 failed, 6 errors из-за отсутствия `docker.sock`) |
| `check_upstream.sh` на клоне `gxg_agents_v2` | не запущено (`UPSTREAM_DRIFT=skip`); тест на синтетическом репозитории зелёный |
| `tests/secrets` | 20 passed, **1 failed**: `test_this_repository_is_clean` ругается на `approval/tests/test_mcp_identity.py:46` (`generic-api-key`, фикстура теста). Файл в моём diff не менялся; разобрать отдельно (правило или `.gitleaksignore`) |

Шаги `check-all.sh` после Docker-шага я запускал по одному, потому что `set -e` останавливает скрипт на нём.
