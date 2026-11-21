# GxG Hub

**GxG Hub** — минимальная мультиагентная платформа: [IBM ContextForge](https://github.com/IBM/mcp-context-forge) как внешний MCP Gateway и отдельные предметные MCP-коннекторы. Никакого форка ContextForge и переписывания существующих сервисов GxG Agents V2.

> **Статус:** стартовый каркас / локальный proof of concept, **не production**. Никакие клиентские данные или аккаунты пока не подключены.
>
> **Контекст всей системы:** [отдельная папка системной архитектуры](system-architecture/README.md) — место GxG Hub среди других компонентов и границы ответственности. Здесь описывается только самостоятельный сервис GxG Hub.

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
- [Security & Action Gate](docs/specs/security-and-action-gate-v1.md) — защита от чужих вызовов, подделки идентичности и обхода коннектора.
- [Approval State Machine](docs/specs/approval-state-machine-v1.md) — проверенный человек, однократное резервирование и восстановление после сбоя.
- [Conformance Matrix](docs/specs/conformance-matrix-v1.md) — критерии security/contract тестирования.
- [Human approvals](docs/APPROVALS.md) — краткий обзор.
- [Connector integration profile](docs/MCP_CONNECTOR_CONTRACT.md) и [Toolkit implementation plan](docs/CONNECTOR_TOOLKIT.md) — практические дополнения к нормативным документам в `docs/specs/`.

**Security stop:** старый `1.0.0-RC-3` удалён из `compose.yaml` и `.env.example`. Пример теперь задаёт `1.1.1` как **кандидат для локальной проверки** известных исправлений IBM по состоянию на 20.11.2026. Тег **не** равен неизменяемому digest и не является подтверждением безопасности: перед общим PoC или production проверяйте новые advisories, закрепляйте digest и подтверждайте identity, RBAC, resource scope, tenant isolation и Action Gate. В dev Compose `IDENTITY_PROPAGATION_ENABLED=false` и `PLUGINS_ENABLED=false`; никакие реальные эффектные инструменты нельзя открывать до end-to-end тестов.

## Структура

- `compose.yaml` — локальный **dev-only** запуск ContextForge (официальный OCI-образ, без изменений исходников).
- `.env.example` — шаблон переменных. Реальный `.env` не коммитить.
- `docs/ARCHITECTURE.md` — краткий обзор целевой архитектуры.
- `docs/specs/security-and-action-gate-v1.md` — нормативная security-модель.
- `schemas/` — примеры машиночитаемых контрактов результатов и намерений.
- `tests/contract/` — локальные тесты JSON Schema (не end-to-end проверка ContextForge).
- `docs/POC.md` — критерии проверки, включая два тенанта и два агента.
- `docs/MIGRATION.md` — постепенное подключение существующих сервисов GXG.
- `connectors/README.md` — будущие доменные MCP-серверы; пока не реализованы.
- `policies/README.md` — схема политики и важные security-инварианты.

## Human approval (целевая архитектура)

**GxG Action Gate** применяет политику перед эффектными вызовами. Если нужно решение человека, открывает запрос в отдельном **GxG Approval Service**, а не ждёт человека внутри MCP-вызова. Подтверждение действительно только для точного действия, получателя, аргументов и срока; коннектор дополнительно проверяет права и запрет обхода. До проверки end-to-end внешние эффекты остаются выключены. Подробнее: [APPROVALS.md](docs/APPROVALS.md).

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

Для локальной проверки пример использует тег `1.1.1`, но неизменяемый digest **ещё не закреплён**. Это не production-конфигурация. Плагины, передача identity и effect tools должны включаться только отдельным контролируемым этапом после проверки security-тестов.

## Действующая архитектурная история

Целевой документ в старом репозитории: [Agent Capability Hub — Our Proposal](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/platform/capability-hub/agent-capability-hub-our-proposal.md). Существующий [Capability Hub v3.0](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/platform/capability-hub/README.md) остаётся нетронутым. Никаких миграций или отключений в GxG Agents V2 этим коммитом не выполняется.

Upstream: [официальный Quick Start ContextForge](https://ibm.github.io/mcp-context-forge/latest/overview/quick_start/).
