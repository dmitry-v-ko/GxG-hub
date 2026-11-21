# M6 — эскалация: нет доступа к `gxg_agents_v2`, нужен read

**Статус:** стоп-эскалация по доступу (раздел «Лимиты и эскалация», пункт про доступ в `reports/GOAL.md`). Остальные части вехи идут дальше.

## Что нужно от владельца

Дать сессии вехи **read-доступ** к `dmitry-v-ko/gxg_agents_v2`. Запрос `add_repo` (owner `dmitry-v-ko`, repo `gxg_agents_v2`, access `read`) 2026-11-21 отклонён политикой разрешений сессии (auto mode: «Unauthorized Access»). Обходить отказ не стали.

Без кода KB не выполнить:

| AC | Почему зависит от `gxg_agents_v2` | Статус |
| --- | --- | --- |
| AC-M6-01 | Gap-аудит описывает реальные tools KB MCP: схемы, коды ошибок, лимиты, auth, изоляцию. Без кода (`docs/current/kb-access-layer-mastra.md` и MCP endpoint) их не узнать | `blocked_no_access` |
| AC-M6-02 | Через ContextForge проверяется wrapper поверх **fake-KB** по контракту из спек. Против реальной KB (локальная копия из кода) — нет | частично: fake-KB |
| AC-M6-03 | Мы ничего не меняли в KB, но показать пустой diff в её репозитории без клона нельзя | `blocked_no_access` (по построению выполнен: репозиторий не клонирован) |
| AC-M6-04 | Отказ записи служебным ключом должна дать **KB**, а не наш fake. Нужна локальная копия KB из её кода (Supabase/Postgres) | `blocked_no_access` |

## Что делается без доступа

Каркас wrapper-коннектора `connectors/kb` на `gxg-connector-kit` с fake-KB по контракту из спек (`service-integration-v1.md` «Knowledge Base», `resource-grants-v1.md`): `kb.documents.search`, `kb.documents.get`, `kb.context.get`, изоляция по коллекциям через grants (GR-01…GR-17), `not_found` для чужих документов (SC-05, SC-10), тесты через ContextForge на стенде M5. Gap-аудит **не выполнен**: `docs/audits/kb-gap-audit.md` — шаблон с допущениями, а не аудит.

## Вопросы владельцу (не блокируют каркас)

| # | Вопрос | По умолчанию | Метка |
| --- | --- | --- | --- |
| 1 | Доступ к `gxg_agents_v2` (read) | Ждём. После доступа — gap-аудит, локальная копия KB, замена fake-KB на HTTP-клиент к KB MCP | `pending_owner_decision` |
| 2 | Контракт fake-KB (имена tools KB, коды ошибок, формат ключей) — догадка по спекам | Wrapper изолирует KB за портом `KbBackend`: после аудита меняется только адаптер, tools и проверки прав остаются | `pending_owner_decision` |

История оценок и итог цикла — ниже (обновляется в конце каждого цикла).
