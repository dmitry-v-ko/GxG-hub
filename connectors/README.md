# MCP-коннекторы: каталог и статус

Нормативные документы: [Connector Contract v1](../docs/specs/mcp-connector-contract-v1.md), [Toolkit v1](../docs/specs/mcp-toolkit-v1.md), [Service Integration](../docs/specs/service-integration-v1.md).

| Интеграция | Целевой путь | Факт в GxG Hub |
| --- | --- | --- |
| Knowledge Base | Existing KB Access Layer MCP, возможно thin wrapper после аудита прав | **Не подключено** |
| Telegram | Новый MCP facade к существующему Telegram session/archive; сначала read-only | **Не реализован** |
| Signal Intelligence | Существующий Mastra процесс, два порта ContextForge: сбор и внешние actions | **Не интегрирован** |
| Signals facade для других агентов | Опциональные read-only `signals.search/get` | **Не реализован** |
| Voice/Oscar | Независимый realtime-сервис, потребляет разрешённые tools | **Не интегрирован** |
| MCP→A2A | Позже, для специализированных агентов-исполнителей | **Future** |

Новым Python-серверам — FastMCP; существующим TypeScript-серверам — официальный MCP SDK. `templates/`, `scripts/new-connector` и `tests/contract/` **ещё предстоит создать**. Не разрабатывать отдельную GxG MCP-runtime библиотеку. Внутренняя маршрутизация между дешёвым и дорогим методами и безопасный fallback принадлежат коннектору. Не доверять полю `agent_id` из аргументов; нужны проверенные tenant/agent/scopes в downstream.
