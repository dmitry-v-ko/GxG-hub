# `kb` — wrapper-коннектор Knowledge Base (M6)

Тонкий MCP-коннектор на FastMCP и `gxg-connector-kit` между ContextForge и KB. Спека: [Интеграция сервисов, KB](../../docs/specs/service-integration-v1.md#knowledge-base). Аудит KB: [kb-gap-audit](../../docs/audits/kb-gap-audit.md) — **не выполнен**, нет доступа к `gxg_agents_v2` ([эскалация](../../reports/M6-ESCALATION.md)).

**Сейчас KB — синтетический fake-KB** в процессе коннектора (`src/kb_connector/backend.py`, данные `fixtures/kb.json`). Порт `Backend` уже такой, каким должен быть сетевой клиент KB: async (таймаут tool'а из manifest прерывает зависший вызов) и постраничный (`search(..., offset, limit)` → страница и признак «есть ещё»). После аудита пишется клиент KB с этим интерфейсом; tools и проверки прав остаются.

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
| `GXG_PROFILE=test`, `KB_TEST_ISSUER_KEY` | Только тесты: тестовый issuer identity |

Секретов в конфигурации нет: fake-KB не требует ключа. Служебный ключ KB (только read, AC-M6-04) появится с настоящим клиентом KB.

## Запуск и тесты

```bash
uv run pytest connectors/kb/tests                      # in-process, из корня репозитория
docker build -f connectors/kb/Dockerfile -t gxg-kb .   # образ
stand/run.sh                                           # стенд M5 + kb: stand/tests/test_kb.py через ContextForge
```

`/healthz` — жив; `/readyz` — загружен валидный bundle и KB доступна (`Backend.ready()`; fake-KB всегда доступен, клиент KB будет проверять соединение).

## Runbook

| Ситуация | Что делать |
| --- | --- |
| `/readyz` 503, вызовы → `temporarily_unavailable` | Нет валидного bundle или он старше 24 ч. Причина — в логе (`not_ready`, `bundle_rejected`) |
| Все вызовы → `unauthenticated` | `KB_IDENTITY_KEYS` не совпадает с ключом адаптера; причина — поле `reason` в строке аудита |
| Агент не видит документы, которые должен | Нет grant'а `read` на коллекцию в `policies/tenants/<tenant>.yaml` (на стенде — `fixtures/grants/`); `grant_id` в аудите пуст |
| Вызов → `PDP_DENY` | Нет правила `unified_pdp` для агента и tool'а (`stand/unified_pdp.rules.json`) |

## Что сознательно не сделано

- Клиент настоящей KB: протокол неизвестен до аудита (`shortcut` в `backend.py`).
- Runner tool'а и загрузка bundle скопированы из `demo` (`shortcut` в `tools.py`, `server.py`): вынести в kit вместе с шаблоном toolkit.
- `kb_calls` в аудите — общий счётчик процесса: точен для последовательных вызовов (`shortcut`).
- Ключ курсора живёт в процессе: после рестарта или на другой реплике курсор → `invalid_input` (`shortcut`, одна реплика).
