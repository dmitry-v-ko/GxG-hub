# Gap-аудит KB MCP (M6)

**Статус: аудит выполнен частично.** 2026-11-22. AC-M6-01 закрыт для того, что есть в `gxg_agents_v2`. Код самого KB MCP endpoint'а в этом репозитории **отсутствует**, поэтому схемы, коды ошибок и проверку ключей на стороне сервера по коду подтвердить нельзя. Ниже везде помечено, что проверено по коду, а что взято из документации.

Источник требований: [service-integration-v1](../specs/service-integration-v1.md#knowledge-base), [MCC-03, MCC-05](../specs/mcp-connector-contract-v1.md#требования), [GR-01…GR-17](../specs/resource-grants-v1.md).

## Источник

| | |
| --- | --- |
| Репозиторий | `dmitry-v-ko/gxg_agents_v2`, только чтение |
| Commit | `f1c4d6d9e7bd5202fc703dabf3b0fba3fc732f0f` (`main`, 2026-11-20, «docs(specs): define autonomy as per-call or within-approved-Experiment (#339)») |
| Рабочее дерево | `git status --porcelain` пустой; в репозитории ничего не менялось и не пушилось |
| История | полная, 1671 commit, не shallow; поиск по истории ниже идёт по ней |

Файлы, на которых основан аудит (пути от корня `gxg_agents_v2`):

| Файл | Что даёт |
| --- | --- |
| `docs/current/kb-access-layer-mastra.md` | URL, переменные ключей, роли и списки tools, локальный запуск |
| `docs/current/config.md:737`, `:758-759` | прод-URL, Railway target, `/mcp` в обход Basic Auth UI |
| `ai/skills/mcp-kb/SKILL.md` (v1.2.0) | второй, **другой** список tools, таймауты, примеры аргументов |
| `mastra/src/mastra/mcp/kb-client.ts` | как Mastra подключается к KB MCP: Bearer, выбор ключа |
| `scripts/smoke-kb-mcp.mjs` | smoke против KB MCP: имена tools, аргументы |
| `mastra/src/mastra/domains/knowledge-base/domain/contracts.ts` | третий список имён tools (22 шт.) |
| `mastra/src/mastra/domains/knowledge-base/ports/knowledge-base-port.ts` | сигнатуры операций (in-process) |
| `mastra/src/mastra/domains/knowledge-base/adapters/supabase-knowledge-base-adapter.ts` | in-process реализация операций поверх Supabase PostgREST |
| `mastra/src/mastra/domains/voice/knowledge-toolsets.ts` | Zod-схемы входа `kb_search`/`kb_get`/`kb_get_context` (in-process) |
| `mastra/src/mastra/mcp/meeting-voice-kb.ts` | отдельный MCP-пир `kb_file_search`/`kb_file_get` с ключами по workspace |
| `mastra/src/mastra/tools/company-knowledge-tool.ts:125` | какие формы ответа KB ожидает потребитель |
| `docs/plans/capability-hub-remediation.md:330` | известный gap: `kb_create` и `kb_publish` оба требуют `write` |
| `supabase/migrations/*kb*` (16 файлов), `supabase/config.toml` | схема БД KB, RLS, grants |

## Главный вывод: KB MCP живёт вне `gxg_agents_v2`

1. **Сервера нет в репозитории.** KB Access Layer запускается из отдельного checkout'а: `cd /Users/dk1/go/Hermes/kb_access` … `KB_REPOSITORY=memory KB_AUTH_DISABLED=true PORT=8090 python3 main.py` (`docs/current/kb-access-layer-mastra.md:82-83`). В `gxg_agents_v2` нет ни `main.py`, ни Python-кода MCP-сервера KB. Прод: `https://kb.agents.gxg-labs.com/mcp` → `https://gxg-kb-access-production.up.railway.app/mcp` (`config.md:758-759`). Отдельного репозитория `kb_access` в списке доступных этой сессии нет: есть только `hermes-agent-admin` и `hermes-desktop`, их содержимое не проверял и не подключал.
2. **Базовой схемы `kb` тоже нет.** Миграции KB в `supabase/migrations` только **меняют** `kb.entities`, `kb_taxonomy_*` и функции `kb.update_updated_at()`, `kb.auto_version_entity()`, но нигде их не создают. Проверено:
   - по всей истории `git log --all -G 'create schema.*\bkb\b'` и `-G 'create table( if not exists)? kb\.entities'` (без учёта регистра) — пусто;
   - на чистом Postgres 16 с заглушками ролей Supabase (`anon`, `authenticated`, `service_role`, `auth.uid()`): `0001_init.sql` применяется, первая же KB-миграция падает:
     ```text
     psql:20260528000000_kb_entities_add_public.sql:5: ERROR:  schema "kb" does not exist
     ```
     Так же падает прогон одних KB-миграций. Значит, `supabase db reset` в этом репозитории KB не поднимает.
3. **Три расходящихся списка tools.** Доку (19 имён), skill `mcp-kb` (23 имени) и `contracts.ts` (22 имени) нельзя свести без кода сервера. Есть только в skill: `kb_list`, `kb_get_concept`, `kb_get_relations`, `kb_learning_path`, `kb_stats`, `kb_taxonomy`. Есть только в `contracts.ts`: `kb_publish`, `kb_quality_audit`. Реальный `tools/list` прод-endpoint'а аудит не вызывал: ключей нет, и это внешний вызов.

Что из этого следует для M6: для полного AC-M6-01 и для AC-M6-03/04 нужен read-доступ к коду `kb_access` (MCP-сервер и его миграции). Без него пункты, помеченные «не подтверждено», остаются открытыми.

## Auth и изоляция (общие для всех tools)

**Auth.** Один Bearer-токен на соединение: `headers.set('Authorization', \`Bearer ${token}\`)` (`kb-client.ts:24`). Ключ выбирается по старшей роли, `GXG_KB_ADMIN_KEY || GXG_KB_WRITE_KEY || GXG_KB_READ_KEY` (`kb-client.ts:5-8`, `SKILL.md:33`). Роли `read ⊂ write ⊂ admin` вложены (`kb-access-layer-mastra.md:67-69`). Формат ключа не документирован: потребитель лишь валидирует непустую строку до 4096 символов (`meeting-voice-kb.ts:13`, `z.string().min(1).max(4_096)`). Где и как сервер проверяет роль, по коду не подтверждено. Известно, что `KB_AUTH_DISABLED=true` выключает проверку целиком (`kb-access-layer-mastra.md:83`). Ещё известно, что create и publish требуют одну и ту же роль `write`, и разделить их по shared-соединению нельзя (`capability-hub-remediation.md:330`).

**Tenant-изоляция: нет.** Ключи различают только роль. В БД есть `workspace_id` (`kb.entities`, `kb_taxonomy_axes`, `kb.profiles`), но прод работает в одном workspace `00000000-0000-0000-0000-000000000000` (константа в `20260606120000_entity_type_to_taxonomy.sql:8`, в `kb.profiles` default `20260703000000_kb_profiles.sql:5`). Как MCP-сервер выбирает workspace, по коду не подтверждено. In-process адаптер **игнорирует** workspace вызывающего и всегда подставляет свой из конфига: `private workspace() { return this.config.databaseWorkspaceId; }` (`supabase-knowledge-base-adapter.ts:100`). Voice передаёт `workspaceId: 'voice'` (`knowledge-toolsets.ts:11`), и это значение нигде не используется. Часть запросов адаптера идёт вообще без фильтра по workspace: `listEntityTypes`, `listTerms` без `axis_key`, `kb_entity_relations` в `get`.

**Агентная изоляция: нет.** Все агенты одной роли получают один ключ (`kb-access-layer-mastra.md:58-60`). Identity вызывающего до KB не доходит. Исключение — отдельный пир meeting-voice: ключи по workspace из `GXG_MEETING_VOICE_KB_READ_KEYS_JSON` (`meeting-voice-kb.ts:37`, `:129`) и tools `kb_file_search`/`kb_file_get` (`:173`). Что сервер делает с этими ключами, не подтверждено.

**Коллекций нет.** У KB нет понятия «коллекция». Ближайшие аналоги — термины таксономии: оси `domain`, `pillar`, `entity_type`, `tool_category`. У сущности может быть несколько терминов на одной оси. Для GR-01 и GR-16 коллекцию нужно определить через таксономию (см. «Контракт `KbBackend`»).

**RLS.** Политик на `kb.entities` в репозитории нет: они живут там же, где базовая схема. View `public.kb_entities` создаётся с `security_invoker = true` (`20260701000000_fix_security_advisor_findings.sql:1-2`), так что права определяют политики `kb.entities`. Frontend читает `kb_*` views ключом `NEXT_PUBLIC_KB_SUPABASE_ANON_KEY` (`frontend/src/lib/supabase-kb.ts:5`). `kb.profiles*` закрыты для `anon`/`authenticated`, функции `kb_profile_*` выполняет только `service_role` (`20260703003000_…sql:233-243`). На `kb_entity_feedback` есть открытые политики `SELECT USING (true)` и `INSERT WITH CHECK (true)` (`20260608120000_…sql:18-19`, `20260710110000_…sql:11`).

**Аудит-лог.** In-process адаптер пишет только `{operation, workspaceId, entityId, outcome}` для мутаций (`supabase-knowledge-base-adapter.ts:13-17`). Что пишет MCP-сервер, не подтверждено.

## Таблица аудита

Обозначения. **D** — документация (`kb-access-layer-mastra.md`), **S** — skill `mcp-kb`, **C** — `contracts.ts`, **IP** — in-process адаптер (`supabase-knowledge-base-adapter.ts`, `knowledge-toolsets.ts`). Схемы IP — это **реализация Mastra**, а не MCP-сервер. Выход MCP-сервера может отличаться: потребитель на всякий случай принимает массив или `{items}`, `{entities}`, `{item}` (`company-knowledge-tool.ts:125-133`). Ошибки MCP-сервера не подтверждены ни для одного tool, это общий пробел.

Вывод — одно из трёх: **как есть** (gateway в ContextForge напрямую), **wrapper** (только через `connectors/kb`), **не публиковать**. «Как есть» не подходит ни для одного tool: ключи не разделяют tenant'ов и агентов, а identity из ContextForge до KB не доходит (SEC-03).

| Tool KB | Где | Схемы in → out | Ошибки | Лимиты | Ключ | Изоляция | Вывод |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `kb_search` | D S C IP | in: `{query: string≥1, limit?: int 1..100 (def 10), entity_types?: string[], domain?: string, pillar?: string, include_body?: bool (def false)}` (`knowledge-toolsets.ts:32-39`) → out (IP): массив строк `kb_entities` `{id: int, title, summary, entity_type_id, status, confidence, data, created_at, updated_at[, body_md]}`, архивные исключены (`adapter:145`) | IP: `Error('Knowledge Base storage is unavailable.')` при HTTP ≥500, иначе `'…request failed.'` + `status` (`adapter:32-36`) | IP: фактический лимит `min(limit,50)` (`adapter:167`), до 200 кандидатов из БД, **пагинации нет** (ни offset, ни cursor). S: таймаут 120 с, connect 30 с, без параллельных вызовов (`SKILL.md:21-22`) | read | нет; **фильтр `domain`/`pillar` применяется после выборки** (`adapter:153-163`) | **wrapper** → `kb.documents.search`; фильтр по разрешённым коллекциям обязан уйти в запрос (GR-16), подтвердить на сервере |
| `kb_get` | D S C IP | in: `{id: int>0}` (`knowledge-toolsets.ts:46`) → out (IP): строка сущности + `taxonomy[]` + `relations{outgoing, incoming}` или `null` (`adapter:132-141`) | IP: `null` при отсутствии; ошибки хранилища как выше | один объект, тело целиком (размер не ограничен) | read | нет; связи выбираются без фильтра workspace | **wrapper** → `kb.documents.get`; коллекцию брать из `taxonomy` ответа (GR-01, GR-03), `relations` наружу не отдавать |
| `kb_get_context` | D S C IP | in: `{topic?: string, query?: string, max_tokens?: int 1..16000 (def 1600)}` (`knowledge-toolsets.ts:55-58`) → out (IP): `{topic, markdown, entities: [{id, title}]}` (`adapter:170-181`) | IP: `Error('topic or query is required.')` | IP: 10 сущностей, `max_tokens` считается в **словах**, а не токенах (`adapter:174-177`) | read | нет; ищет по всей KB | **wrapper**: `kb.context.get` собирать в wrapper'е из отфильтрованного поиска; upstream-tool не вызывать, пока он не принимает фильтр коллекций |
| `kb_taxonomy_list_axes` | D C IP | in: `{include_inactive?: bool}` → строки `kb_taxonomy_axes` | как выше | без пагинации | read | фильтр по workspace (IP) | **не публиковать**; wrapper может использовать для карты коллекций |
| `kb_taxonomy_list_terms` | D C IP | in: `{axis_key?: string, include_inactive?: bool}` (smoke: `{axis_key: 'domain'}`) → строки терминов + `axis` | как выше | без пагинации | read | без `axis_key` — без фильтра workspace | **не публиковать**; wrapper: список коллекций (`list_collections`) |
| `kb_taxonomy_get_entity` | D C IP | in: `{entity_id: int}` → назначения терминов | IP: `'KB entity was not found.'` | — | read | фильтр по workspace | **не публиковать**; wrapper: коллекция документа, если её нет в `kb_get` |
| `kb_list_entity_types` | S C IP | in: `{}` → `[{id, key, label, is_active}]` | — | — | read | без workspace | не публиковать |
| `kb_profile_get` / `kb_profile_list` | D S C IP | in: `{workspace_id?}` (+`include_archived?`) → активный профиль `{…, body_md}` / метаданные | IP: `'KB workspace is not permitted.'` при чужом workspace (`adapter:103`) | — | read | workspace = конфиг | не публиковать (инструкции курации, не данные) |
| `kb_quality_audit` | C IP | in: `{limit?}` → `{sampled, issue_counts, issues}` | — | N+1 `get` на каждую опубликованную | read? (не подтверждено) | — | не публиковать |
| `kb_list`, `kb_get_concept`, `kb_get_relations`, `kb_learning_path`, `kb_stats`, `kb_taxonomy` | только S | только описания в SKILL.md | — | — | read | — | **не публиковать**: существование не подтверждено |
| `kb_create` | D S C IP | in: поля сущности (`title, summary, entity_type_id, body_md, data, …`); `status: 'published'` запрещён (`adapter:227-231`) → созданная строка | IP: `'Create entities as draft; use kb_publish to publish.'` | — | write | нет | **не публиковать в M6** (по спеке это `write_reversible` для curator позже; AC-M6-04 требует ключ только на чтение) |
| `kb_update` | D S C IP | in: `{id, patch}`, поля `title, summary, body_md, status, confidence, data, entity_type_id` → строка | IP: `'No supported fields to update.'`, `'Use kb_publish …'` | — | write | нет | **не публиковать в M6** (так же) |
| `kb_publish` | C IP | in: `{id}` → строка; publish gate (`publishFailures`) | IP: `'Publish gate failed: …'` и при этом **меняет** статус на `enriched` | — | write (`capability-hub-remediation.md:330`) | нет | не публиковать |
| `kb_relate`, `kb_tag`, `kb_untag`, `kb_profile_update` | D S C IP | связи, термины, черновик профиля | IP: `'Taxonomy term was not found.'` и т.п. | — | write | нет | не публиковать |
| `kb_taxonomy_create_term`, `kb_taxonomy_update_term`, `kb_taxonomy_audit`, `kb_profile_activate`, `kb_profile_history` | D S C IP | таксономия и профили | IP: `'Taxonomy axis was not found.'` | — | admin | нет | не публиковать |
| `kb_file_search`, `kb_file_get` | `meeting-voice-kb.ts` | схемы берутся из каталога пира, в коде не зафиксированы | все ошибки маскируются в `meeting_voice_kb_read_failed` (`:51-58`, `:102`) | таймаут 5 с (`:8`) | read-ключ на workspace | per-workspace ключ (не подтверждено на сервере) | не публиковать (отдельный пир, вне M6) |

## Контракт `KbBackend` для `connectors/kb`

Порт — `Backend` в `connectors/kb/src/kb_connector/backend.py`: `list_collections(tenant)`, `get_document(tenant, id) -> Document | None`, `search(tenant, collections, query, *, offset, limit) -> (docs, has_more)`. Что должен уметь сетевой адаптер (код `connectors/kb` этим аудитом не менялся):

| | |
| --- | --- |
| URL | `GXG_KB_MCP_URL`; прод `https://kb.agents.gxg-labs.com/mcp`, локально `http://127.0.0.1:8090/mcp` (`smoke-kb-mcp.mjs:3`). Транспорт — MCP по HTTP (Streamable HTTP), не stdio (`SKILL.md:16-17`). Одна connection на tenant (`kb:kb-main/…`) |
| Auth | заголовок `Authorization: Bearer <read key>`, только ключ роли `read`. Ключ непрозрачный, до 4096 символов. Хранить как секрет connection, в лог не писать |
| Методы | `initialize` → `tools/list` → `tools/call`. На `/readyz` (MCC-09) проверять, что в `tools/list` есть `kb_search`, `kb_get`, `kb_taxonomy_list_terms`. Скрывает ли сервер write-tools от read-ключа, не известно: зафиксировать при AC-M6-04 |
| `list_collections` | `kb_taxonomy_list_terms {axis_key: <ось коллекций>}` → ключи терминов |
| `get_document` | `kb_get {id: int}`; `Document.id` = `str(id)`, коллекция = термины оси коллекций из `taxonomy`; `null` → `None` |
| `search` | `kb_search {query, limit, <фильтр коллекции>, include_body: true}`; на каждую разрешённую коллекцию отдельный вызов, если сервер не принимает список (GR-16) |
| Пагинация | **upstream её нет.** Максимум 50 результатов (IP). Адаптер запрашивает `limit = offset + limit + 1 ≤ 50`, режет страницу и выставляет `has_more`. `offset + limit ≥ 50` → честно `has_more = false` плюс флаг усечения, или отказ `invalid_argument`. Решает тред-владелец M6 |
| Ошибки → MCC-05 | HTTP 401/403 → `internal_error` (неверная конфигурация ключа, не раскрывать); 429 → `rate_limited` с `retry_after_s` из `Retry-After`; 5xx, таймаут, обрыв → `temporarily_unavailable`; `isError: true` от tool → `internal_error`; `kb_get` → `null` → `not_found`. Текст upstream-ошибки наружу не передавать (SEC-18). Точные коды сервера не подтверждены |
| Таймаут | свой на вызов (в wrapper'е уже есть `asyncio.wait_for`); 120 с из SKILL.md для tool-вызова агента слишком много |
| Параллельность | SKILL.md: параллельные вызовы не поддерживаются. Один вызов за раз на connection или пул сессий |

Решения, которые нужны до кода адаптера (открытые вопросы для треда-владельца M6):

1. **Какая ось задаёт коллекцию.** Кандидат — `domain` (13 терминов). Сущность может иметь несколько `domain`. Нужно правило: доступ, если разрешён **любой** из терминов, или если **все**? Безопаснее «все».
2. **Фильтр поиска.** `kb_search` принимает один `domain` (IP), и фильтрует после выборки из 200 кандидатов. Если на сервере так же, GR-16 не выполнен: нужен серверный фильтр или изменение KB, а это противоречит AC-M6-03.
3. **Workspace.** Как MCP-сервер выбирает `workspace_id` и можно ли выпустить read-ключ, привязанный к одному workspace (как в meeting-voice).

Допущения прежнего шаблона: № 1 (ключи не различают агентов) — подтверждено. № 2 (одна connection на tenant) — совместимо, в KB один workspace. № 3 (поиск по списку коллекций) — **не подтверждено, вероятно неверно**. № 4 (`get` отдаёт коллекцию) — частично: `taxonomy` в ответе IP есть, но коллекций как понятия нет. № 5 (`kb.context.get` собирать в wrapper'е) — подтверждено: upstream `kb_get_context` есть, но фильтра коллекций в нём нет. № 6 (ID без `/` и `*`) — подтверждено: ID целые числа, ключи терминов — slug.

## Локальная копия KB для AC-M6-03 и AC-M6-04

**Из `gxg_agents_v2` поднять нельзя.** Нет кода сервера и базовой схемы `kb` (см. главный вывод; падение миграций воспроизведено выше). Поэтому копию миграций в `connectors/kb/vendor/kb-migrations/` я **не** делал: 16 инкрементальных файлов без базовой схемы ничего не поднимают. Файла лицензии в `gxg_agents_v2` нет, ни `LICENSE*`, ни поля `license`. Две миграции (`20260628143000_kb_domain_tree_from_hermes.sql`, `20260628152000_kb_type_guidance.sql`) несут контент KB, а не схему.

Что нужно от владельца (одно из двух):

- **A (предпочтительно).** Read-доступ к репозиторию `kb_access` (MCP-сервер и его миграции). Тогда копия миграций идёт в `connectors/kb/vendor/kb-migrations/` с commit и лицензией, как `connectors/telegram/archive` в `claude/m11-telegram-read`.
- **B.** Дамп **только схемы** прод-KB без данных: `pg_dump --schema-only --no-owner --no-privileges --schema=kb`, плюс `public.kb_*` views и функции `kb_profile_*` и политики RLS. Дамп снимает владелец; клиентских данных в нём нет. Сервер всё равно нужен из A.

План, когда A или B есть:

1. **AC-M6-03, до работ.** Записать `git rev-parse HEAD` и `git status --porcelain` репозитория `kb_access` и `gxg_agents_v2` (сейчас `f1c4d6d9…`, чисто). Снять `pg_dump --schema-only` локальной копии как эталон схемы. Миграции к живой Supabase не применять.
2. **Поднять БД.** Supabase CLI: `supabase start` в отдельной копии каталога `supabase/` (порты из `config.toml`: API 54321, DB 54322). Или Postgres 16 с ролями `anon`, `authenticated`, `service_role`. Применить базовую схему, затем миграции по порядку имён. Seed выключен (`[db.seed] enabled = false`). Синтетические документы в двух доменах, например `sales` и `ops`.
3. **Поднять KB Access Layer** против локальной БД в Supabase-режиме, **не** в `KB_REPOSITORY=memory` и **не** с `KB_AUTH_DISABLED=true`: оба режима делают AC-M6-04 бессмысленным. Имена переменных для ключей read/write/admin и для подключения к Supabase брать из кода `kb_access`; здесь они не известны.
4. **Выдать read-ключ.** Сгенерировать случайный токен (`openssl rand -hex 32`) и прописать его серверу только как ключ роли `read`. Write- и admin-ключи не задавать или задать другими значениями.
5. **AC-M6-04, отказ записи.** Тем же read-ключом напрямую в KB MCP вызвать `tools/call kb_create {title: "probe", …}` и `kb_update {id, patch}`. Ожидается отказ **от KB**: auth- или permission-ошибка, или tool скрыт для роли `read`. Затем `select count(*) from kb.entities where title = 'probe'` → `0`, а `updated_at` у цели `kb_update` не изменился. Контроль: тот же вызов write-ключом проходит, иначе тест ничего не доказывает. Отдельно: wrapper не публикует write-tools (manifest), так что через ContextForge запись невозможна и без отказа KB.
6. **AC-M6-03, после работ.** Повторить шаг 1: `git status` и `git diff` пустые, `HEAD` тот же, schema-дамп совпадает с эталоном побайтно.

## Что уже сделано на fake-KB

[`connectors/kb`](../../connectors/kb): `kb.documents.search`, `kb.documents.get`, `kb.context.get`. KB сидит за async постраничным портом `Backend` (`backend.py`), данные обрезаются до схем выхода. Проверено через ContextForge на стенде M5 ([`stand/tests/test_kb.py`](../../stand/tests/test_kb.py)): агент с grant'ом на `sales` видит только `sales`; документ другой коллекции или tenant'а → `not_found`, побайтно как отсутствующий; при отказе ноль вызовов KB. Это AC-M6-02 **на fake-KB**, а не на KB.
