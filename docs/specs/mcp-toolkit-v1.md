# MCP Connector Toolkit v1

**Статус:** план. Генератор, шаблоны и интеграционный стенд не созданы. Существуют только JSON Schema в `schemas/` и smoke-тесты в `tests/contract/`.

## Цель

Новый коннектор, соответствующий [Connector Contract](mcp-connector-contract-v1.md), создаётся одной командой, проходит общий набор тестов и регистрируется в ContextForge без изменения ContextForge. Это **не** собственный MCP-фреймворк.

- **Python:** [FastMCP](https://gofastmcp.com/), версия закреплена lockfile'ом каждого коннектора.
- **TypeScript:** официальный MCP TypeScript SDK для существующих TS-сервисов. Переписывать их на Python не нужно.
- **Общая библиотека `gxg-connector-kit`** (тонкая, Python): проверка identity claims, проверка execution token, канонизация и хеш (EFF-05), клиент Approval Service, envelope ошибок, редакция логов. Только то, что иначе дублировалось бы в каждом коннекторе и где ошибка = дыра в безопасности. Для TS — эквивалентный минимальный пакет, когда появится первый TS-коннектор с эффектами.

## Что генерирует шаблон

`scripts/new-connector --name <domain> --lang python [--with-effects]`:

- FastMCP-сервер с примером `read` tool и, с `--with-effects`, парой `propose` + внутренние `execute`/`reconcile`;
- `gxg-manifest.yaml`, input/output JSON Schema;
- проверка identity (fail closed) и проверка ресурса как обязательный hook, который нельзя пропустить;
- envelope ошибок, ограниченные результаты и пагинация, `/healthz`, `/readyz`;
- fake provider для тестов (со счётчиком вызовов, режимами timeout, «ответ потерян», 429);
- Dockerfile, пример конфигурации без секретов, **выключенный** пример регистрации в ContextForge;
- подключение общего conformance-набора.

Генератор не создаёт credentials, не выдаёт права и не деплоит.

## Общий conformance-набор

`scripts/check-connectors --connector <name>` запускает тесты из [Conformance Matrix](conformance-matrix-v1.md), применимые к коннектору:

1. Контракт: manifest, схемы, имена, коды ошибок, ограниченный вывод (MC-*).
2. Identity и ресурсы: отказ без claims, с поддельными claims, при чужом tenant/ресурсе; нет вызова провайдера до проверки (SC-*).
3. Эффекты (если есть): execute без токена, с чужим `aud`, с истёкшим токеном, с повтором `jti`, с подменой args; одноразовость `dispatch`; `outcome_unknown` без повтора (EX-*).
4. Интеграция с закреплённым ContextForge на стенде (этап B и далее).

## Definition of done для toolkit

Коннектор можно создать из шаблона, запустить локально, прогнать набор тестов и вручную зарегистрировать в ContextForge, не меняя ContextForge.

## Порядок работ

См. [Roadmap](roadmap-v1.md). Кратко: reference read-only коннектор → conformance-набор → генератор → `gxg-connector-kit` с эффектами вместе с Approval Service на этапе C.
