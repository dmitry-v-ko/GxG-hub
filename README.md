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
- [План реализации](docs/implementation-plan-v1.md) — упрощённый маршрут (вехи M0, M1, M4, M5, M7, M9, M13), инварианты, критерии приёмки, таблица отложенного.
- [Roadmap](docs/specs/roadmap-v1.md) — этапы A–F, PoC, GO/NO-GO, миграция.

## Структура

- `docs/specs/` — нормативные спеки.
- `schemas/` — JSON Schema, OpenAPI внутреннего API, тестовые векторы канонизации (не замена MCP wire protocol).
- `docs/adr/` — решения, принимаемые по итогам экспериментов.
- `tests/contract/` — schema smoke-тесты (не end-to-end проверка безопасности).
- `policies/` — grants, политики согласования, реестр платформы (примеры, без секретов).
- `connectors/` — каталог коннекторов и их статус. Коннекторы пока не реализованы.
- `compose.yaml`, `.env.example` — локальный dev-стенд ContextForge.

## Локальный запуск ContextForge (только разработка)

Нужен Docker с Compose v2.

```bash
cp .env.example .env
python3 - <<'PY'
from pathlib import Path
import secrets
p = Path(".env")
s = p.read_text()
s = s.replace("__GENERATE_JWT_SECRET__", secrets.token_urlsafe(48))
s = s.replace("__GENERATE_ENCRYPTION_SECRET__", secrets.token_urlsafe(48))
p.write_text(s)
print("Сгенерированы два локальных секрета в .env")
PY
```

Задайте в `.env` свои `PLATFORM_ADMIN_EMAIL` и сильный `PLATFORM_ADMIN_PASSWORD`, затем:

```bash
docker compose config
docker compose pull
docker compose up -d
curl -f http://127.0.0.1:4444/health
```

Админка: http://127.0.0.1:4444/admin. Остановить: `docker compose down`.

**Ограничения стенда:**
- SQLite, порт только на localhost.
- Тег `1.1.1` — кандидат для локальной проверки, а не закреплённый digest.
- Identity propagation и плагины выключены.

Не публикуйте этот стенд в интернет. Требования к production: [Security](docs/specs/security-v1.md).

## Разработка и тесты

Python-workspace на [uv](https://docs.astral.sh/uv/), Python ≥ 3.12. Пакеты:

| Путь | Пакет / модуль | Статус |
| --- | --- | --- |
| `packages/gxg-connector-kit/` | `gxg-connector-kit` / `gxg_connector_kit` | Каркас (M0), функциональность — M1 |
| `approval/` | `gxg-approval` / `gxg_approval` | Каркас (M0), реализация — M7, M9 |
| `tools/gxg-policy/` | `gxg-policy` / `gxg_policy` | Каркас (M0), реализация — M1 |
| `connectors/demo/` | `gxg-connector-demo` / `demo_connector` | Каркас (M0), свой `uv.lock`; реализация отложена (см. план) |

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
