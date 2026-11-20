# MCP Connector Toolkit v1 — целевой стандарт разработки

Статус: **план/требования; генератор, шаблоны и тесты ещё не созданы**.

## Цель

Создание нового совместимого MCP-сервера с минимальным собственным кодом, с одинаковыми тестами и правилами публикации в ContextForge. Это **не новый собственный MCP framework**.

- Python: использовать готовый [FastMCP](https://gofastmcp.com/) (version pinned по lockfile каждого сервера).
- TypeScript: допускается официальный MCP TypeScript SDK без переписывания существующих сервисов.
- Gateway: официальный ContextForge как внешняя зависимость, версия/digest закреплены; исходники ContextForge в репозиторий не копировать.
- Business logic: только в доменных адаптерах, за стандартными портами.

## Будущая структура проекта

```text
templates/python-fastmcp/     # reference server + Dockerfile + tests (TODO)
templates/typescript-mcp/     # optional TypeScript reference (TODO)
scripts/new-connector        # генератор, одна команда (TODO)
scripts/check-connectors     # прогон contract suite (TODO)
tests/contract/              # общий набор тестов (TODO)
connectors/                  # независимые MCP-серверы/подключения
policies/                    # правила доступа, без секретов
docs/specs/                  # нормативные требования
```

Планируемые команды (НЕ существуют и не должны считаться работающими):

```bash
./scripts/new-connector --name telegram --lang python --kind read
./scripts/check-connectors --connector telegram --contextforge-version pinned
```

Генератор SHOULD создавать: typed input/output schema, читаемую документацию и metadata sidecar, заготовку FastMCP или TS SDK, локальный test fixture, health/readiness, structured errors, auth/scope hooks (fail-closed), bounded result/pagination, config example без секретов, Dockerfile, автоматические тесты и **отключённый** пример регистрации в ContextForge. Генератор не должен создавать реальные credentials, сам выдавать permissions или разворачивать в production.

## Общий conformance-набор

1. MCP negotiate/discover/invoke + JSON Schema, версии.
2. Два тенанта и два разных agent principals; server-side отказ на прямой запрещённый вызов.
3. Защита от forged identity headers/arguments и чужих chat/document refs; отсутствие backend вызова до admission.
4. Bounded output, error categories, sensitive redaction.
5. Valid empty result vs safe technical fallback, no fallback at `outcome_unknown`.
6. Effects: согласование, idempotency, журнал результата, отсутствие повторной отправки.
7. ContextForge integration по pinned image: identity propagation, audit, scope, reload, denial.
8. Обновления FastMCP/ContextForge проходят upgrade-compatibility tests до смены pin.

**Definition of done:** коннектор можно создать из шаблона, локально запустить, проверить и зарегистрировать вручную, **без модификации ContextForge**. Пока DoD не достигнут и коннекторы не публикуются.

## Этапы

P0. Исправить небезопасный/устаревший образ ContextForge в Compose до запуска.
P1. Создать минимальный read-only reference на FastMCP.
P2. Создать conformance tests и CLI-генератор.
P3. Аудит/возможный wrapper готового GxG KB MCP.
P4. Telegram read-only, Signals collection + action port.
P5. Эффекты с approval, затем A2A.

Подробный стандарт: [MCP Connector Contract](mcp-connector-contract-v1.md).
