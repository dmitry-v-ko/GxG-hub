# Контракты подключения существующих сервисов к GxG Hub — v1

**Статус:** целевой migration contract, не развернут и не реализован. 2026-11-20.

Общий принцип: сохраняем действующие источники правды и оркестраторы. ContextForge предоставляет доступ к инструментам. Логика маршрутизации внутри одной возможности — в доменном MCP-коннекторе. Каждый опубликованный инструмент соответствует [MCP Connector Contract v1](mcp-connector-contract-v1.md).

## Knowledge Base — уже есть MCP, не переписывать

В GXG Agents V2 описан [GxG KB Access Layer](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/current/kb-access-layer-mastra.md) с работающим MCP endpoint и категориями read/write/admin. Existing KB и её Supabase остаются единственным источником истины.

**Цель:** подключить сервер через ContextForge как downstream MCP и выполнить gap-audit схем, контрактов, client/tenant authorization, полноты/размеров ответов, tool scopes, ошибок и аудита. До проверки **не утверждать**, что текущие read/write/admin ключи изолируют разных агентов или документы конкретных tenants. При необходимости писать тонкий wrapper, а не новый KB.

Предполагаемые операции: `kb_search`, `kb_get`, `kb_get_context`, `kb_create`, `kb_update` (выпускать лишь реально обнаруженные и авторизованные). Запись доступна только curator/admin и под отдельным scope.

## Telegram — одна сессия и архив

Существующий [Telegram connector](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/current/telegram-ingestion.md) владеет пользовательной сессией, locking, archive-sync, per-chat scope, flood-wait и секретами. Нельзя делать второй session-owner, переписывать историю в ContextForge или позволять agent напрямую выбирать credentials.

Новый **Telegram MCP facade** должен опираться на его domain ports. По этапам:

- `telegram.messages.search` — read: разрешённые чаты и архив, опционально легитимный live fallback в рамках исходных grants.
- `telegram.thread.read` — read: отдельный разрешённый чат/thread, bounded history.
- `telegram.messages.collect` — read/collection: Cursor-aware contract для Signal Sources.
- `telegram.message.send` — **поздний effect**, по умолчанию выключен до прав, подтверждения, idempotency/reconciliation.

Старый `telegram_archive` уже имеет полнотекстовую модель, но на 2026-11-13 поиск из архива **не опубликован как публичная Capability**. Это task for adapter, не повод переписывать storage.

Перед открытием сессии, чтением архива, live API и каждым резервным маршрутом обязательно проверять chat scope в доменном сервисе на основании проверенной идентичности. Не выводить в журнал raw session, peer IDs и тексты сообщений.

## Signal Intelligence — Hub по обе стороны процесса

См. [Signals domain](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/domains/signal-intelligence/README.md), [Actions](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/domains/signal-intelligence/actions.md), [current worker boundaries](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/current/source-inbox.md).

**Signals — отдельный бизнес-сервис и оркестратор, не один общий MCP-tool.** Он использует ContextForge дважды:

```text
Automation / Source schedule
  -> Signals Collector
  -> ContextForge MCP [read or collect from Telegram/Web/Apify/other source]
  -> SourceInboxItem persisted by Signals
  -> optional embeddings -> EvaluationAttempt / ScoredSignal
  -> ActionCandidate persisted by Signals
  -> independent Action / Tenant Workflow worker
  -> ContextForge MCP [effect/action through CRM/Telegram/Social/Browser]
  -> Outcome + Feedback persisted by Signals
```

Принципы:
1. Сбор, оценка, action candidate и outcome сохраняют свои отдельные durable факты и leases. Не переносить таблицы, scoring, события, work claims или retry процесса в Gateway.
2. Со стороны **сбора** определяется typed port `CollectSource`: source+cursor+limit → MCP read tool → нормализованные items, а затем durable store/dedupe в Signals.
3. Со стороны **действий** определяется typed port `ExecuteAction`: action contract+candidate_ref+idempotency → разрешённый MCP effect tool → domain `Outcome` и feedback. Действия не исполняются из коллектора напрямую.
4. Исторические ActionCandidate и их версии должны оставаться неизменными. Операции, имеющие эффект, не исполняются параллельно старым и новым Hub: один авторитетный dispatcher на candidate при переключении.
5. Human review остаётся во внешнем Human Inbox. Без подтверждения нет эффекта. Gateway не решает, какой сигнал «достаточно хороший».
6. Если другим агентам нужен самостоятельный доступ к Signal данным, опциональный `signals.search/get` MCP facade может быть добавлен отдельно. Он не заменяет двустороннее взаимодействие самого Signals с ContextForge.

**Внимание к версиям:** новое [Scoring Workflows](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/domains/signal-intelligence/scoring-workflows.md) — целевое изменение модели; в исходной документации помечено implementation:none. Не выдавать его за действующий production.

## Voice / Oscar

Realtime голос остаётся своим сервисом (low latency, stream/audio). Он MAY вызывать KB/CRM/Signals через ContextForge как обычные tools, но аудио в ContextForge не проксируем «ради единообразия».

## Migration acceptance

- KB всё ещё один источник истины, есть строгий read/write/tenant scope и контрактные тесты.
- Telegram session-owner один, chat-level denial работает до открывания ресурса, нет утечек raw sessions, есть синхронизация и flood-wait.
- Signals создаёт те же SourceInbox, EvaluationAttempt, ActionCandidate, Outcome, что до миграции, и uses Gateway на двух внешних boundaries. Идемпотентность доказана.
- Ни один внешний effect не удваивается при cutover, агент не обходит права через delegate.
- Старый GXG Agents V2 остаётся работающим до отдельно одобренного вывода из эксплуатации.

Никакие из этих критериев не считаются выполненными лишь от появления этого файла.
