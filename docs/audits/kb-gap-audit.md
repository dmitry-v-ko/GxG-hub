# Gap-аудит KB MCP (M6)

**Статус: аудит НЕ выполнен.** 2026-11-21. Нет read-доступа к `dmitry-v-ko/gxg_agents_v2` ([эскалация](../../reports/M6-ESCALATION.md)). Ниже — шаблон аудита, что известно из спек, и допущения, на которых построен каркас wrapper'а [`connectors/kb`](../../connectors/kb). AC-M6-01 этим документом **не выполняется**.

Источник требований: [service-integration-v1](../specs/service-integration-v1.md#knowledge-base), [MCC-03](../specs/mcp-connector-contract-v1.md#требования), [GR-01…GR-17](../specs/resource-grants-v1.md).

## Что известно без кода

Из спек: у GxG Agents V2 есть KB Access Layer (`docs/current/kb-access-layer-mastra.md`) с MCP endpoint и ключами `read`/`write`/`admin`. KB и её Supabase — единственный источник истины. Спека предупреждает: не считать, что ключи разделяют агентов и tenant'ов, пока это не проверено.

## Таблица аудита (заполняется после доступа)

Одна строка на каждый существующий tool KB MCP. Вывод — одно из трёх: **как есть** (подключить gateway напрямую в ContextForge), **wrapper** (только через `connectors/kb`), **не публиковать**.

| Tool KB | Схемы (in/out, `outputSchema`) | Коды ошибок → MCC-05 | Лимиты (размер ответа, пагинация, таймаут, rate) | Auth (ключ, scope) | Изоляция tenant / агент / коллекция | Вывод |
| --- | --- | --- | --- | --- | --- | --- |
| _не известно_ | — | — | — | — | — | — |

## Что проверить в коде KB

1. **Список tools** MCP endpoint'а и их JSON Schema. Есть ли `outputSchema` и `structuredContent` (MCC-04).
2. **Ошибки.** Как KB отвечает на «нет документа», «нет прав», перегрузку, таймаут. Отображение в `not_found`, `rate_limited` (с `retry_after_s`), `temporarily_unavailable`, `internal_error`. Не утекает ли текст исключения или SQL (SEC-18).
3. **Лимиты.** Максимальный размер ответа, пагинация (cursor или offset), таймауты, rate limits Supabase.
4. **Auth.** Как проверяются ключи `read`/`write`/`admin`: на уровне MCP-слоя или RLS Supabase. Чем ключ отличается от service role.
5. **Изоляция.** Есть ли в KB понятие tenant'а и коллекции. Фильтрует ли поиск по коллекции **в запросе** (нужно для GR-16) или только после. Отдаёт ли `get` коллекцию документа (нужно для GR-01, GR-03).
6. **Аудит.** Что KB пишет в лог: запросы, тексты, ключи.
7. **AC-M6-03.** Хеш коммита KB до и после работ, `git status` и `git diff` пустые. Миграции схемы Supabase не применялись.

## Допущения каркаса wrapper'а (проверить аудитом)

| # | Допущение | Если неверно |
| --- | --- | --- |
| 1 | Ключи KB не различают агентов: права по коллекциям проверяет wrapper через grants (GR) | Если KB сама умеет per-agent scope, wrapper всё равно нужен: identity из ContextForge до KB не доходит (SEC-03) |
| 2 | Одна KB-connection на tenant (`kb:kb-main/collection/<id>`) | Несколько — RID с другим `connection_id`, выбор credentials по connection (MCC-03) |
| 3 | Поиск KB принимает список коллекций и ищет только в них | Если нет — GR-16 требует отдельного запроса на каждую разрешённую коллекцию; фильтр только после поиска недопустим |
| 4 | `get` возвращает коллекцию документа | Если нет — отдельный lookup коллекции до проверки права |
| 5 | `kb.context.get` в KB нет как операции: wrapper собирает контекст из поиска | Если есть — использовать её с теми же проверками коллекций |
| 6 | Коллекции и ID документов — строки без `/` и `*` (формат RID, GR-04) | Экранирование в RID |

## Что уже сделано на fake-KB

[`connectors/kb`](../../connectors/kb): `kb.documents.search`, `kb.documents.get`, `kb.context.get`; KB за портом `Backend` (`backend.py`). Проверено через ContextForge на стенде M5 ([`stand/tests/test_kb.py`](../../stand/tests/test_kb.py)): только `sales` для агента с grant'ом на `sales`, документ другой коллекции или tenant'а → `not_found` побайтно как отсутствующий, ноль вызовов KB при отказе. Это AC-M6-02 **на fake-KB**, а не на KB.

## План AC-M6-04 (после доступа)

1. Поднять локальную копию KB из кода `gxg_agents_v2`: Supabase CLI (`supabase start`) или Postgres + миграции репозитория. Живую Supabase владельца не трогать.
2. Выпустить в локальной копии служебный ключ уровня `read`.
3. Тест: этим ключом вызвать операцию записи KB (create/update документа) напрямую в KB MCP. Ожидается отказ **KB** (ошибка auth/permission), документ не создан (проверка `select` в локальной БД).
4. Тот же ключ — в конфигурации wrapper'а; wrapper не публикует write-tools (manifest), поэтому запись через ContextForge невозможна и без отказа KB.
