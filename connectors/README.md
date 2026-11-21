# MCP-коннекторы: каталог и статус

Нормативно: [Connector Contract](../docs/specs/mcp-connector-contract-v1.md), [Toolkit](../docs/specs/mcp-toolkit-v1.md), [Интеграция сервисов](../docs/specs/service-integration-v1.md).

| Интеграция | Целевой путь | Этап | Статус |
| --- | --- | --- | --- |
| Reference (`demo`) | Синтетический FastMCP-коннектор: read + `propose` на fake provider ([спека](../docs/specs/demo-connector-v1.md)) | B, C | Read-only локально (M3): 3 tools, тестовый issuer, Docker ([README](demo/README.md)); `propose` — M8 |
| Knowledge Base | Существующий KB MCP после gap-аудита; при необходимости тонкий wrapper | B | Не подключено |
| Telegram | Отдельный сервис-коннектор, чтение архива `telegram_archive` существующего сервиса ролью только на `SELECT` ([ADR-0004](../docs/adr/0004-telegram-connector-service.md)) | D, E | Read-only (M11): 3 tools, на стенде через ContextForge ([README](telegram/README.md)); live-операции и `propose` — после решения по сессии |
| Signal Intelligence | Существующий Mastra-процесс; Hub на сборе и на `propose` | D | Не интегрирован |
| `signals.search/get` | Опциональный read-only фасад для других агентов | — | Не планируется до запроса |
| Voice / Oscar | Отдельный realtime-сервис, потребитель tools | D | Не интегрирован |
| CRM | `read` + `propose_update` | После F | Не выбрана |
| MCP→A2A | Bridge к агентам-исполнителям | После F | Future |

Каждый коннектор — отдельный каталог со своим `gxg-manifest.yaml`, lockfile, Dockerfile и тестами. Собственный MCP-фреймворк не пишем: Python — FastMCP, TypeScript — официальный SDK.

Коннекторы не делают свой approval UI и не верят `approved: true` в аргументах. Эффекты они исполняют только через внутренний `execute` по execution token от Effect Executor.
