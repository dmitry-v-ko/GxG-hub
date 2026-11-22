# `kb` — wrapper-коннектор Knowledge Base (M6)

Тонкий MCP-коннектор на FastMCP и `gxg-connector-kit` между ContextForge и KB. Спека: [Интеграция сервисов, KB](../../docs/specs/service-integration-v1.md#knowledge-base). Аудит KB: [kb-gap-audit](../../docs/audits/kb-gap-audit.md) (по `gxg_agents_v2@f1c4d6d`; кода сервера `kb_access` там нет — G1).

**KB-адаптер** (`src/kb_connector/backend.py`) — MCP-клиент к KB Access Layer по контракту аудита: только `kb_list_entity_types`, `kb_search`, `kb_get`, read-ключ connection'а tenant'а в `Authorization: Bearer`, один вызов на connection за раз (G13). Коллекция = `entity_type.key` (Q1); каждая строка сверяется с `workspace_id` connection'а (G6); читателю — только `published` без `archived_at` (Q3); `kb_search` всегда просит окно 50 строк (у KB нет offset), обрезанная окном выдача — `complete: false` (G9). Ошибки KB → коды MCC-05 без текста KB (сеть и 5xx → `temporarily_unavailable`, 429 → `rate_limited`, отказ ключа и дрейф формы → `internal_error`).

**fake-KB** (`src/kb_connector/fake_kb.py`, данные `fixtures/kb.json` в форме `kb.entities`) — MCP-сервер того же контракта для тестов и стенда: ключи по ролям и workspace, черновики в поиске (как у KB), `kb_get` без проверки workspace, write-tool скрыт от read-ключа. В продовом образе удаляется (`KEEP_FAKE_KB=0`).

## Tools (read)

| Tool | Что делает | Права (GR) |
| --- | --- | --- |
| `kb.documents.search` | Поиск по коллекциям, `search-result.schema.json`, cursor | `read` на `kb:<connection>/collection/<id>`; запрос в KB только по разрешённым коллекциям (GR-16), каждый результат проверяется снова; названная коллекция без права → `not_found` до обращения к KB (GR-17) |
| `kb.documents.get` | Документ по ID; `content` до 20 000 символов, `truncated: true`, если документ длиннее | Коллекцию называет KB, не аргумент (GR-01, GR-03). Нет права или чужой tenant → `not_found`, побайтно как отсутствующий (SC-05). Нет права ни на одну коллекцию → `not_found` без обращения к KB |
| `kb.context.get` | Контекст под вопрос: отрывки лучших документов в пределах `max_chars` | Как поиск. Обрезанный результат — `complete: false`, `truncated` |

`kb.documents.create/update` (`write_reversible`, роль curator) — не в M6.

Порядок проверок каждого вызова: identity (`X-GxG-Identity` от ContextForge, ADR-0001 C) → JSON Schema входа → валидный bundle (GR-12) → grants → KB. Одна строка аудита на вызов: `tenant`, `principal`, `bundle_version` (GR-14), `grant_id`, `outcome`, `kb_calls`, без запроса и текста документов (SEC-18). Заголовки и тексты KB обрезаются до лимитов схем выхода (MCC-04).

## Конфигурация

| Переменная | Что |
| --- | --- |
| `KB_IDENTITY_KEYS` | `kid=ed25519:<base64url>[,…]` — ключи адаптера identity ContextForge. Без них все вызовы `unauthenticated` |
| `KB_BUNDLE_DIR`, `KB_BUNDLE_KEYS`, `KB_STATE_FILE` | Подписанный grants bundle и ключи его проверки; состояние `built_at` (GR-13a) |
| `KB_CONNECTIONS_FILE` | `{tenant: {connection_id, url, workspace_id, key_file}}` — одна KB-connection на tenant (Q2). `key_file` — файл-секрет с **read**-ключом wrapper'а; write/admin-ключи wrapper'у не выдаются (G4). Без файла у tenant'ов нет KB и `/readyz` = fail |
| `GXG_PROFILE=test`, `KB_TEST_ISSUER_KEY` | Только тесты: тестовый issuer identity |

Значений секретов в конфигурации и в Git нет: ключ KB только в `key_file`.

## Запуск и тесты

```bash
uv run pytest connectors/kb/tests                      # in-process, из корня репозитория
docker build -f connectors/kb/Dockerfile -t gxg-kb .   # образ
stand/run.sh                                           # стенд M5 + kb: stand/tests/test_kb.py через ContextForge
```

`/healthz` — жив; `/readyz` — загружен валидный bundle и для каждой connection `tools/list` read-ключом показывает три нужных tool'а и **ни одного** write/admin-tool'а (G4, G15, Q4).

## Runbook

| Ситуация | Что делать |
| --- | --- |
| `/readyz` 503, вызовы → `temporarily_unavailable` | Нет валидного bundle или он старше 24 ч. Причина — в логе (`not_ready`, `bundle_rejected`) |
| Все вызовы → `unauthenticated` | `KB_IDENTITY_KEYS` не совпадает с ключом адаптера; причина — поле `reason` в строке аудита |
| Агент не видит документы, которые должен | Нет grant'а `read` на коллекцию в `policies/tenants/<tenant>.yaml` (на стенде — `fixtures/grants/`); `grant_id` в аудите пуст |
| `/readyz` 503, в логе `kb_not_ready` | KB недоступна, отказала ключу, нет нужного tool'а или ключ видит write-tools (выдан не read-ключ). Проверить `KB_CONNECTIONS_FILE` и ключ |
| Все вызовы → `internal_error`, в stderr `ContractError` | KB ответила не по контракту (дрейф формы, G10) или отказала ключу. Сверить контракт аудита с `kb_access` |
| Вызов → `PDP_DENY` | Нет правила `unified_pdp` для агента и tool'а (`stand/unified_pdp.rules.json`) |

## Что сознательно не сделано

- Проверка на настоящей KB: кода `kb_access` и схемы `kb` нет в `gxg_agents_v2` (G1, G2). Адаптер проверен против fake-KB по контракту аудита; AC-M6-04 (запись read-ключом отклоняет KB) — после доступа к `kb_access` (Q5, Q7).
- Runner tool'а и загрузка bundle скопированы из `demo` (`shortcut` в `tools.py`, `server.py`): вынести в kit вместе с шаблоном toolkit.
- `kb_calls` в аудите — общий счётчик процесса: точен для последовательных вызовов (`shortcut`).
- Ключ курсора живёт в процессе: после рестарта или на другой реплике курсор → `invalid_input` (`shortcut`, одна реплика).
