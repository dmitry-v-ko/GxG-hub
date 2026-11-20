# GxG MCP Connector Contract v1

Статус: **целевой обязательный стандарт для GxG-managed коннекторов; не реализован**.

## Два разных стандарта

**MCP wire-протокол** уже регламентирует discovery/invoke, транспорты, схемы и результаты. Не создавать собственную несовместимую вариацию протокола. **GxG application profile** определяет семантику наших tools, идентичность, права, ошибки, лимиты и тесты. Для независимых сторонних MCP-серверов допускается read-only аудит и тонкий wrapper. Наш sidecar manifest НЕ является обязательным нестандартным полем сетевого MCP.

## Нормативные требования

**MCC-01 Discovery.** Каждый tool MUST иметь стабильное имя и версию контракта (версия может храниться в регистрационном manifest), понятное описание, валидную JSON Schema для входа, классификацию read/effect. SHOULD иметь типизированный outputSchema и structuredContent при поддержке SDK/версии MCP. Использовать ограниченные payload, курсоры и лимиты вместо огромных ответов.

**MCC-02 Identity.** Каждый вызов MUST быть связан с проверенными tenant и agent principal. Downstream коннектор MUST проверять доверенную identity и scoped grants; **ни аргументы `tenant_id`/`agent_id`, ни произвольные headers от модели не являются доказательством**. Подписанная identity propagation, issuer/audience/expiry, проверка подписи, stripping клиентских headers и защита от повторов MUST быть реально подтверждены на выбранном ContextForge [VERIFY]. Если сквозная identity неизвестна — resource-specific доступ НЕ публикуется. Допустим временный независимый проверенный per-tenant credential, но не универсальный shared token.

**MCC-03 Access.** Разрешения — пересечение tenant, agent, tool, connection, resource scope, risk. Проверять доступ до обращения к credential/provider. Коннектор проверяет точный chat/doc/account; hub-level RBAC недостаточен. Делегированный executor не получает полномочия шире выданного для конкретной задачи.

**MCC-04 Output.** У каждого инструмента контрактный структурированный результат, schema validation, bounded records и source references. Для поисковых tools SHOULD использовать совместимый object-envelope:
```json
{
  "items": [{"id": "opaque-id", "excerpt": "bounded text"}],
  "next_cursor": null,
  "complete": true,
  "partial_reasons": [],
  "source_refs": ["opaque-id"]
}
```
Это **наш рекомендуемый формат поиска**, не новый MCP wire-format и не универсальный формат всех tools. Для эффектов отдельно определить `operation_id`, `status`, `effect_ref`, `reconciliation_required`. Пустой успешный результат не ошибка. Не включать секреты, полные персональные истории и raw provider errors в обычный ответ.

**MCC-05 Errors.** Внутренняя таксономия: `invalid_input`, `permission_denied`, `resource_forbidden`, `rate_limited`, `temporarily_unavailable`, `timeout_before_start`, `outcome_unknown`, `internal_error`. `not_found` и пустой результат могут быть валидным доменным исходом. Маппить на стандартные MCP protocol/tool errors и `isError` по возможности; не считать кастомный JSON error автоматически понятным всем MCP clients. Retry только когда безопасен и разрешён.

**MCC-06 Routing.** Внутри одного инструмента MAY быть несколько approved backends с health/cost/latency/priority/fallback; tier fast/deep MAY быть явными tools. Технический fallback не должен расширять scope и срабатывать на обычный нулевой результат. Семантическое качество оценивает агент.

**MCC-07 Effects.** `send_message`, `publish`, CRM write MUST иметь effect classification, необходимые проверки/approval, идемпотентность или reconciliation, журнал операции и защиту от повторной отправки при `outcome_unknown`. Обычный read tool MUST не менять состояние провайдера.

**MCC-10 Human approval and enforcement (new target).** Для MCP-инструмента с эффектом policy MUST определять необходимость человеческого подтверждения **серверно**, не по аргументу агента. **GxG Action Gate** MUST не исполнять действие в состоянии `approval_required`, а создавать запрос в [GxG Approval Service](../APPROVALS.md). Approval MUST быть проверенным решением авторизованного человека, ограниченным tenant, caller, tool, connection/resource, canonical argument hash, expiry и одноразовым использованием. Исполняющий connector MUST fail-closed без действительного разрешения и без возможности обхода прямым вызовом. Agent-provided `approved: true` или произвольный approval ID никогда не авторизация. Исход `outcome_unknown` требует reconciliation, а не повторной отправки.

**MCC-08 Reliability and audit.** Каждый tool MUST иметь finite timeout, ограничение результата и batch, лимит скорости/стоимости, безопасную обработку секретов, trace/correlation ID, минимально необходимый audit. Telegram flood waits соблюдаются. Сырые сообщения не сохранять в общем audit по умолчанию. События/хуки (если реализованы) имеют подписку, scope, retry-safe dedupe и не становятся новым source of truth.

**MCC-09 Publication gate.** До публикации коннектор MUST пройти тесты: MCP handshake, discovery, схемы; разные principals/tenants; forged identities/resources; права на точные chats/docs; типовые ошибки; cost/fallback; эффекты/идемпотентность/approval с обходом gate; redaction; интеграцию и audit через **закреплённую ContextForge версию**. При отсутствии теста/доказательства статус **unverified**, не «готово».

## Результат аудита старых сервисов

Существующий Knowledge MCP НЕ переписываем автоматически. Сначала сравнить его с MCC-01..09, затем либо добавить wrapper, либо исправить строго недостающее. Telegram и Signals получают тот же стандарт, но сохраняют собственные хранилища и жизненный цикл.

Референсы: https://modelcontextprotocol.io/specification ; https://gofastmcp.com/ ; https://github.com/IBM/mcp-context-forge .
