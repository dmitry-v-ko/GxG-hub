# GxG Hub — целевые спецификации

Статус: **целевое решение v1 от 2026-11-20; не реализовано и не развёрнуто**.

- [Платформа v1](target-platform-v1.md) — роль ContextForge, FastMCP, каталог возможностей и границы компонентов.
- [MCP Connector Contract v1](mcp-connector-contract-v1.md) — нормативный стандарт для GxG-коннекторов и проверок совместимости.
- [MCP Toolkit v1](mcp-toolkit-v1.md) — требования к шаблону FastMCP, генератору и conformance-тестам.
- [Интеграции сервисов v1](service-integration-v1.md) — существующая база знаний, Telegram, Signals, Voice.

**Приоритет документов:** эти файлы определяют новое целевое направление GxG Hub. `docs/ARCHITECTURE.md`, `docs/POC.md` и `docs/MIGRATION.md` служат краткими обзорами и планами исполнения. Старый [Capability Hub v3.0](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/platform/capability-hub/README.md) остаётся источником для анализа миграции, но не задаёт новую архитектуру.

MUST = обязательное требование; SHOULD = предпочтительное, отклонение нужно обосновать; MAY = допускается. Все отмеченные **VERIFY** возможности сторонних систем необходимо проверить в закреплённой версии и в реальных тестах до production.

Порядок: спецификации → reference connector → тестовый набор → аудит существующей KB → PoC ContextForge → Telegram/Signals → эффекты и A2A.
