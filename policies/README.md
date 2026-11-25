# Политики доступа

Нормативно: [Целевая платформа → роли и права](../docs/specs/target-platform-v1.md#роли), [Эффекты → классы и согласование](../docs/specs/effects-and-approval-v1.md#классы-эффектов-и-путь-исполнения).

Права агентов на ресурсы хранятся в `policies/tenants/<tenant>.yaml` по [модели прав на ресурсы](../docs/specs/resource-grants-v1.md). Пример: [tenants/example.yaml](tenants/example.yaml).

| Каталог | Что | Спека | Пример |
| --- | --- | --- | --- |
| `tenants/` | Grants: connections, principals, группы, права на ресурсы | [resource-grants](../docs/specs/resource-grants-v1.md) | [example.yaml](tenants/example.yaml) |
| `approval/` | Политика согласования и квоты | [approval-policy](../docs/specs/approval-policy-v1.md) | [example.yaml](approval/example.yaml) |
| `platform/` | Реестр сервисов и webhooks | [approval-service](../docs/specs/approval-service-v1.md#реестр-платформы) | [registry.example.yaml](platform/registry.example.yaml) |

Всё это собирается в один подписанный bundle (GR-11). Правила `unified_pdp` для ContextForge (tool-level RBAC, постоянный запрет `*.execute` для агентов) не входят в bundle: их читает ContextForge. Для dev-стенда M5 они лежат в [`stand/unified_pdp.rules.json`](../stand/unified_pdp.rules.json) (точный email агента × точное имя tool, иначе deny). Генерация правил для production из реестра платформы — после этапа B.

## Пример для первого пилота (tenant оператора)

| Principal | KB | Telegram | Эффекты |
| --- | --- | --- | --- |
| Оператор (`platform_admin`) | Конфигурация | Конфигурация | Не исполняет сам; может быть `approver`, если назначен явно |
| Hermes (`agent`) | `read` | `read`, только разрешённые чаты | `propose` запрещён по умолчанию |
| Analyst (`agent`) | `read`, подмножество коллекций | Нет | Нет |
| Signals Action worker (`agent`) | Нет | Нет | `propose` только для разрешённых действий; исполнение — после решения человека |
| Approver (человек) | — | — | Решения по intents своего tenant'а в Web UI |

Права на tool в ContextForge — только первая граница. Права на конкретный чат, документ или CRM-аккаунт проверяет коннектор по проверенной identity. Решение человека не расширяет права агента.

## Инварианты

- Ни одна политика не открывает агентам `*.execute`.
- Изменения политик логируются, ослабление вызывает alert.
- Новые effect tools выключены, пока не пройдены тесты этапа C.
