# GxG Hub

**GxG Hub** — минимальная мультиагентная платформа: [IBM ContextForge](https://github.com/IBM/mcp-context-forge) как внешний MCP Gateway и отдельные предметные MCP-коннекторы. Никакого форка ContextForge и переписывания существующих сервисов GxG Agents V2.

> **Статус:** стартовый каркас / локальный proof of concept, **не production**. Никакие клиентские данные или аккаунты пока не подключены.

## Зачем

- Централизованно публиковать MCP-инструменты и давать разным агентам разные права.
- Не дублировать базы знаний, Telegram-историю, сигналы и голосовой сервис.
- Скрывать внутри доменных коннекторов выбор API/провайдера и безопасный технический fallback.
- Сохранять границы: Hub — доступ и каталог; агент — оценка результата; оркестратор — бизнес-задачи.

## Целевая спецификация (2026-11-20)

- [Целевая платформа](docs/specs/target-platform-v1.md) — ContextForge как внешняя зависимость, независимые коннекторы, права, fallback.
- [MCP Connector Contract v1](docs/specs/mcp-connector-contract-v1.md) — стандартизованные tools, identity, authorization, results, errors, effects.
- [MCP Toolkit v1](docs/specs/mcp-toolkit-v1.md) — FastMCP/TypeScript SDK, будущий генератор и conformance-тесты.
- [KB / Signals / Telegram / Voice](docs/specs/service-integration-v1.md) — существующие сервисы и особый двусторонний путь Signals.

**Security stop:** имеющиеся `compose.yaml` и `.env.example` всё ещё указывают на prerelease ContextForge `1.0.0-RC-3`. **Не запускайте его с личными данными, в мультиарендной среде или в production.** Перед PoC выберите проверенный исправленный образ (IBM рекомендует минимум v1.1.1 для известных на 20.11.2026 проблем), закрепите digest и подтвердите identity, RBAC, resource scope и межтенантную изоляцию. Этот этап изменяет только документацию, не конфигурацию и не runtime.

## Структура

- `compose.yaml` — локальный **dev-only** запуск ContextForge (официальный OCI-образ, без изменений исходников).
- `.env.example` — шаблон переменных. Реальный `.env` не коммитить.
- `docs/ARCHITECTURE.md` — целевая архитектура и границы ответственности.
- `docs/POC.md` — критерии проверки, включая два тенанта и два агента.
- `docs/MIGRATION.md` — постепенное подключение существующих сервисов GXG.
- `connectors/README.md` — будущие доменные MCP-серверы; пока не реализованы.
- `policies/README.md` — схема политики и важные security-инварианты.

## Локальный запуск (только разработка)

Требуются Docker Engine / Docker Desktop с Compose v2.

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

**Перед запуском** установите в `.env` свой сильный `PLATFORM_ADMIN_PASSWORD` и актуальный `PLATFORM_ADMIN_EMAIL`. Не используйте пример как пароль. Затем:

```bash
docker compose config
docker compose pull
docker compose up -d
docker compose ps
curl -f http://127.0.0.1:4444/health
```

Локальная админка: http://127.0.0.1:4444/admin

Остановить:

```bash
docker compose down
```

**Важно:** здесь SQLite и порт, доступный только с локальной машины. Для SaaS/production нужны отдельные PostgreSQL, HTTPS, управление секретами и ключами, проверка политик, закрепление проверенного digest образа, бэкапы, обновления и тесты изоляции. Не публикуйте этот Compose в интернет.

Образ ContextForge закреплён на документированном релизном теге для начальной проверки; перед развёртыванием проверьте актуальную поддерживаемую версию и совместимость схем.

## Действующая архитектурная история

Целевой документ в старом репозитории: [Agent Capability Hub — Our Proposal](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/platform/capability-hub/agent-capability-hub-our-proposal.md). Существующий [Capability Hub v3.0](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/platform/capability-hub/README.md) остаётся нетронутым. Никаких миграций или отключений в GxG Agents V2 этим коммитом не выполняется.

Upstream: [официальный Quick Start ContextForge](https://ibm.github.io/mcp-context-forge/latest/overview/quick_start/).
