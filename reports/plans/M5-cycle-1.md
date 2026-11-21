# M5 — цикл 1: план (ponytail ultra)

Веха: [M5 — read-only PoC через ContextForge](../../docs/implementation-plan-v1.md#m5--read-only-poc-через-contextforge), AC-M5-01…05. Порог 9 («остальные вехи»).
База: `claude/integration` + `claude/m3-demo-read` + `claude/m4.1-gateway-comparison` + plan/backlog/GOAL из `claude/admiring-planck-vzys0x`.

## Что найдено при слиянии

`claude/m3-demo-read` синхронизировал спеки с ветки плана, где правок M4 не было. При слиянии это откатило SEC-02, SEC-03, TEN-01 и формат principal к тексту с VERIFY. Восстановлено дословно из `claude/integration` (коммит `c54346b`). ADR-0002 на ветке M4.1 — `Proposed`. Владелец принял его 2026-11-21 (`reports/progress.md` ветки плана, коммит `3f4c736`): ставлю статус `Accepted` со ссылкой на это решение.

## Вопросы к ultra: что из задания не нужно

| Пункт | Решение |
| --- | --- |
| Отдельный стенд рядом с корневым `compose.yaml` | Не нужен. Корневой `compose.yaml` (SQLite, плагины выключены) заменяется стендом M5. Два dev-стенда — два места, где расходится конфигурация |
| Свой PDP или OPA/Cedar sidecar | Не нужен. Встроенный в образ `plugins/unified_pdp`, движок `native`, `default_decision: deny` |
| Хранилище `jti` на несколько реплик | Не нужно на стенде (одна реплика). `shortcut` в коде, upgrade — при второй реплике |
| Secret store для ключа адаптера | Не нужен на dev-стенде: ключ генерирует bootstrap в git-ignored `stand/secrets/`. `shortcut` |
| Перенос плагина на `extensions.http.headers` | Нельзя на v1.1.1 (ADR-0001). Остаётся `headers` |
| Реестр платформы как источник team → tenant | Нет сервиса реестра. Конфиг плагина генерирует bootstrap из созданных team. `shortcut` |
| MC-03 «после отправки» для read | Для `read` нет эффекта. «После отправки» = таймаут tool'а у ContextForge при медленном backend: проверяем код ошибки и отсутствие ответа-успеха |

## Места, которых касается изменение

1. `packages/gxg-connector-kit/src/gxg_connector_kit/cf_identity.py` (новый): `ContextForgeVerifier` — `IdentityVerifier` для `X-GxG-Identity` по SEC-03.
2. `packages/gxg-connector-kit/src/gxg_connector_kit/identity.py`: протокол `IdentityVerifier.verify(headers, *, tool, arguments)`. Привязку `tool` и `ahs` (SEC-03, MUST) нельзя проверить по одним заголовкам. `StubVerifier` принимает и игнорирует новые аргументы. Аргументы идут только в сравнение хеша и никогда не становятся identity (MCC-02).
3. `connectors/demo/src/demo_connector/tools.py`: передать `tool` и `arguments` верификатору. `server.py`: `DEMO_IDENTITY_KEYS` → `ContextForgeVerifier`, иначе `NoVerifier`, как сейчас.
4. `connectors/demo/src/demo_connector/{backend.py,testctl.py}`: тестовый режим отказа backend'а (`rate_limited`, `temporarily_unavailable`, `timeout_before_start`, `slow:<ms>`) и `GET /_test/backend/calls` (счётчик для «ноль обращений», SC-04/SC-10/MC-02). Только тестовый профиль, в production-образе модуля нет (AC-M3-11 не меняется).
5. `gateway/contextforge/gxg_identity/`: плагин из спайка в пакет. Чистая логика решения и claims (`core.py`, без `cpex`, с unit-тестами и mutmut) + тонкий адаптер `plugin.py`. JWS `typ` = `gxg-identity+jwt`: kit проверяет `typ`, и execution token не спутать с identity.
6. `gateway/contextforge/unified_pdp.rules.json`: правила как код. Allow — точный email агента × точное имя tool. Deny — `tools.invoke.*execute*` для всех. Остальное — deny по умолчанию.
7. `compose.yaml` (корень): ContextForge (digest SEC-22) + PostgreSQL 18 + `demo` (образ из `connectors/demo/Dockerfile`, тестовый профиль только с `DEMO_TEST_CONTROLS`). Сети: `edge` (агенты ↔ ContextForge), `connectors` (`internal: true`, ContextForge ↔ demo), `db` (`internal`). Порт есть только у ContextForge: `127.0.0.1:4444`. `restart: unless-stopped`, тома для PostgreSQL, bundle, конфигурации плагина.
8. `stand/`: `gen_env.sh` (секреты в git-ignored `.env` и `secrets/`), `bootstrap.py` (идемпотентно: team, агенты, токены, gateway `demo` в обоих team, virtual servers, алиас, REST-tool `*.execute` для проверки deny, конфиг плагина → рестарт), `run.sh` (up → bootstrap → тесты → рестарт всех контейнеров → те же тесты → отчёт), `README.md` (runbook).
9. `stand/tests/` — живые тесты SC-01…SC-10, MC-01…MC-04 через ContextForge и пробы из сетей стенда (контейнер в `edge` = «хост агента», в `connectors` = «хост внутри сети»). Не в корневых `testpaths`: им нужна сеть, как `tests/secrets`.
10. `.github/workflows/ci.yml`: job `m5-stand` (`stand/run.sh`), он входит в `ci-ok`.
11. `scripts/ci/mutmut.sh` и `pyproject.toml`: mutmut по `cf_identity.py` и `gxg_identity/core.py`.
12. Документы: `docs/specs/roadmap-v1.md` (GO/NO-GO), ADR-0002 (статус), ADR-0001 («что дальше»: verifier в ките сделан), `docs/specs/security-v1.md` SEC-22 («одобрен для этапа B» — только если GO), `policies/README.md` (где правила `unified_pdp`), `README.md` (запуск стенда), `reports/conformance/M5.json`.

## Лестница по частям

| Часть | Ступень | Почему |
| --- | --- | --- |
| Проверка JWS | 2: `_jwt.verify` кита (PyJWT, `typ`, `kid`, iss/aud/iat/exp/ttl, `JtiCache`) | Своё — только `tool`, `ahs`, `sub` в tenant, разбор ключей |
| Разбор ключей `kid=ed25519:…` | 2: `parse_keys` в `demo_connector.server` (уже для bundle) | Переиспользуем |
| Канонический JSON для `ahs` | 5: одна строка `json.dumps(sort_keys, separators, ensure_ascii=False)` по SEC-03 | `canonical.py` кита — JCS+NFC (EFF-05), это другое определение. Перепутать нельзя: разные хеши |
| Политика tools | 4: `unified_pdp` из образа ContextForge | Готовый PDP с deny by default |
| Подпись в плагине | 4: PyJWT из образа | Как в M4 |
| Сетевая изоляция | 3: сети Docker (`internal: true`), без портов у `demo` | Платформенная возможность |
| Рестарт и восстановление | 3: `restart: unless-stopped`, тома | — |
| HTTP/MCP-клиент тестов | 4: `httpx` и SDK `mcp` (уже в lockfile), urllib для проб внутри контейнера (stdlib, образ python-slim без зависимостей) | — |
| Bootstrap | 6: минимальный скрипт поверх REST ContextForge | Конфигурации как кода (import) для teams/users/tokens в v1.1.1 нет: `/admin/import` переносит gateway/tools, но не пользователей и токены. Проверить на стенде, иначе `shortcut` |
| Выпуск просроченного токена для SC-02 | 4: PyJWT + `JWT_SECRET_KEY` стенда | ContextForge не выпускает токен с прошедшим `exp` |

## DoD-6: готовые решения для своего кода

| Компонент | Рассмотрено | Решение |
| --- | --- | --- |
| `ContextForgeVerifier` | joserfc, authlib, PyJWT напрямую, `_jwt.verify` кита | `_jwt.verify` (уже PyJWT): единые правила для всех JWS кита. Своё — привязка к вызову, которой нет ни в одной библиотеке |
| Плагин `gxg_identity` | встроенное identity propagation ContextForge (вариант A, отклонён ADR-0001), `jwt_claims_extraction`, `header_injector` из образа | Ни один не подписывает асимметрично и не привязывает к tool/аргументам. Свой, из спайка M4 |
| Правила tools | `unified_pdp` native/OPA/Cedar, RBAC ContextForge | `unified_pdp` native. OPA/Cedar требуют sidecar без выигрыша для allowlist |
| Bootstrap стенда | `/admin/import`, `mcpgateway` CLI, Terraform-провайдера нет | REST-скрипт. Проверить `/admin/import` на стенде, записать результат |
| Пробы сети | `nc`/`curl` в образе, `docker run --network` | `docker run` с пинованным `python:3.12-slim` и stdlib-скриптом: в образе нет curl |
| Тестовые отказы backend | toxiproxy, fault-injection плагины ContextForge | Режим в `testctl` (спека demo уже задаёт тестовые режимы fake provider). Toxiproxy ломает транспорт, а коды `rate_limited`/`timeout_before_start` рождаются в коннекторе |

## Как проверяются AC

| AC | Тест |
| --- | --- |
| AC-M5-01 | `stand/run.sh`: pytest `stand/tests` (SC-01…SC-10, MC-01…MC-04), отчёт `reports/conformance/M5.json` |
| AC-M5-02 | `test_sc01_*`: `tools/list` на `/rpc` и virtual server каждого агента; вызов tool'а `t-beta` по имени на `/rpc`, `/mcp`, virtual server, алиасе — отказ, коннектор не получил вызов |
| AC-M5-03 | `test_sc06_*`: проба из сети `edge` (и с хоста по IP контейнера) — соединения нет; проба из сети `connectors` без identity — `unauthenticated` |
| AC-M5-04 | `run.sh`: `docker compose restart`, затем `docker compose down && up` без `-v`, ожидание health, тот же набор тестов без bootstrap |
| AC-M5-05 | `docs/specs/roadmap-v1.md`: решение, дата, ссылка на отчёт |

Условие 4 ADR-0002 (`unified_pdp` deny by default на всех путях): агент `t-alpha:nobody` имеет allow только на `demo.boards.list`. Вызов остальных tools отклоняется `PDP_DENY` на `/rpc`, virtual server и алиасе. Tool `*.execute` отклоняется для всех агентов. Неизвестный правилам агент или tool — deny. Если хотя бы один путь пропускает вызов, это NO-GO по условию 4 и эскалация (ToolHive — запасной вариант, сам не перехожу).

## Агенты стенда

`t-alpha`: `reader` (grant на `a-public`, `a-sales`), `nobody` (без grant'ов). `t-beta`: `reader` (все доски), `nobody`. Email `<name>@<tenant>.agents.gxg.example.com`, principal `agent:<tenant>:<name>` (resource-grants-v1). Grants — fixtures `demo` без правок. `agent:t-beta:nobody` в fixtures не объявлен: у неизвестного principal'а нет grant'ов. Это проверяется.

## Что сознательно не делается

- MC-05 (апгрейд ContextForge) — этап C.
- `resources/read`, `prompts/get` — у `demo` их нет. Коннектор закрыт по умолчанию (SEC-03).
- TLS на стенде — только `127.0.0.1`, dev.
