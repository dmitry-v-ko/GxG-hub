# MCP Connector Toolkit v1 — целевой стандарт разработки

Статус: **план/требования; генератор и MCP-интеграционный стенд ещё не созданы**. Первые JSON Schema и локальные schema smoke-тесты существуют, но не заменяют полноценные security/conformance tests.

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
approvals/                   # отдельный Approval Service + Action Gate (TODO)
docs/specs/                  # нормативные требования
```

Планируемые команды (НЕ существуют и не должны считаться работающими):

```bash
./scripts/new-connector --name telegram --lang python --kind read
./scripts/check-connectors --connector telegram --contextforge-version pinned
```

Генератор SHOULD создавать: typed input/output schema, читаемую документацию и metadata sidecar, заготовку FastMCP или TS SDK, локальный test fixture, health/readiness, structured errors, auth/scope hooks (fail-closed), bounded result/pagination, config example без секретов, Dockerfile, автоматические тесты и **отключённый** пример регистрации в ContextForge. Генератор не должен создавать реальные credentials, сам выдавать permissions или разворачивать в production.

## Действующие заготовки

- `schemas/search-result.schema.json`, `schemas/effect-intent.schema.json`, `schemas/effect-result.schema.json` — структурированные доменные envelopes (не новый MCP wire protocol).
- `tests/contract/test_envelopes.py` — schema smoke tests, запуск: `python -m pip install -r tests/contract/requirements.txt && python -m unittest discover -s tests/contract -v`.
- [Conformance Matrix](conformance-matrix-v1.md) — отдельные **пока не реализованные** end-to-end security и approval сценарии, которые должны проверяться с работающими ContextForge и fake-provider.

## Общий conformance-набор

1. MCP negotiate/discover/invoke + JSON Schema, версии.
2. Два тенанта и два разных agent principals; server-side отказ на прямой запрещённый вызов.
3. Защита от forged identity headers/arguments и чужих chat/document refs; отсутствие backend вызова до admission.
4. Bounded output, error categories, sensitive redaction.
5. Valid empty result vs safe technical fallback, no fallback at `outcome_unknown`.
6. Effects: GxG Action Gate, Approval Service, неизменный payload, проверка approver, deny/expire/replay, защита от прямого обхода коннектора, idempotency, журнал результата, отсутствие повторной отправки.
7. ContextForge integration по pinned image: identity propagation, audit, scope, reload, denial.
8. Обновления FastMCP/ContextForge проходят upgrade-compatibility tests до смены pin.

**Definition of done:** коннектор можно создать из шаблона, локально запустить, проверить и зарегистрировать вручную, **без модификации ContextForge**. Пока DoD не достигнут и коннекторы не публикуются.

## Этапы

P0. Удалить prerelease из Compose (**сделано**); проверить `1.1.1` на локальном стенде, выбрать поддерживаемый безопасный релиз, закрепить неизменяемый digest, затем доказать security gate и identity перед доступом к персональным данным.
P1. Создать минимальный read-only reference на FastMCP.
P2. Создать conformance tests и CLI-генератор.
P3. Аудит/возможный wrapper готового GxG KB MCP.
P4. Telegram read-only, Signals collection + action port.
P5. Проверить `tool_pre_invoke` в fail-closed режиме как Action Gate и его покрытие на каждом пути, реализовать отдельный Approval Service с atomic reservation и UI. Только после [security/conformance tests](conformance-matrix-v1.md) включать effects, затем A2A.

Подробный стандарт: [MCP Connector Contract](mcp-connector-contract-v1.md).
