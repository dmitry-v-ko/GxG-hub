# M12 — Signals через GxG Hub (промпт для Codex в `gxg_agents_v2`)

Ты работаешь в репозитории `dmitry-v-ko/gxg_agents_v2`, в новой ветке `codex/m12-signals-gxg-hub` от `main`. Пушь только в эту ветку. В `main` не мержи — это делает владелец.

## Цель

Перевести Signal Intelligence с внутреннего Capability Hub (`mastra/src/mastra/platform/capability-hub`) на внешний **GxG Hub**. Это MCP-шлюз ContextForge с grants и Approval Service. Signals ходит в Hub с двух сторон:
- **CollectSource:** чтение источников через read-tools Hub;
- **ExecuteAction:** действия через `*.propose`, затем Approval Service, затем `effects.status` или webhook, затем `Outcome`.

Таблицы, scoring, leases и retries остаются в Signals. Hub их не хранит и не решает, «хорош ли сигнал».

Контракты лежат в репозитории `dmitry-v-ko/gxg-hub`, ветка `claude/admiring-planck-vzys0x`, commit `ffec05f`. Если доступ есть, читай:
- `docs/specs/service-integration-v1.md` — раздел «Signal Intelligence», пп. 1–7;
- `docs/specs/effects-and-approval-v1.md` — EFF-08, EFF-28, EFF-29, раздел про `business_task_id`;
- `docs/specs/approval-service-v1.md` — AS-21…AS-23, tool `effects.status`;
- `docs/specs/mcp-connector-contract-v1.md` — коды ошибок;
- схемы `schemas/intent-create.schema.json`, `intent-status.schema.json`, `intent-event.schema.json`, `source-event.schema.json`, `error.schema.json`, `search-result.schema.json`.

Если доступа нет, ниже всё необходимое.

## Что есть сейчас (проверено на `acbfc6f`)

Префикс `SI` = `mastra/src/mastra/domains/signal-intelligence`.

- **Сбор.** `SI/application/collect-source.ts` (`CollectSource`, `MapSourceDriverRegistry`) и `collect-source-worker.ts`. Драйверы создаёт `createCapabilityHubSignalSourceDrivers` в `SI/composition.ts:264`. Это `HubCollectionDriver` (`SI/adapters/hub-collection-driver.ts`) для `signal.web_search`, `signal.telegram_search`, `signal.dynamic_collection`, `signal.telegram_folder_collection`.
- **Запись.** `PostgresCollectionAdapter` в `SI/adapters/postgres-collection.ts` пишет в `signal_v12.source_inbox_items`. Dedupe идёт по `(workspace_id, source_id, external_id)` и по `content_hash`.
- **Действия.** `RunActionWorkflow` в `SI/application/run-action-workflow.ts` делает claim, lease, затем handler, затем Outcome. Вызывает его `TenantWorkflowWorker`. Handlers: `request_human_review@1` и `capabilityActionHandler`, который зовёт `CapabilityHubAdapter` (`SI/adapters/capability-hub.ts`). `idempotency_key = action-candidate:${candidate.id}`.
- **`business_task_id`.** В коде его нет.
- **Отправка в Telegram.** В Signals её нет.

## Что сделать

1. **Клиент GxG Hub** — `SI/adapters/gxg-hub/client.ts`.
   - MCP Streamable HTTP к ContextForge: `GXG_HUB_MCP_URL`.
   - Агент `signals` со своим API-токеном ContextForge: `GXG_HUB_AGENT_TOKEN`, один credential на агента (SEC-02).
   - Используй `@modelcontextprotocol/client`, он уже есть в `platform/capability-hub/external-mcp`.
   - Ошибки tools приходят по `error.schema.json`. `rate_limited` и `temporarily_unavailable` означают повтор с `retry_after_s`. `permission_denied` и `not_found` — без повтора.
   - Токен в логи не пишется.

2. **CollectSource через GxG Hub** — `GxgHubCollectionDriver`, тот же интерфейс драйвера, что у `HubCollectionDriver`.
   - Источник Telegram вызывает tool `telegram.messages.collect` с полями `chat_ref` (обязателен, вид `tgc1_…`), `cursor`, `limit`. Ответ по `search-result.schema.json`: `items`, `next_cursor`, `complete`, `partial_reasons`, `source_refs`. Точные схемы лежат в репозитории `dmitry-v-ko/gxg-telegram-service`, ветка `claude/m11.1-telegram-service`: `schemas/messages-collect.input.json` и `schemas/messages.output.json`. Тело сообщения fake-сервер в тестах отдаёт по `messages.output.json`.
   - Каждый элемент нормализуется в `SourceInboxItem`. `external_id = <chat_ref>:<message_id>`: это ключ dedupe нового пути.
   - `cursor` хранится в Signals. Список `chat_ref` берётся из конфигурации источника.
   - Остальные источники (web_search, dynamic_collection) остаются на старом драйвере, пока в GxG Hub нет соответствующих tools.

3. **ExecuteAction через GxG Hub** — `gxgHubActionHandler`.
   - Действие вызывает `<domain>.<object>.propose` с полями:
     - `business_task_id = action_candidate.id`;
     - `idempotency_key = ${action_candidate.id}:${action_candidate.version}`;
     - args действия.
   - Ответ сразу `{intent_id, status}` (EFF-29). Повторный propose того же кандидата возвращает тот же `intent_id` (EFF-08).
   - `intent_id` сохраняется у кандидата. Нужна аддитивная миграция в `supabase/migrations`: колонка, без удаления и переименования.
   - Ожидание: опрос `effects.status({intent_id})` с интервалом `GXG_HUB_STATUS_POLL_S`, по умолчанию 30.
   - Терминальные статусы:
     - `succeeded` и `reconciled_succeeded` → `Outcome` успех;
     - `failed`, `rejected`, `expired`, `cancelled` и `reconciled_failed` → `Outcome` неуспех с причиной.
   - `outcome_unknown` — ждать дальше, не повторять propose.

4. **Webhooks статуса (AS-21…AS-23)** — HTTP endpoint Signals для `intent-event`.
   - Проверка подписи: `GxG-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, t + "." + body)>`.
   - Отказ при `|now − t| > 300 с`.
   - Повтор `event_id` подтверждается 2xx и игнорируется. Dedupe хранится не меньше 24 ч.
   - Tenant определяется по секрету, которым проверена подпись. Несовпадение с `tenant_id` в теле → отказ.
   - Webhook — только подсказка: по нему Signals сразу делает `effects.status`. Источник истины — `effects.status`.

5. **Подсказка о новых сообщениях (SI-03)** — тот же приёмник для тела `source-event`.
   - Тело: `{schema_version: 1, event_id, type: "telegram.message.created", tenant_id, source_ref: "telegram:<connection>/chat/<chat_ref>", chat_ref, message_id, sent_at}`.
   - Правила подписи, времени, tenant и dedupe — как в п. 4.
   - Из тела `SourceInboxItem` **не создаётся**. Signals запускает обычный CollectSource через Hub для `chat_ref` из события.
   - Чат, которого нет в конфигурации источника, игнорируется.

6. **Переключатель и один dispatcher (AC-M12-05).**
   - Env `SIGNAL_HUB_BACKEND=internal|gxg`, по умолчанию `internal`, задаётся отдельно для сбора и для действий.
   - Для каждого ActionCandidate есть ровно один авторитетный dispatcher. При `gxg` старый путь кандидатов не исполняет.
   - Счётчик исполнений старого путём — метрика или лог-счётчик за прогон.

7. **Тесты (vitest), все без сети и без реального Telegram.** Используй fake MCP-сервер, который отвечает по схемам выше, и fake Approval-ответы.
   - **AC-M12-01.** Один и тот же фиксированный набор сообщений: старый путь (архив) и новый (`telegram.messages.collect` через fake Hub) дают одно и то же множество `SourceInboxItem` по ключам dedupe. Сопоставление ключей `peer_id:message_id` ↔ `chat_ref:message_id` — через фикстуру соответствия.
   - **AC-M12-02.** 100 `ActionCandidate` → 100 propose, у каждого `business_task_id` совпадает → 100 `Outcome`. Журнал fake provider — 100 записей.
   - **AC-M12-03.** Повторный запуск Action worker'а по тем же 100 кандидатам → 0 новых intents и 0 новых записей провайдера.
   - **AC-M12-04.** Webhooks выключены → `Outcome` всё равно появляются через опрос `effects.status`, не позже чем через 2 интервала.
   - **AC-M12-05.** При `gxg` счётчик старого пути равен 0 за прогон.
   - **AC-M12-06.** Подписанный `telegram.message.created` запускает collect для `source_ref`, а тело события не создаёт `SourceInboxItem`. Ноль новых элементов дают: неверная подпись, `t` старше 300 с, чужой `tenant_id`, повтор `event_id`, чат вне конфигурации. С выключенными webhook'ами плановый collect даёт то же множество элементов.

## Ограничения

- Боевые секреты, production-базу и реальный Telegram не трогать. Миграции только аддитивные.
- Существующие тесты Signals (`npm test`, `npm run typecheck` в `mastra/`) должны остаться зелёными.
- Не удалять старый путь: он нужен до отдельно одобренного вывода из эксплуатации.
- Изменения минимальные: новые адаптеры рядом со старыми, сборка — в `SI/composition.ts`.
- Секреты только из env. В git, логи и тексты ошибок они не попадают.

## Что сдать

- Ветка `codex/m12-signals-gxg-hub`, `npm run typecheck` и `npm test` зелёные.
- Файл `docs/plans/m12-signals-gxg-hub.md` в репозитории:
  - что сделано, по пунктам 1–7;
  - таблица AC-M12-01…06 → тест → результат;
  - список env-переменных: только имена;
  - что осталось на стороне Hub: регистрация агента `signals` в ContextForge, grants `collect`/`propose` для его principal'а, подписчик webhooks в bundle.
