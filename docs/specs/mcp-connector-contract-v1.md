# GxG MCP Connector Contract v1

**Статус:** единственный нормативный контракт MCP-коннекторов GxG Hub. Реализация и прохождение тестов не подтверждены. 2026-11-20.

MCP — протокол обнаружения и вызова. Этот документ — **профиль GxG поверх MCP**: требования к именам, схемам, identity, результатам, ошибкам, эффектам и тестам. Нестандартные обязательные поля в MCP wire protocol не добавляем. Security context передаётся только доверенным каналом ([SEC-03](security-v1.md#identity)).

## Словарь

### Имена tools

`<domain>.<object>.<verb>`, нижний регистр, точки как разделители: `kb.documents.search`, `telegram.messages.search`, `telegram.message.propose`, `crm.contact.propose_update`.

Глаголы с особым смыслом:

| Глагол | Смысл |
| --- | --- |
| `search`, `get`, `list`, `read`, `collect` | `read` |
| `propose`, `propose_<action>` | Создаёт intent эффекта ([Эффекты](effects-and-approval-v1.md)) |
| `execute`, `reconcile` | Внутренние операции, вызываются только Executor'ом и **никогда не публикуются в ContextForge** |

### Effect class

`read`, `write_reversible`, `external_effect`, `high_risk`. Определения и путь исполнения — в [Эффекты и согласование](effects-and-approval-v1.md#классы-эффектов-и-путь-исполнения).

### Коды ошибок

| Код | Когда | Повторять? |
| --- | --- | --- |
| `invalid_input` | Аргументы не прошли схему или доменную валидацию | Нет |
| `unsupported_version` | Неподдерживаемая версия tool'а или контракта | Нет |
| `unauthenticated` | Нет или невалидна identity | Нет |
| `permission_denied` | Нет права на tool или операцию | Нет |
| `resource_forbidden` | Нет права на конкретный чат, документ или аккаунт | Нет |
| `not_found` | Ресурс не существует **или** скрыт правами (не различаем, чтобы не раскрывать существование) | Нет |
| `conflict` | Нарушено предусловие: версия записи, повтор `idempotency_key` с другими параметрами | Нет, нужен новый запрос |
| `effect_disabled` | Эффект выключен: kill switch, tool не включён | Нет |
| `rate_limited` | Лимит Hub или провайдера (flood-wait); `retry_after_s` в деталях | Да, после паузы |
| `temporarily_unavailable` | Backend или провайдер временно недоступен, запрос **не** выполнен | Да, для `read` |
| `timeout_before_start` | Таймаут с доказательством, что провайдер не получил запрос | Да |
| `outcome_unknown` | Исход эффекта неизвестен | **Никогда автоматически**, только reconciliation |
| `internal_error` | Ошибка коннектора | Для `read` — да, ограниченно |

Прежние коды `forbidden`, `unknown_outcome`, `timeout`, `approval_required`, `approval_rejected`, `approval_expired` не используются. Состояния согласования — это статусы intent'а, а не ошибки.

### Outcome эффекта

`succeeded`, `failed_before_effect`, `outcome_unknown` ([effect-result.schema.json](../../schemas/effect-result.schema.json)).

## Требования

**MCC-01 — Discovery, версии, manifest.**
- Каждый tool MUST иметь стабильное уникальное имя, описание, типизированную `inputSchema` и, если это совместимо с закреплённой версией MCP SDK, `outputSchema`.
- Коннектор MUST иметь manifest (`gxg-manifest.yaml`) со списком tools. Для каждого tool указываются версия (semver), effect class, approval policy по умолчанию, лимиты и ссылки на схемы.
- Breaking change — новая major-версия tool'а. Старая версия поддерживается до явного вывода.

**MCC-02 — Identity.**
- Каждый вызов MUST быть привязан к проверенным tenant и principal, полученным доверенным каналом ([SEC-03](security-v1.md#identity)).
- Аргументы `agent_id`, `tenant_id`, `approved`, `approval_id`, `user` и любые заголовки от клиента полномочий не дают.
- Без валидных claims — `unauthenticated`.

**MCC-03 — Доступ к ресурсам.**
- Коннектор MUST проверить право на конкретный ресурс **до** получения provider credentials и обращения к провайдеру.
- Credentials выбираются по проверенному tenant и connection.
- Нет token passthrough, нет общих credentials между tenant'ами.

**MCC-04 — Результаты.**
- Типизированный результат передаётся в `structuredContent` (с дублем в `content` для старых клиентов).
- Поиск MUST соответствовать [search-result.schema.json](../../schemas/search-result.schema.json) или документированной совместимой схеме: ограниченный размер, `next_cursor`, `complete`, `partial_reasons`, `source_refs`.
- Пустой результат — успех, а не ошибка.
- Секреты и внутренние идентификаторы провайдера не возвращаются.

**MCC-05 — Ошибки.**
- Коды из словаря выше передаются в стабильном машиночитаемом виде. Tool-ошибки возвращаются как MCP tool error (`isError: true`) со `structuredContent` `{code, message, retryable, details?}`. Протокольные ошибки MCP используются только для протокольных проблем.
- `timeout_before_start` — только при доказательстве отсутствия запроса к провайдеру.
- `outcome_unknown` никогда не помечается как `retryable`.

**MCC-06 — Маршрутизация и fallback.**
- Внутри коннектора допустим технический fallback на другой метод или провайдера при `temporarily_unavailable` / `timeout_before_start` / `rate_limited`, но только для `read` и для эффектов до `dispatch`.
- Fallback MUST NOT расширять права, запускать дорогой поиск при валидном пустом результате или повторять эффект после `outcome_unknown`.

**MCC-07 — Эффекты.**
- Tools с классом `external_effect` и `high_risk` публикуются только как `propose`.
- Коннектор реализует внутренние `execute` и `reconcile` по [Эффекты и согласование](effects-and-approval-v1.md) (EFF-04…EFF-08, EFF-18, EFF-19, EFF-25).
- `write_reversible` вызывается напрямую, но требует `idempotency_key`, проверки ресурса и аудита.

**MCC-08 — Надёжность, стоимость, аудит.**
- Конечные таймауты, лимиты размера ответа и батча, rate/cost limits, учёт flood-wait провайдера.
- Корреляция `request_id`, `intent_id`, `business_task_id` во всех логах.
- Редакция credentials, текстов сообщений и персональных данных ([SEC-18](security-v1.md#аудит)).

**MCC-09 — Health.**
- `GET /healthz` (жив) и `GET /readyz` (готов: backend, Approval Service, JWKS доступны). Без раскрытия конфигурации.

**MCC-10 — Публикация.**
- Коннектор регистрируется в ContextForge только после прохождения тестов соответствующего этапа [Conformance Matrix](conformance-matrix-v1.md).
- Effect tools регистрируются выключенными ([SEC-21](security-v1.md#аварийные-выключатели)).

## Пример manifest

```yaml
connector: telegram
version: 1.0.0
contract: gxg-mcp-connector/v1
tools:
  - name: telegram.messages.search
    version: 1.0.0
    effect_class: read
    input_schema: schemas/messages-search.input.json
    output_schema: ../../schemas/search-result.schema.json
    limits: { max_results: 50, timeout_s: 20 }
  - name: telegram.message.propose
    version: 1.0.0
    effect_class: external_effect
    approval_default: required
    executes_via: telegram.message.execute   # внутренний, не публикуется
    input_schema: schemas/message-send.input.json
    limits: { max_text_len: 4096 }
```

## Существующие системы

- **KB:** у неё уже есть MCP. Проводим аудит и, если нужно, делаем тонкий wrapper. Не считаем, что текущие ключи read/write/admin разделяют агентов и tenant'ов, пока это не проверено.
- **Telegram:** session-owner остаётся прежним. Сначала только чтение, `propose` выключен до этапа C.
- **Signals:** состояние остаётся в Signals; сбор и действия идут через Hub ([Интеграция](service-integration-v1.md)).

## Ссылки

- [MCP specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25)
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [ContextForge](https://github.com/IBM/mcp-context-forge)
