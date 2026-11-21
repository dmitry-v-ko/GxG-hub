# GxG Hub

**GxG Hub** — мультиарендная MCP-платформа доступа агентов к возможностям: KB, Telegram, Signals, CRM, внешние API. Шлюзом служит [IBM ContextForge](https://github.com/IBM/mcp-context-forge) как внешняя зависимость без форка. К нему подключаются самостоятельные доменные MCP-коннекторы. Эффекты агент только предлагает, а исполняет отдельный Effect Executor после согласования человеком.

> **Статус:** спецификации и локальный dev-стенд. **Не production.** Клиентские данные и аккаунты не подключены, эффекты не реализованы.

Зачем и что строим: [intent.md](intent.md). Как именно: [docs/specs/](docs/specs/README.md).

## Спецификации

- [Целевая платформа](docs/specs/target-platform-v1.md) — компоненты, tenancy, роли, права.
- [Security](docs/specs/security-v1.md) — identity, границы доверия, сеть, секреты, kill switch.
- [Эффекты и согласование](docs/specs/effects-and-approval-v1.md) — `propose` → решение человека (бот + Web UI) → Executor с одноразовым токеном.
- [Права на ресурсы](docs/specs/resource-grants-v1.md) — RID, grants в Git (`policies/tenants/`), подписанный bundle.
- [Политика согласования](docs/specs/approval-policy-v1.md) — авто-одобрение, кворум, запреты, квоты.
- [Approval Service](docs/specs/approval-service-v1.md) — модель данных, переходы, API, UI, бот, webhooks.
- [MCP Connector Contract](docs/specs/mcp-connector-contract-v1.md) — обязательный стандарт коннекторов.
- [MCP Toolkit](docs/specs/mcp-toolkit-v1.md) — шаблоны, генератор, общий набор тестов.
- [Reference-коннектор `demo`](docs/specs/demo-connector-v1.md) — эталон и мишень для тестов.
- [Интеграция сервисов](docs/specs/service-integration-v1.md) — KB, Telegram, Signals, Voice.
- [Conformance Matrix](docs/specs/conformance-matrix-v1.md) — тесты, которые должны пройти до включения.
- [План реализации](docs/implementation-plan-v1.md) — вехи M0–M14, зависимости, оценки, объективные критерии приёмки.
- [Roadmap](docs/specs/roadmap-v1.md) — этапы A–F, PoC, GO/NO-GO, миграция.

## Структура

- `docs/specs/` — нормативные спеки.
- `schemas/` — JSON Schema, OpenAPI внутреннего API, тестовые векторы канонизации (не замена MCP wire protocol).
- `docs/adr/` — решения, принимаемые по итогам экспериментов.
- `tests/contract/` — schema smoke-тесты (не end-to-end проверка безопасности).
- `policies/` — grants, политики согласования, реестр платформы (примеры, без секретов).
- `connectors/` — каталог коннекторов и их статус. Коннекторы пока не реализованы.
- `compose.yaml`, `stand/` — локальный dev-стенд (M5): ContextForge, PostgreSQL, `demo`.
- `gateway/contextforge/` — наш плагин ContextForge `gxg_identity` (ADR-0001, вариант C).

## Локальный стенд (только разработка)

Корневой `compose.yaml` — dev-стенд M5: ContextForge (закреплён по digest, SEC-22) на PostgreSQL, коннектор `demo` во внутренней сети, плагины `unified_pdp` и `gxg_identity` (ADR-0001, вариант C). Нужны Docker с Compose v2 и `uv`.

```bash
stand/run.sh   # секреты, сборка, запуск, настройка, тесты этапа B через ContextForge, рестарт, повтор, отчёт
```

Шаги по отдельности, сети, runbook: [stand/README.md](stand/README.md). Секреты генерирует `stand/init.py` в `.env` и `stand/secrets/`, оба пути игнорирует Git.

**Ограничения стенда:** только `127.0.0.1`, синтетические данные, ключи в файлах, а не в secret store. Не публикуйте этот стенд в интернет. Требования к production: [Security](docs/specs/security-v1.md).

## Разработка и тесты

Python-workspace на [uv](https://docs.astral.sh/uv/), Python ≥ 3.12. Пакеты:

| Путь | Пакет / модуль | Статус |
| --- | --- | --- |
| `packages/gxg-connector-kit/` | `gxg-connector-kit` / `gxg_connector_kit` | Каркас (M0), функциональность — M1 |
| `approval/` | `gxg-approval` / `gxg_approval` | Каркас (M0), реализация — M7+ |
| `tools/gxg-policy/` | `gxg-policy` / `gxg_policy` | Каркас (M0), реализация — M2 |
| `connectors/demo/` | `gxg-connector-demo` / `demo_connector` | Каркас (M0), свой `uv.lock`; реализация — M3 |

```bash
uv sync && uv run pytest          # все тесты: tests/contract, tests/ci, unit-тесты пакетов
scripts/ci/check-all.sh           # то же, что CI: ruff, mypy, тесты, schema guard, проверка секретов
```

- Тесты не ходят в сеть (сокеты блокирует `conftest.py`). `skip` и `xfail` делают прогон красным.
- Изменили нормативную часть схемы в `schemas/` — перегенерируйте lock и закоммитьте его вместе со схемой:
  `uv run python scripts/ci/schema_lock.py --write`. Ослабление `required`/`additionalProperties` требует security-ревью.
- CI (`.github/workflows/ci.yml`) отклоняет файлы `*.pem`, `*.key`, `*.p12`, `.env*` (кроме `.env.example`)
  и приватные PEM-ключи в любом коммите PR. Защита `main`: [docs/setup/branch-protection.md](docs/setup/branch-protection.md).

Только контрактные тесты, без uv:

```bash
python -m pip install -r tests/contract/requirements.txt
python -m unittest discover -s tests/contract -p 'test_*.py' -v
```

## История

Требования унаследованы из [Agent Capability Hub — Our Proposal](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/platform/capability-hub/agent-capability-hub-our-proposal.md) в GxG Agents V2. Сам GxG Agents V2 этим репозиторием не меняется.
