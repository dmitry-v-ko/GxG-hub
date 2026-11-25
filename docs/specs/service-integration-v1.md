# Интеграция существующих сервисов v1

**Статус:** целевой план, не реализован. 2026-11-20.

**Общий принцип:** существующие источники правды и оркестраторы остаются на месте. Hub даёт к ним доступ через коннекторы по [Connector Contract](mcp-connector-contract-v1.md). Все эффекты идут по пути [propose → approve → execute](effects-and-approval-v1.md).

## Knowledge Base

В GxG Agents V2 уже есть [KB Access Layer](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/current/kb-access-layer-mastra.md) с MCP endpoint и ключами read/write/admin. KB и её Supabase остаются единственным источником истины.

1. Подключаем существующий MCP как downstream в ContextForge и проводим **gap-аудит**: схемы, ошибки, размер ответов, scopes, аудит и главное — разделение по tenant и агентам.
2. Если текущие ключи не разделяют tenant'ов и агентов (вероятно), пишем тонкий wrapper-коннектор. Он проверяет identity и коллекции/документы tenant'а и ходит в KB служебным ключом с минимальными правами. Новую KB не делаем.
3. Операции: `kb.documents.search`, `kb.documents.get`, `kb.context.get` (`read`); `kb.documents.create` и `kb.documents.update` (`write_reversible`, только роль curator). Публикуем только реально существующие и проверенные операции.

## Telegram

Существующий [Telegram connector](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/current/telegram-ingestion.md) владеет пользовательской сессией, блокировками, archive-sync, per-chat scope, flood-wait и секретами. Второго session-owner'а не создаём.

**Решение владельца 2026-11-22 ([ADR-0005](../adr/0005-telegram-service-mcp.md), заменяет ADR-0004).** Telegram-коннектор — отдельный сервис `gxg-telegram-service`. Он единственный владеет сессией и архивом и сам является MCP-сервером по [MCC](mcp-connector-contract-v1.md) за ContextForge. Права устроены так:
- видимость tools — RBAC и `unified_pdp` ContextForge;
- чаты — grants;
- запись — `propose`, затем политика или approver, затем Executor.

Mastra из `gxg_agents_v2` ходит в Telegram только через Hub. Прямого чтения базы архива из Hub нет.

Tools Telegram-коннектора:

| Tool | Class | Этап |
| --- | --- | --- |
| `telegram.messages.search` | `read` | B: архив разрешённых чатов; live fallback — после решения по сессии |
| `telegram.thread.read` | `read` | B: ограниченная история одного разрешённого чата |
| `telegram.messages.collect` | `read` | D: cursor-based сбор из архива для Signals (свежесть = задержка archive-sync) |
| `telegram.message.propose` | `external_effect` | E: выключен до прохождения этапа C на fake provider |

### Ядро и расширения

Telegram-сервис умеет значительно больше четырёх операций выше, и их число будет расти (методы библиотеки Pyrofork). Публиковать каждый метод отдельным tool'ом нельзя: каталог раздуется, а риск эффектов вырастет. Применяем [MCC-11](mcp-connector-contract-v1.md#расширения):

- **Ядро** — таблица выше (плюс `telegram.chats.list` для списка доступных чатов, когда понадобится): типизированные tools с полными тестами.
- **Расширение** — `telegram.ext.search`, `telegram.ext.describe`, `telegram.ext.call` поверх реестра возможностей `gxg-capabilities.yaml` в коннекторе.
- **Классы реестра.** `read`: история, информация о чате и пользователе, участники, закреплённые сообщения и т.п.; вызываются через `ext.call` в пределах разрешённых чатов. Эффекты (отправка, пересылка, правка, удаление, реакции, вступление и выход) через `ext.call` не вызываются, только через `telegram.message.propose` и другие `propose`-tools после этапа E. **Запрещены вообще:** смена пароля или 2FA, завершение сессий, экспорт сессии, удаление аккаунта, произвольный `invoke`.
- **Владение.** Знание о методах Telegram и их классификация (реестр) живут в коннекторе. Формат реестра и тесты — в контракте Hub. Права (`telegram.ext:read`, allowlist чатов, включение записи для tenant'а) выдаёт Hub через политики. Сессию, flood-wait и очередь вызовов по-прежнему ведёт существующий сервис.
- **Очерёдность.** Расширение добавляется после этапов B и D, когда ядро работает. Начинаем с малого набора `read` и расширяем по данным аудита.

### Ограничения Telegram-фасада

- Chat scope проверяется до открытия сессии, чтения архива или live API — на каждом маршруте fallback: запрос к базе фильтруется списком разрешённых `chat_ref`.
- Чат в RID — `chat_ref` архива (стабильный внутренний ID чата), а не peer ID Telegram: `telegram:<connection>/chat/<chat_ref>`. Peer ID остаётся внутренним идентификатором провайдера (MCC-04).
- В логи не попадают сессии, peer ID и тексты сообщений.
- Полнотекстовая модель `telegram_archive` уже есть, но как capability не опубликована (на 2026-11-13). Это задача адаптера, а не повод переписывать хранилище.
- Notify-бот Approval Service — **отдельный** бот, не связанный с этой сессией.

## Signal Intelligence

См. [Signals domain](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/domains/signal-intelligence/README.md), [Actions](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/domains/signal-intelligence/actions.md), [worker boundaries](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/current/source-inbox.md).

Signals — отдельный бизнес-сервис и оркестратор, а не MCP-tool. Он использует Hub с двух сторон:

```text
Расписание источника
  -> Signals Collector
  -> Hub: <source>.collect (read)                 # порт CollectSource
  -> SourceInboxItem, dedupe, EvaluationAttempt, ScoredSignal   (в Signals)
  -> ActionCandidate                               (в Signals)
  -> Action worker
  -> Hub: <domain>.<object>.propose                # порт ExecuteAction
  -> intent: согласование и исполнение            (Approval Service)
  -> effects.status / webhook
  -> Outcome + Feedback                            (в Signals)
```

1. Таблицы, scoring, события, leases и retries процесса остаются в Signals. Hub их не хранит.
2. **CollectSource:** `source + cursor + limit` → read tool → нормализованные элементы → durable store и dedupe в Signals.
3. **ExecuteAction:** `ActionCandidate` → `propose` с `business_task_id = action_candidate_id` и `idempotency_key = action_candidate_id:version` → ожидание статуса → `Outcome`. Повторный `propose` того же кандидата возвращает тот же intent (EFF-08), так что дубля не будет.
4. ActionCandidate и его версии неизменяемы. При переключении со старого пути на Hub для каждого кандидата есть ровно один авторитетный dispatcher. Параллельное исполнение старым и новым путём запрещено.
5. Hub не решает, «достаточно ли хорош» сигнал. Он решает только, разрешено ли действие и согласовано ли оно.
6. Если другим агентам нужен доступ к данным Signals, отдельно добавляется read-only фасад `signals.search/get`.
7. **Подсказка о новых данных.** Сервис-источник MAY отправлять в Signals webhook «появились новые данные», чтобы сбор начался раньше расписания. Первый такой источник — `telegram.message.created` у `gxg-telegram-service` (M11.1). Тест — строка SI-03 [матрицы](conformance-matrix-v1.md).
   - **Тело.** По [`source-event.schema.json`](../../schemas/source-event.schema.json). В нём только идентификаторы: `tenant_id`, `source_ref`, позиция и время элемента. Текста и peer ID нет (ADR-0005; SEC-18 по аналогии).
   - **Только по grants.** Сервис ставит событие в outbox, только если у principal'а Signals этого tenant'а есть grant `collect` на RID `source_ref` (GR). Сам факт и время нового сообщения — уже данные, поэтому scope Connection недостаточно.
   - **Подпись и доставка.** По правилам webhooks Approval Service (AS-22, AS-23): `GxG-Signature`, допуск 300 с, backoff, `dead` через 24 ч. URL получателя — по SEC-15.
   - **Секрет.** Один на пару «сервис-источник, tenant». Хранится и ротируется как остальные секреты (SEC-16, SEC-17).
   - **Приём.** Signals отвергает событие с неверной подписью или с `|now − t| > 300 с`. Tenant он определяет по секрету, которым проверена подпись. Если `tenant_id` в теле с ним не совпадает, событие тоже отвергается. Повтор `event_id` подтверждается ответом 2xx и игнорируется: dedupe хранится не меньше окна доставки, 24 ч. `event_id` детерминирован по источнику и позиции.
   - **Webhook — только подсказка.** Signals не создаёт `SourceInboxItem` из тела события. Он запускает обычный `CollectSource` через Hub (`<source>.collect`) от своего principal'а для `source_ref` из события. Права проверяют grants при этом вызове.
   - **Потеря webhook'а.** Данные не теряются: следующий плановый `collect` по cursor подхватит всё.

Целевая модель [Scoring Workflows](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/domains/signal-intelligence/scoring-workflows.md) помечена implementation:none. Это не текущий production.

## Voice / Oscar

Realtime-голос остаётся отдельным сервисом. Он MAY вызывать tools KB, CRM и Signals через ContextForge как обычный агент со своим principal. Аудио через ContextForge не проксируется.

## CRM

Пока не выбрана. Когда появится: `crm.*.search/get` (`read`), `crm.*.propose_update` (`external_effect`) с предусловием по версии записи (`conflict` при расхождении) и diff в preview.

## Критерии приёмки миграции

- KB по-прежнему единственный источник истины. Read, write и tenant scope проверены тестами.
- У Telegram один session-owner. Отказ по чату срабатывает до доступа к ресурсу, сессии не утекают, flood-wait учитывается.
- Signals создаёт те же SourceInboxItem, EvaluationAttempt, ActionCandidate и Outcome, что и до миграции, и ходит через Hub на обеих границах.
- Ни один эффект не удваивается при переключении.
- GxG Agents V2 продолжает работать до отдельно одобренного вывода из эксплуатации.
