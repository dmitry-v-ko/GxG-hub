# M6, цикл 3 — план (ponytail full)

Основание: gap-аудит KB готов (`docs/audits/kb-gap-audit.md`, сделан тредом M11 по клону `gxg_agents_v2@f1c4d6d`; `reports/M6-AUDIT-READY.md`). Решение оркестратора по поручению владельца: по Q1–Q4 и Q6 действуют варианты по умолчанию из аудита (коллекция = `entity_type.key`; одна connection на tenant; читателям только `published`; write-tools скрыты от read-ключа; копии миграций только для тестов). AC-M6-04 остаётся blocked (Q5/Q7: нужен код `kb_access`).

Чек-лист цикла 2 (`reports/critic/M6-cycle-2.json`): пункты 1–4 — теперь 1 и 3 закрываются аудитом и проверкой «gxg_agents_v2 не изменён»; 2 — клиент по контракту аудита против fake-KB того же контракта (реальный `kb_access` недоступен); 4 — blocked. Minor N1–N4 — заодно, где дёшево.

| Что | Как | Ступень |
| --- | --- | --- |
| Адаптер `KbMcpBackend` (`backend.py`) | MCP-клиент (`mcp` SDK, уже зависимость) к `kb_search`, `kb_get`, `kb_list_entity_types` по контракту аудита: read-ключ connection'а в `Authorization: Bearer`, `asyncio.Semaphore(1)` на connection, сверка `workspace_id`, только `published` без `archived_at`, id `^[1-9][0-9]{0,17}$`, окно `limit = min(offset+limit+1, 50)` и признак «KB обрезала» → `complete: false`, маппинг ошибок (сеть/502–504 → `temporarily_unavailable`, 429 → `rate_limited`, auth/дрейф формы → `internal_error`), текст KB наружу не идёт | установленная зависимость + минимальный код |
| `ready()` async (N2) | `tools/list` read-ключом: три нужных tool'а есть, write/admin-tools нет (G4, G15, Q4) | минимальный код |
| Конфигурация | `KB_CONNECTIONS_FILE`: `{tenant: {connection_id, url, workspace_id, key_file}}`; ключ — из файла-секрета, не из Git | stdlib json |
| fake-KB (`fake_kb.py`) | FastMCP-сервер того же контракта: ключи → (workspace, роль), write-tool `kb_create` виден и работает только для write-ключа; данные `fixtures/kb.json` в форме `kb.entities`. Только тесты и стенд: в продовом образе удаляется (как `testctl` у demo) | установленная зависимость |
| Unit-тесты | Все тесты коннектора идут через настоящий адаптер к fake-KB по ASGI (без сокетов) + тесты адаптера: workspace, черновики, ошибки, форма ответа, окно 50 | pytest |
| Стенд | Сервис `fake-kb` в отдельной internal-сети `kb-backend`, её видит только `kb`; `init.py` пишет синтетические ключи и `connections.json` в `stand/secrets/` | конфиг |
| AC-M6-03 | Ссылка на проверку треда M11 (`git status` клона пуст на `f1c4d6d`) | — |
| N1 | Версия bundle — снимок на вызов | одна строка |
| N3 | Тест проверки дублей `merged_grants` | один тест |
| N4 | Имя артефакта CI, лишняя проверка времени | правка |

## DoD-6

| Компонент | Рассмотрено | Решение |
| --- | --- | --- |
| Клиент KB MCP | `mcp` SDK `ClientSession` + `streamable_http_client` (зависимость), fastmcp `Client` | `mcp` SDK: тот же клиент, что в тестах; `http_client` передаётся — подменяется ASGI-транспортом в тестах |
| Сервер fake-KB | FastMCP (зависимость) | FastMCP |
| Проверка ключа fake-KB | Starlette middleware | middleware в 10 строк |

Не делаем: `kb_get_context` и прочие tools KB (аудит: не публиковать); запись (`write_reversible` — не M6).
