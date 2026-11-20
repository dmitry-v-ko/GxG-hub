# Модель доступа — предварительная

RBAC на tenant и agent principal. Пример для первого пилота:

| Principal | Knowledge | Telegram | Effects |
| --- | --- | --- | --- |
| Admin operator | configuration | configuration | с подтверждением |
| Hermes | read | ограниченные чаты: read | запрещены по умолчанию |
| Analyst agent | read subset | deny | deny |

Права на MCP tools в ContextForge — лишь первая граница. Права на конкретный Telegram чат, документы KB и аккаунт CRM должны дополнительно проверяться в адаптере, используя подтверждённую идентичность и scope. Не передавать универсальный токен всем агентам.

Проверки до production: tenant isolation, agent principals, forged identity, resource scope, secrets, audit redaction, effect approval, unknown-outcome retries. Точная модель ролей зависит от реально проверенных механизмов ContextForge.

Нормативные требования к identity и scopes: [MCP Connector Contract v1](../docs/specs/mcp-connector-contract-v1.md). Подписанная downstream identity propagation и защита от поддельных headers требуют реального теста, а не предположения о ContextForge. Операции с персональными чатами и эффектами не публикуются до проверки.
