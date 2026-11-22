# Gap-аудит KB MCP (M6)

**Статус: аудит выполнен по коду gxg_agents_v2, сервер KB MCP в нём отсутствует.** 2026-11-22. Источник — `dmitry-v-ko/gxg_agents_v2` на commit `f1c4d6d9e7bd5202fc703dabf3b0fba3fc732f0f` (дальше `@f1c4d6d`), клон только для чтения, `git status` пуст. Каркас wrapper'а — ветка `origin/claude/m6-kb` GxG Hub на `79ce286` (ссылки `m6:путь:строка`).

Сокращения в ссылках (все `@f1c4d6d`): «адаптер» = `mastra/src/mastra/domains/knowledge-base/adapters/supabase-knowledge-base-adapter.ts`; `SKILL.md` = `ai/skills/mcp-kb/SKILL.md`; `knowledge-toolsets.ts` = `mastra/src/mastra/domains/voice/knowledge-toolsets.ts`; `meeting-voice-kb.ts` = `mastra/src/mastra/mcp/meeting-voice-kb.ts`; `kb-client.ts` = `mastra/src/mastra/mcp/kb-client.ts`; `contracts.ts` = `mastra/src/mastra/domains/knowledge-base/domain/contracts.ts`; миграции — `supabase/migrations/<файл>`; `tools.py`/`backend.py` = `m6:connectors/kb/src/kb_connector/…`.

Источник требований: [service-integration-v1](../specs/service-integration-v1.md#knowledge-base), [MCC-03](../specs/mcp-connector-contract-v1.md#требования), [GR-01…GR-17](../specs/resource-grants-v1.md), [security-v1](../specs/security-v1.md), [AC-M6-01…04](../implementation-plan-v1.md#m6--knowledge-base).

## Главный вывод

1. **Кода MCP endpoint'а KB (KB Access Layer) в gxg_agents_v2 нет.** Документ запускает его из отдельного каталога `kb_access` вне репозитория (`docs/current/kb-access-layer-mastra.md:82-83@f1c4d6d`); верификатор ждёт отдельный корень `--kb-root` с `requirements.txt`, `tests`, `kb_access` и гоняет там `python3 -m unittest` (`mastra/scripts/verify-meeting-voice-contract.mjs:105-109,167-171@f1c4d6d`). В репозитории нет ни одного серверного MCP для KB: единственный `MCPServer` — operator (`mastra/src/mastra/mcp/operator-server.ts:1,11@f1c4d6d`). Поэтому реальные `inputSchema`/`outputSchema`, коды ошибок, проверка ключей, лимиты и rate limit сервера **в этом коде не определены**. Ниже они восстановлены по документации, клиентам и in-process реализации тех же read-tools в Mastra (`SupabaseKnowledgeBaseAdapter`) — с пометкой источника.
2. **Базовой схемы KB в миграциях нет.** `supabase/migrations/0001_init.sql:1@f1c4d6d`: «applied directly via Supabase dashboard, not tracked locally». KB-миграции только меняют уже существующие `kb.entities` и т. д.; на чистом PostgreSQL 16 все 16 падают (`schema "kb" does not exist`). Локальную копию KB из кода gxg_agents_v2 поднять нельзя.
3. **Ни один tool KB нельзя подключать к ContextForge «как есть»:** ключи — на роль (read/write/admin), а не на агента или tenant'а (`docs/current/kb-access-layer-mastra.md:58-61@f1c4d6d`), понятия «коллекция» в KB нет, клиент берёт самый сильный ключ из окружения (`mastra/src/mastra/mcp/kb-client.ts:5-8@f1c4d6d`). Нужен wrapper `connectors/kb` (решение спеки, п. 2). Публиковать только `kb.documents.search`, `kb.documents.get`, `kb.context.get`, построенные на `kb_search` и `kb_get`.

## Что есть в коде gxg_agents_v2

| Что | Где | Значение для аудита |
| --- | --- | --- |
| Описание KB Access Layer: URL, ключи, роли tools | `docs/current/kb-access-layer-mastra.md:14-23,63-69@f1c4d6d` | Каталог tools по ролям, без схем |
| Skill `mcp-kb`: другой каталог tools, auth, таймауты, примеры аргументов | `ai/skills/mcp-kb/SKILL.md:17-22,41-74,87-93@f1c4d6d` | Единственные примеры аргументов MCP (`query`, `limit`, `id`, `topic`, `max_tokens`) |
| MCP-клиенты: Mastra (`kb-client.ts`), smoke, Meeting Voice | `mastra/src/mastra/mcp/kb-client.ts:1-43`, `scripts/smoke-kb-mcp.mjs:1-47`, `mastra/src/mastra/mcp/meeting-voice-kb.ts:1-193@f1c4d6d` | Как передаётся ключ, имена tools у клиента |
| In-process KB для Voice поверх Supabase REST (service role) | `mastra/src/mastra/domains/knowledge-base/**`, `mastra/src/mastra/domains/voice/knowledge-toolsets.ts@f1c4d6d` | Эталон read-логики над теми же таблицами; схемы входа `kb_search/kb_get/kb_get_context` (zod) |
| Список имён tools в домене | `mastra/src/mastra/domains/knowledge-base/domain/contracts.ts:1-7@f1c4d6d` | Есть `kb_publish`, `kb_quality_audit`, которых нет в документе |
| KB-миграции (частичные) | `supabase/migrations/2026052800…20260710110000@f1c4d6d` (16 файлов, [vendor](../../connectors/kb/vendor/kb-migrations/SOURCE.md)) | Колонки `kb.entities`, профили, RLS, гранты |
| Тесты endpoint'а KB MCP | нет: `scripts/smoke-kb-mcp.mjs` — ручной smoke; тесты `kb_access` — в другом репозитории (`verify-meeting-voice-contract.mjs:167-171@f1c4d6d`) | Серверное поведение тестами здесь не подтверждено |

### Расхождения каталогов tools

| Tool | Документ (`kb-access-layer-mastra.md:67-69`) | Skill (`SKILL.md:45-74`) | `contracts.ts:1-7` | Прочее |
| --- | --- | --- | --- | --- |
| `kb_list`, `kb_get_concept`, `kb_get_relations`, `kb_learning_path`, `kb_stats`, `kb_taxonomy` | — | read | — | — |
| `kb_taxonomy_list_axes`, `kb_taxonomy_list_terms`, `kb_taxonomy_get_entity` | read | — | есть | smoke вызывает `kb_taxonomy_list_terms` (`scripts/smoke-kb-mcp.mjs:39-41`) |
| `kb_list_entity_types` | — | read | есть | — |
| `kb_quality_audit` | — | — | есть | — |
| `kb_publish` | — | — | есть | «`kb_create` и `kb_publish` оба требуют `write`» (`docs/plans/capability-hub-remediation.md:330`) |
| `kb_file_search`, `kb_file_get` | — | — | — | обязательны для Meeting Voice (`meeting-voice-kb.ts:173-175`) |
| `kb_file_sync` | — | — | — | write-tool в тестах/evals Meeting Voice (`mastra/src/mastra/mcp/meeting-voice-kb.test.ts:21,27`, `mastra/scripts/run-meeting-voice-evals.mjs:372`) |

Авторитетный список — только `tools/list` живого сервера; к нему по условиям задачи не подключались.

## Общее для всех tools KB MCP

| Аспект | Факт | Ссылка `@f1c4d6d` |
| --- | --- | --- |
| Транспорт | MCP по HTTP (URL), сервер `gxg-kb` | `ai/skills/mcp-kb/SKILL.md:15-19` |
| URL | `https://kb.agents.gxg-labs.com/mcp`; по документу проксируется на Railway `gxg-kb-access-production…/mcp`, `/mcp` обходит Basic Auth UI | `docs/current/kb-access-layer-mastra.md:15,25`; `docs/current/config.md:758-759` |
| Расхождение URL | В коде фронтенда `/mcp` на KB-хосте переписывается на `https://hub-origin.agents.gxg-labs.com/mcp`, не на Railway KB | `frontend/next.config.ts:54-64` (redirect с других хостов — `:15-40`) |
| Auth: формат | `Authorization: Bearer <ключ>`; ключ непрозрачная строка, у Meeting Voice 1…4096 символов | `mastra/src/mastra/mcp/kb-client.ts:24`; `scripts/smoke-kb-mcp.mjs:18`; `meeting-voice-kb.ts:13,152` |
| Auth: роли | read ⊂ write ⊂ admin; имена переменных `GXG_KB_READ_KEY`, `GXG_KB_WRITE_KEY`, `GXG_KB_ADMIN_KEY` | `docs/current/kb-access-layer-mastra.md:16-22,65-69` |
| Auth: проверка | **нет в коде** (сервер вне репозитория). В БД права на профили есть только у `service_role`, `updated_by` по умолчанию `kb-access`, `source` — `mcp`: KB Access Layer ходит в Supabase service role, разделение read/write/admin — только в процессе `kb_access`, не в RLS | `supabase/migrations/20260703000000_kb_profiles.sql:11,15,49-52,363-373` |
| Выбор ключа клиентом | admin > write > read — первый найденный | `mastra/src/mastra/mcp/kb-client.ts:5-8`; `scripts/smoke-kb-mcp.mjs:4-8`; `SKILL.md:33` |
| Таймауты | 120 с запрос, 30 с connect (skill); 5 с (Meeting Voice) | `SKILL.md:21`; `meeting-voice-kb.ts:8,145` |
| Параллельность | «Parallel tool calls: Not supported (sequential only)» | `SKILL.md:22` |
| Rate limit | **нет в коде** — ни у клиентов, ни в документации; ожидался бы в `kb-access-layer-mastra.md` «Environment» или `SKILL.md` «Server Identity» | `docs/current/kb-access-layer-mastra.md:12-25`; `SKILL.md:17-22` |
| Лимит размера ответа | **нет в коде**; in-process `get` отдаёт весь `body_md` и все связи | `mastra/src/mastra/domains/knowledge-base/adapters/supabase-knowledge-base-adapter.ts:132-141` |
| Пагинация | **нет**: только `limit`, без `offset`/`cursor` | `SKILL.md:90`; `knowledge-toolsets.ts:33-40`; адаптер `:145,167` |
| Коды ошибок | **нет в коде сервера**. Клиенты не полагаются на текст: Meeting Voice заменяет любую ошибку на `meeting_voice_kb_read_failed`; in-process: HTTP ≥500 → «storage is unavailable», иначе «request failed»; нет сущности → `null` (get) или исключение «KB entity was not found.» | `meeting-voice-kb.ts:51-58,99-105`; адаптер `:32-36,118,134` |
| Форма результата | не зафиксирована: потребитель принимает массив, `{items}`, `{entities}` или `{item}` | `mastra/src/mastra/tools/company-knowledge-tool.ts:123-141` |
| `outputSchema` / `structuredContent` (MCC-04) | **нет в коде**; in-process tools объявляют только `inputSchema` | `knowledge-toolsets.ts:30-60` |
| Tenant | В БД есть `workspace_id` (сущности, оси таксономии, профили), по умолчанию `00000000-0000-0000-0000-000000000000`. Привязан ли ключ MCP к workspace — **нет в коде**; Meeting Voice держит отдельный read-ключ на workspace (намёк, не доказательство) | `supabase/migrations/20260701000000_fix_security_advisor_findings.sql:1-23`; `20260703000000_kb_profiles.sql:5`; `20260606120000_entity_type_to_taxonomy.sql:8`; `meeting-voice-kb.ts:14-17,129-133` |
| Агент | Изоляции нет: один ключ на роль для всех агентов | `docs/current/kb-access-layer-mastra.md:58-61` |
| Коллекции | Понятия нет. Ближайшие: `entity_type_id` (ровно один у сущности), термы таксономии (много на сущность, `kb_entity_taxonomy`), флаг `public`, `status` | `20260701000000_fix_security_advisor_findings.sql:6-21`; адаптер `:122-130` |
| Черновики | In-process поиск исключает только `archived`: `draft`/`enriched` видны читателю. Что делает сервер — **нет в коде** | адаптер `:145`, `:248` |
| Аудит | Сервер — **нет в коде**. In-process пишет только мутации: `operation`, `workspaceId`, `entityId`, `outcome`, без тел | адаптер `:13-18,93-98` |

## Таблица аудита

Вывод: **как есть** (gateway KB напрямую в ContextForge), **wrapper** (только через `connectors/kb`), **не публиковать**. «Сервер: нет в коде» означает, что поле не определено в gxg_agents_v2; схемы восстановлены по клиентам.

| Tool KB | Схемы in → out (обязательные **жирным**) | Ошибки → MCC-05 | Лимиты | Auth | Изоляция tenant / агент / коллекция | Вывод |
| --- | --- | --- | --- | --- | --- | --- |
| `kb_search` | **`query`** string ≥1; `limit` int (1…100, по умолч. 10 в zod; адаптер режет до 1…50); `entity_types` string[]; `domain` string; `pillar` string; `include_body` bool=false → массив строк `kb_entities`: `id` int, `title`, `summary`, `entity_type_id`, `status`, `confidence`, `data`, `created_at`, `updated_at`, (`body_md`) (`knowledge-toolsets.ts:33-40`; адаптер `:143-168`) | сервер: нет в коде; см. маппинг ниже | `limit` ≤50; нет cursor/offset; при пустых термах (слова <3 символов) — фильтра нет, отдаются любые `limit` записей (адаптер `:42-44,145-146`) | read | workspace — только если сервер фильтрует (in-process — да, из конфигурации, `:100,145`); агент — нет; коллекция — фильтр `entity_types[]` есть, `domain`/`pillar` — по одному значению | **wrapper** → `kb.documents.search` |
| `kb_get` | **`id`** int > 0 → строка `kb_entities` (`*`: `id`, `workspace_id`, `entity_type_id`, `title`, `slug`, `summary`, `body_md`, `status`, `confidence`, `owner_user_id`, `data`, `provenance`, `current_version_no`, `created_at`, `updated_at`, `archived_at`, `public`, `search_vector`) + `taxonomy[]` + `relations{outgoing,incoming}`; нет → `null` (`knowledge-toolsets.ts:27,44-49`; адаптер `:132-141`; представление `20260701000000…sql:1-23`) | нет → `null` (in-process) | ответ не ограничен | read | workspace — как выше; коллекцию (`entity_type_id`) отдаёт KB — годится для GR-01/GR-03 | **wrapper** → `kb.documents.get` |
| `kb_get_context` | `topic` или `query` string (одно обязательно, иначе ошибка); `max_tokens` int ≤16000, по умолч. 1600 → `{topic, markdown, entities:[{id,title}]}` (`knowledge-toolsets.ts:51-60`; адаптер `:170-182`) | — | `max_tokens` считает слова, не токены (адаптер `:174-178`) | read | По skill «expands relations» (`SKILL.md:49`) — связи ведут в другие коллекции; фильтра коллекций во входе нет; markdown склеен — отфильтровать после нельзя (GR-16) | **не публиковать**; `kb.context.get` строится wrapper'ом из `kb_search` |
| `kb_list` | фильтры `entity_type`, `domain`, `pillar`, `status` (`SKILL.md:50`); схема — нет в коде | — | нет в коде | read | обход без запроса | не публиковать (в M6 нет операции) |
| `kb_get_concept` | концепт + все сущности кластера (`SKILL.md:51`) | — | нет в коде | read | обход графа через коллекции | не публиковать |
| `kb_get_relations` | соседи 1–2 хопа (`SKILL.md:52`) | — | нет в коде | read | то же; in-process связи читаются без фильтра workspace (адаптер `:137-138`) | не публиковать |
| `kb_learning_path` | порядок чтения кластера (`SKILL.md:53`) | — | нет в коде | read | то же | не публиковать |
| `kb_stats` | статистика всей KB (`SKILL.md:54`) | — | нет в коде | read | агрегаты раскрывают число скрытых записей (против GR-16) | не публиковать |
| `kb_quality_audit` | `limit` → `{sampled, issue_counts, issues}` (адаптер `:186-196`) | — | `limit` по умолч. 50 | read (`contracts.ts`, в документе нет) | все коллекции | не публиковать |
| `kb_taxonomy` | оси, термы, счётчики (`SKILL.md:55`) | — | нет в коде | read | счётчики по термам | не публиковать |
| `kb_taxonomy_list_axes` | `include_inactive` bool → оси (адаптер `:198-202`) | — | — | read | фильтр workspace (in-process) | не публиковать; wrapper может вызывать для конфигурации |
| `kb_taxonomy_list_terms` | `axis_key` string, `include_inactive` → термы (адаптер `:204-212`) | — | — | read | без `axis_key` термы читаются без фильтра workspace (адаптер `:208-211`) | не публиковать |
| `kb_taxonomy_get_entity` | **`entity_id`** int → назначения таксономии (адаптер `:214-217`) | нет сущности → исключение (`:116-120`) | — | read | — | не публиковать |
| `kb_list_entity_types` | — → `id,key,label,is_active` активных (адаптер `:184`) | — | — | read | глобально, без workspace | не публиковать; **wrapper вызывает** для `entity_type_id → key` и `list_collections` |
| `kb_profile_get` | `workspace_id` uuid → активный профиль (`body_md`…) (адаптер `:219-221`; RPC `20260703000000_kb_profiles.sql:54-90`) | — | — | read | `workspace_id` — аргумент вызывающего; RPC `SECURITY DEFINER` доверяет ему (`:54-90`); in-process сверяет с конфигурацией (адаптер `:102-105`), сервер — нет в коде | не публиковать (нет в спеке M6) |
| `kb_profile_list` | `workspace_id`, `include_archived` (адаптер `:223-225`) | — | — | read | то же | не публиковать |
| `kb_file_search`, `kb_file_get` | нет в коде; результат `{data: [...]}` / `{data: {chunks}}` (`mastra/src/mastra/agents/meeting-voice.ts:185-201`) | клиент заменяет на `meeting_voice_kb_read_failed` | — | read-ключ на workspace (`meeting-voice-kb.ts:129-133`) | неизвестно | не публиковать до описания владельцем |
| `kb_create` | `title`, `summary`, `type`, `body`, теги (`SKILL.md:64`); `status=published` запрещён (адаптер `:227-231`) | — | — | write | нет `idempotency_key` (MCC-07) | не публиковать (write_reversible — не M6) |
| `kb_update` | **`id`**, `patch` из `title, summary, body_md, status, confidence, data, entity_type_id` (адаптер `:233-240`) | — | — | write | то же | не публиковать |
| `kb_publish` | **`id`**; publish gate (адаптер `:242-252`) | gate → исключение со списком причин (`:249`) | — | write (`capability-hub-remediation.md:330`) | — | не публиковать |
| `kb_relate`, `kb_tag`, `kb_untag` | связи и теги (адаптер `:254-284`) | — | — | write | — | не публиковать |
| `kb_profile_update` | профиль-черновик (адаптер `:286-289`) | — | — | write | — | не публиковать |
| `kb_file_sync` | нет в коде | — | — | write (по тестам Meeting Voice) | — | не публиковать |
| `kb_taxonomy_create_term`, `kb_taxonomy_update_term`, `kb_taxonomy_audit` | адаптер `:291-317` | — | — | admin | in-process: workspace проверяется через ось (`:262,293-294`); сервер — нет в коде | не публиковать |
| `kb_profile_activate` | адаптер `:319-321` | — | — | admin | — | не публиковать |
| `kb_profile_history` | **`id`** uuid (адаптер `:323`) | — | — | admin | RPC не фильтрует по workspace: `WHERE v.profile_id = p_id` (`20260703003000_remove_kb_profile_digest_dependency.sql:229`) | не публиковать |

**Ни одного «как есть»:** нет identity агента и tenant'а (SEC-02, SEC-03, MCC-02), нет прав по RID (MCC-03, GR-16/17), нет `outputSchema` и MCC-кодов (MCC-04/05), нет лимитов ответа и rate limit (MCC-08), клиенты берут сильнейший ключ.

### Сопоставление с tools `connectors/kb`

| Tool wrapper'а (`m6:connectors/kb/gxg-manifest.yaml`) | KB tool | Что делает wrapper поверх | Требование |
| --- | --- | --- | --- |
| `kb.documents.search` | `kb_search` с `entity_types = разрешённые коллекции` | Названная коллекция без grant'а → `not_found` до вызова KB (`m6:connectors/kb/src/kb_connector/tools.py:147-167`); запрос только по разрешённым (GR-16); повторная `check` каждого элемента (`tools.py:169-186`); cursor — HMAC wrapper'а | GR-16, GR-17, MCC-04 |
| `kb.documents.get` | `kb_get` (+ кэш `kb_list_entity_types`) | Коллекция = `entity_type.key` из ответа KB, не из аргумента; нет права / другой workspace → `not_found` побайтно (`tools.py:215-237`) | GR-01, GR-02, GR-03, SC-05 |
| `kb.context.get` | `kb_search` с `include_body=true` | Склейка отрывков в wrapper'е (`tools.py:239-266`), не `kb_get_context` | GR-16, MCC-04 |

### Допущения каркаса wrapper'а — проверка

| # | Допущение (шаблон) | Результат |
| --- | --- | --- |
| 1 | Ключи KB не различают агентов | **Подтверждено**: ключ на роль (`kb-access-layer-mastra.md:58-61@f1c4d6d`) |
| 2 | Одна KB-connection на tenant | **Не подтверждено и не опровергнуто**: в БД есть `workspace_id`; привязка ключа к workspace — нет в коде. Принять: connection = (URL, read-ключ, `workspace_id`) на tenant (Q2) |
| 3 | Поиск принимает список коллекций | **Частично**: `entity_types[]` — да; `domain`/`pillar` — по одному значению (`knowledge-toolsets.ts:36-38@f1c4d6d`). Верно, если коллекция = тип сущности (Q1) |
| 4 | `get` возвращает коллекцию документа | **Да**, если коллекция = `entity_type_id` (одно значение на сущность). Для таксономии — нет: термов много, контейнер неоднозначен (GR-03) |
| 5 | `kb.context.get` в KB нет | **Есть** `kb_get_context`, но с обходом связей и без фильтра коллекций — не использовать |
| 6 | ID — строки без `/` и `*` | ID документа — целое (`knowledge-toolsets.ts:27@f1c4d6d`) → десятичная строка; ключ типа — `insight`, `growth_hack`… (`20260628152000_kb_type_guidance.sql:5,25@f1c4d6d`), формату RID подходит |

## Контракт для адаптера `KbBackend`

Заменяет fake в `m6:connectors/kb/src/kb_connector/backend.py:38-87` с тем же интерфейсом. Ниже — вариант по умолчанию (Q1–Q3); всё помеченное «нет в коде» сверить с `kb_access`.

| Пункт | Контракт |
| --- | --- |
| Базовый URL | Из конфигурации connection (не из аргументов, SEC-15). Прод: `https://kb.agents.gxg-labs.com/mcp` (`kb-access-layer-mastra.md:15@f1c4d6d`); какой upstream за ним — Q5. Локально: `http://127.0.0.1:8090/mcp` (`scripts/smoke-kb-mcp.mjs:3@f1c4d6d`) |
| Протокол | MCP поверх HTTP; вызовы `tools/list`, `tools/call`. Последовательно: не более одного вызова на connection одновременно (`SKILL.md:22@f1c4d6d`) — `asyncio.Semaphore(1)` |
| Ключ | Только read-ключ (не fallback на write/admin, в отличие от `kb-client.ts:5-8@f1c4d6d`). Заголовок `Authorization: Bearer <ключ>`. Значение — из secret store по (tenant, connection) (SEC-16, MCC-03); новая переменная wrapper'а, например `KB_CONNECTIONS_FILE` со ссылками на секреты, без значений в Git |
| Конфигурация tenant'а | `connections[tenant] = {connection_id, url, key_ref, workspace_id}` — заменяет `fixtures/kb.json` (`backend.py:41`) |
| `ready()` | Сделать `async` (критик M6-cycle-2 N2). Проба `tools/list` с read-ключом: успех и **в списке нет write/admin tools** (`kb_create`, `kb_update`, `kb_publish`, `kb_relate`, `kb_tag`, `kb_untag`, `kb_profile_update`, `kb_file_sync`, `kb_taxonomy_*_term`, `kb_taxonomy_audit`, `kb_profile_activate`, `kb_profile_history`); иначе `/readyz` = fail. Скрывает ли сервер tools по роли — нет в коде (Q4) |
| `list_collections(tenant)` | `kb_list_entity_types` → `key` активных; кэш с TTL (типы меняются миграциями) |
| `get_document(tenant, document_id)` | `document_id` не `^[1-9][0-9]{0,17}$` → `None` без вызова KB. Иначе `kb_get {id: int}`. `null`/ошибка «not found» → `None`. Ответ с `workspace_id` ≠ workspace connection'а → `None` (GR-02, защита на случай общего ключа). `archived_at`≠null или `status`=`archived` → `None`; черновики — Q3. Маппинг: `id`=str(`id`), `collection`=key по `entity_type_id`, `title`, `content`=`body_md` (если пусто — `summary`), `updated_at` |
| `search(tenant, collections, query, offset, limit)` | `collections` пуст → `([], False)` без вызова. Иначе `kb_search {query, entity_types: collections, limit: min(offset+limit+1, 50), include_body: true}`; страница = `[offset:offset+limit]`, `more` = длина > `offset+limit`. KB не даёт offset: глубина ≤50 результатов. Порядок — ранжирование KB (адаптер `:165@f1c4d6d`), стабильность между вызовами не гарантирована |
| Ответы | Брать только известные поля; остальное (`data`, `provenance`, `owner_user_id`, `search_vector`, связи) отбрасывать (MCC-04: внутренние данные провайдера не возвращаются). Обрезка — уже в `tools.py:45-46` |
| Документ → RID | Право на `kb:<connection_id>/collection/<entity_type_key>`; `source_ref` — тот же RID (`tools.py:65-66`). Номер сущности — только как `id` документа, не в RID |
| Таймаут | Таймауты tool'а из manifest (10–15 с) прерывают вызов (`tools.py:92-95`); у клиента — connect ≤5 с |

**Ошибки → Connector Contract.** Текст ошибки KB наружу не передаётся (SEC-18; так же поступает Meeting Voice, `meeting-voice-kb.ts:99-105@f1c4d6d`).

| От KB | Код Hub | Комментарий |
| --- | --- | --- |
| Сеть, connect timeout, HTTP 502/503/504 | `temporarily_unavailable` | для `read` повтор безопасен |
| HTTP 429 | `rate_limited`, `retry_after_s` из `Retry-After`, иначе 5 | rate limit у KB — нет в коде, на всякий случай |
| Таймаут после отправки (`read`) | `temporarily_unavailable` | уже в `tools.py:92-95` |
| `kb_get` → `null` / tool error «not found» | `not_found` с общим `NOT_FOUND` | побайтно как нет права (SC-05) |
| HTTP 401/403, JSON-RPC ошибка auth | `internal_error` + `/readyz` fail | это неверный служебный ключ, а не identity вызывающего — не `unauthenticated` |
| JSON-RPC `-32601`/`-32602`, неизвестный tool, ответ не той формы | `internal_error` | дрейф контракта; лог — класс и место, без текста |
| Любой другой tool error (`isError: true`) | `internal_error` | — |

## Локальная копия KB (AC-M6-03/04)

**Сейчас невыполнимо из кода gxg_agents_v2** (G1, G2). Нужно три вещи, двух нет:

| Компонент | Есть в gxg_agents_v2 | Что нужно |
| --- | --- | --- |
| Базовая схема `kb.*` и `public.kb_*` | нет (`0001_init.sql:1@f1c4d6d`) | DDL без данных: `pg_dump --schema-only --schema=kb` и определения `public.kb_*` от владельца, либо миграции из `kb_access` (Q7) |
| KB-миграции поверх базы | да, 16 файлов, [vendor](../../connectors/kb/vendor/kb-migrations/SOURCE.md) | — |
| Сервер `kb_access` (Python, `requirements.txt`, `tests`) | нет (`verify-meeting-voice-contract.mjs:105-109@f1c4d6d`) | read-доступ к его репозиторию (Q7). Документированный локальный запуск — `KB_REPOSITORY=memory KB_AUTH_DISABLED=true PORT=8090 python3 main.py` (`kb-access-layer-mastra.md:82-83@f1c4d6d`) — с **выключенной** auth и для AC-M6-04 не годится |

Процедура после получения обоих (синтетические данные, живую Supabase не трогать):

```bash
# 1. База: локальный PostgreSQL 16 или `supabase start` (supabase/config.toml@f1c4d6d: порт БД 54322)
createdb kb_local
psql kb_local -c 'create role anon; create role authenticated; create role service_role;'
psql kb_local -v ON_ERROR_STOP=1 -f kb_schema_only.sql            # DDL от владельца, без данных
for f in connectors/kb/vendor/kb-migrations/*.sql; do psql kb_local -v ON_ERROR_STOP=1 -f "$f"; done
psql kb_local -c "insert into kb.entities (workspace_id, entity_type_id, title, summary, body_md, status)
  values ('00000000-0000-0000-0000-000000000000', 1, 'M6 synthetic', 'synthetic', 'synthetic body', 'published');"
# 2. Ключи: два случайных синтетических значения; передать kb_access как read- и write-ключ
#    способом из его кода (имя переменной в gxg_agents_v2 отсутствует). В Git/логи не писать.
export KB_LOCAL_READ="$(openssl rand -hex 32)" KB_LOCAL_WRITE="$(openssl rand -hex 32)"
# 3. kb_access с включённой auth и репозиторием Supabase/Postgres (не memory), PORT=8090.
```

Проверка AC-M6-04 — запись read-ключом отклоняется **KB**:

```bash
U=http://127.0.0.1:8090/mcp
H=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
# initialize: protocolVersion — из SDK kb_access; session id — из заголовка ответа Mcp-Session-Id
curl -sSi "${H[@]}" -H "Authorization: Bearer $KB_LOCAL_READ" "$U" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"m6-ac04","version":"0"}}}'
S=<Mcp-Session-Id из ответа>
curl -sS "${H[@]}" -H "Authorization: Bearer $KB_LOCAL_READ" -H "Mcp-Session-Id: $S" "$U" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
# а) в tools/list нет write-tools
curl -sS "${H[@]}" -H "Authorization: Bearer $KB_LOCAL_READ" -H "Mcp-Session-Id: $S" "$U" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
# б) прямой вызов записи: ожидается JSON-RPC error или result.isError=true
curl -sS "${H[@]}" -H "Authorization: Bearer $KB_LOCAL_READ" -H "Mcp-Session-Id: $S" "$U" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"kb_create","arguments":{"title":"M6 AC-04 probe","summary":"synthetic","body_md":"synthetic","entity_type_id":1}}}'
# в) в БД ничего не появилось
psql kb_local -tAc "select count(*) from kb.entities where title = 'M6 AC-04 probe'"   # ожидается 0
# г) контроль, что тест не пустой: тот же tools/call с $KB_LOCAL_WRITE создаёт запись (count = 1), затем
psql kb_local -c "delete from kb.entities where title = 'M6 AC-04 probe'"
```

То же через существующий smoke (только read-ключ: скрипт берёт сильнейший из окружения, `scripts/smoke-kb-mcp.mjs:4-8@f1c4d6d`):
`env -u GXG_KB_ADMIN_KEY -u GXG_KB_WRITE_KEY GXG_KB_MCP_URL=$U GXG_KB_READ_KEY=$KB_LOCAL_READ node scripts/smoke-kb-mcp.mjs synthetic` (из корня gxg_agents_v2 после `npm ci`).

AC-M6-03: на `f1c4d6d` клон не менялся (`git -C gxg_agents_v2 status --short` пуст). Но «репозиторий KB» — это и `kb_access`, которого здесь нет: для него проверку повторить после доступа.

## Gaps

| # | Severity | Что | Предложение |
| --- | --- | --- | --- |
| G1 | blocker | Кода сервера KB MCP (`kb_access`) нет в gxg_agents_v2: схемы, ошибки, auth, лимиты сервера не проверяемы; AC-M6-04 и AC-M6-02 на реальной KB невыполнимы | Read-доступ к репозиторию `kb_access` (Q7); до этого — wrapper на контракте выше и fake-KB |
| G2 | blocker | Базовой схемы `kb.*` нет в миграциях (`0001_init.sql:1@f1c4d6d`), локальную KB не собрать | DDL без данных от владельца или миграции `kb_access` (Q7) |
| G3 | critical | Ключи на роль, общие для всех агентов; нет агентной изоляции (SEC-02, MCC-03) | Только через wrapper; один служебный read-ключ wrapper'а на connection, агентам ключи KB не выдавать |
| G4 | critical | Клиенты берут сильнейший ключ (admin > write > read): процесс с `GXG_KB_ADMIN_KEY` в окружении работает как admin (`kb-client.ts:5-8@f1c4d6d`) | Wrapper читает только свой read-ключ; `/readyz` проверяет, что write-tools ему не видны |
| G5 | high | Понятия «коллекция» в KB нет (GR-04, RID `kb:…/collection/<id>`) | Коллекция = `entity_type.key` (одна на сущность; фильтр `entity_types[]` есть) — Q1 |
| G6 | high | Привязка ключа к tenant/workspace не определена; RPC профилей `SECURITY DEFINER` верят `p_workspace_id`/`p_id` вызывающего, `kb_profile_history` — без фильтра workspace (`20260703003000…sql:229@f1c4d6d`) | Wrapper сверяет `workspace_id` в ответе `kb_get`; profile-tools не публиковать; владельцу — подтвердить workspace-scope ключей (Q2) |
| G7 | high | `kb_get_context` расширяет связи и не фильтрует коллекции (`SKILL.md:49@f1c4d6d`) — нарушит GR-16 | Не использовать; `kb.context.get` из `kb_search` |
| G8 | high | Read видит черновики: поиск исключает только `archived` (адаптер `:145@f1c4d6d`); поведение сервера — нет в коде | По умолчанию wrapper отдаёт только `status = published` (Q3) |
| G9 | medium | Нет пагинации (только `limit` ≤50) | Окно `limit = offset+limit+1` в адаптере; глубже 50 — конец выдачи; добавить в порт признак «KB обрезала» → `complete:false` |
| G10 | medium | Нет `outputSchema`, форма результата не зафиксирована (`company-knowledge-tool.ts:123-141@f1c4d6d`) | Адаптер валидирует ответ своей схемой; несовпадение → `internal_error`; контрактный тест на записанных синтетических ответах |
| G11 | medium | Нет машиночитаемых кодов ошибок | Маппинг выше; текст KB не передавать |
| G12 | medium | Нет лимита размера ответа `kb_get` (весь `body_md` + связи) | Обрезка в wrapper'е (уже `CONTENT_MAX = 20000`); ограничить чтение тела HTTP-ответа (например 2 МБ) |
| G13 | medium | Rate limit — нет в коде; параллельные вызовы не поддерживаются (`SKILL.md:22@f1c4d6d`) | Семафор 1 на connection, 429 → `rate_limited`; лимит Hub на агента — в ContextForge |
| G14 | medium | Upstream `/mcp`: документ — Railway `gxg-kb-access`, код фронтенда — `hub-origin.agents.gxg-labs.com` (`frontend/next.config.ts:54-64@f1c4d6d`) | Wrapper ходит на явно заданный upstream, не через UI-хост (Q5) |
| G15 | medium | Каталоги tools расходятся (документ / skill / `contracts.ts` / Meeting Voice) | Источник истины — `tools/list` сервера; wrapper вызывает только `kb_search`, `kb_get`, `kb_list_entity_types` и падает в `/readyz`, если их нет |
| G16 | low | `kb_search` без значимых термов (слова <3 символов) возвращает произвольные записи (адаптер `:42-44,145-146@f1c4d6d`) | Для `query` без слова ≥3 символов — пустой результат в wrapper'е или `minLength: 3` по словам |
| G17 | low | Хранимый XSS в карточках KB (SBP-002, `security_best_practices_report.md:56-64@f1c4d6d`) | Wrapper отдаёт текст как данные (SEC-11); вне M6 |
| G18 | low | Лицензии у gxg_agents_v2 нет | Q6 |
| G19 | low | `kb_publish`, `kb_file_*` — без документации и схем | Не публиковать; описать владельцу |

## Открытые вопросы владельцу

Не блокируют каркас; везде метка `pending_owner_decision`, действует вариант по умолчанию.

| # | Вопрос | По умолчанию |
| --- | --- | --- |
| Q1 | Что считать «коллекцией» KB для grants | `entity_type.key` (`insight`, `tool`, …): одна на сущность, поиск фильтрует списком |
| Q2 | Ключ KB привязан к workspace? Один workspace на tenant? | Да: connection = (URL, read-ключ, `workspace_id`) на tenant; wrapper всё равно сверяет `workspace_id` ответа |
| Q3 | Видят ли читатели черновики (`draft`, `enriched`) | Нет: только `published`, без `archived_at` |
| Q4 | Скрывает ли `kb_access` write-tools в `tools/list` для read-ключа | Считаем, что должен; `/readyz` wrapper'а это проверяет |
| Q5 | Какой upstream у `kb.agents.gxg-labs.com/mcp` (Railway `gxg-kb-access` или `hub-origin`) | Wrapper ходит на явно заданный URL KB Access Layer |
| Q6 | Условия использования копий миграций (лицензии нет) | Копии только для тестов/стенда, не распространяются |
| Q7 | Доступ к репозиторию `kb_access` и DDL схемы `kb` без данных | Ждём; до этого AC-M6-02 на fake-KB, AC-M6-04 — процедура выше, не выполнена |

## Что уже сделано на fake-KB

[`connectors/kb`](../../connectors/kb) на ветке `claude/m6-kb`: `kb.documents.search`, `kb.documents.get`, `kb.context.get`; KB за async постраничным портом `Backend` (`m6:connectors/kb/src/kb_connector/backend.py:38-87`), данные KB обрезаются до схем выхода. Проверено через ContextForge на стенде M5 (`m6:stand/tests/test_kb.py`): только `sales` для агента с grant'ом на `sales`, документ другой коллекции или tenant'а → `not_found` побайтно как отсутствующий, ноль вызовов KB при отказе. Это AC-M6-02 **на fake-KB**. Порт совместим с контрактом выше без изменения tools; нужны: `async ready()`, конфигурация connection с `workspace_id` и ключом, признак «KB обрезала выдачу».

## Статус AC

| AC | Статус |
| --- | --- |
| AC-M6-01 | Выполнен в пределах кода gxg_agents_v2: все tools перечислены с выводом; серверные схемы/ошибки/лимиты помечены «нет в коде» (G1) |
| AC-M6-02 | На fake-KB; на реальной KB — после G1/G2 |
| AC-M6-03 | gxg_agents_v2 не изменён на `f1c4d6d`; `kb_access` — после доступа |
| AC-M6-04 | Не выполнен: процедура готова, нужны `kb_access` и схема (G1, G2) |

## Цикл 3 M6: адаптер по контракту (2026-11-22)

`connectors/kb/src/kb_connector/backend.py` — MCP-клиент по разделу «Контракт для адаптера `KbBackend`» с вариантами по умолчанию Q1–Q4, Q6 (решение оркестратора по поручению владельца). `connectors/kb/src/kb_connector/fake_kb.py` — fake-KB того же контракта (ключи по ролям и workspace, черновики в поиске, `kb_get` без проверки workspace, write-tool скрыт от read-ключа); на стенде — отдельный сервис `fake-kb` в internal-сети только с wrapper'ом. Отступления от контракта: `kb_search` всегда с `limit: 50` (не `offset+limit+1`): иначе отброшенные черновики укорачивают страницу; лимит чтения тела HTTP-ответа (G12) — только `max_sse_event_size` SDK (1 МБ), отдельного нет. Не сделано: проверка на настоящем `kb_access` (G1, G2), AC-M6-04 (Q5, Q7).
