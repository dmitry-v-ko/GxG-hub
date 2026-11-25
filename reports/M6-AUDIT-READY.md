# M6 — gap-аудит KB MCP готов

**Источник:** `dmitry-v-ko/gxg_agents_v2` @ `f1c4d6d9e7bd5202fc703dabf3b0fba3fc732f0f` (клон только для чтения, `git status` пуст). Каркас wrapper'а — `origin/claude/m6-kb` @ `79ce286`. 2026-11-22.

**Главное:** кода сервера KB MCP (`kb_access`) в gxg_agents_v2 нет, базовой схемы `kb.*` в миграциях тоже нет. Аудит построен по документации, MCP-клиентам и in-process реализации read-tools в Mastra. Серверные схемы, ошибки, проверка ключей и лимиты помечены «нет в коде». Ни один tool KB не подключается «как есть». Публикуются только три tool'а wrapper'а `connectors/kb` поверх `kb_search` и `kb_get`.

## Tools KB и выводы

| Tool KB | Роль | Вывод |
| --- | --- | --- |
| `kb_search` | read | **wrapper** → `kb.documents.search` (`entity_types` = разрешённые коллекции) |
| `kb_get` | read | **wrapper** → `kb.documents.get` (коллекция = тип сущности из ответа KB) |
| `kb_get_context` | read | не публиковать: обходит связи, коллекции не фильтрует; `kb.context.get` собирается из `kb_search` |
| `kb_list_entity_types` | read | не публиковать; wrapper вызывает его сам (тип → коллекция) |
| `kb_list`, `kb_get_concept`, `kb_get_relations`, `kb_learning_path`, `kb_stats`, `kb_quality_audit`, `kb_taxonomy`, `kb_taxonomy_list_axes`, `kb_taxonomy_list_terms`, `kb_taxonomy_get_entity`, `kb_profile_get`, `kb_profile_list`, `kb_file_search`, `kb_file_get` | read | не публиковать: обход графа или агрегаты по всей KB (против GR-16), не входят в M6 или не описаны |
| `kb_create`, `kb_update`, `kb_publish`, `kb_relate`, `kb_tag`, `kb_untag`, `kb_profile_update`, `kb_file_sync` | write | не публиковать (write_reversible не входит в M6, нет `idempotency_key`) |
| `kb_taxonomy_create_term`, `kb_taxonomy_update_term`, `kb_taxonomy_audit`, `kb_profile_activate`, `kb_profile_history` | admin | не публиковать |

## Главные gaps

- **G1 (blocker):** в gxg_agents_v2 нет кода `kb_access`. Без него не проверить схемы, ошибки, auth и лимиты сервера, и не выполнить AC-M6-04.
- **G2 (blocker):** в миграциях нет базовой схемы `kb.*`. Все 16 KB-миграций падают на чистом PostgreSQL 16.
- **G3/G4 (critical):** ключи выдаются на роль и общие для всех агентов. Клиенты берут самый сильный ключ из окружения (admin > write > read).
- **G5/G6 (high):** в KB нет понятия «коллекция». Привязка ключа к workspace не определена. `kb_profile_history` не фильтрует по workspace.
- **G7/G8 (high):** `kb_get_context` выходит за пределы коллекций. Read-ключ видит черновики.
- **G9–G15 (medium):** нет пагинации (только `limit` ≤ 50), нет `outputSchema` и кодов ошибок, нет лимита размера ответа, rate limit в коде нет. Параллельные вызовы не поддерживаются. Upstream `/mcp` в документации и в коде расходится. Каталоги tools тоже расходятся.

## Вопросы владельцу (`pending_owner_decision`, не блокируют)

Q1 — коллекция = `entity_type.key`. Q2 — одна connection (URL, read-ключ, `workspace_id`) на tenant. Q3 — читателям только `published`. Q4 — `kb_access` прячет write-tools от read-ключа (проверяет `/readyz`). Q5 — какой upstream стоит за `kb.agents.gxg-labs.com/mcp`. Q6 — у gxg_agents_v2 нет лицензии, копии миграций только для тестов. Q7 — доступ к репозиторию `kb_access` и DDL `kb` без данных.

## Статус AC

AC-M6-01 выполнен в пределах кода, серверная часть отмечена «нет в коде». AC-M6-02 проверен только на fake-KB. По AC-M6-03 gxg_agents_v2 не изменён; `kb_access` нужно проверить после получения доступа. AC-M6-04 не выполнен: процедура (curl и SQL) описана в аудите, но для неё нужны `kb_access` и схема.

## Файлы

- [docs/audits/kb-gap-audit.md](../docs/audits/kb-gap-audit.md): аудит, контракт `KbBackend`, локальная копия, gaps, вопросы.
- [connectors/kb/vendor/kb-migrations/](../connectors/kb/vendor/kb-migrations/SOURCE.md): 16 KB-миграций дословно, `SOURCE.md` с commit, sha256 и статусом лицензии.
