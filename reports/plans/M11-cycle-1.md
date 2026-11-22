# M11 — цикл 1: план (ponytail ultra)

Веха: [M11 — Telegram read-only](../../docs/implementation-plan-v1.md#m11--telegram-read-only), AC-M11-01…05. Порог 9 («остальные вехи»).
База: `claude/m5-readonly-poc` (`fb14af1`, M5 = 9.5) + `docs/` и `reports/GOAL.md` из `claude/admiring-planck-vzys0x`.
Существующий сервис: `dmitry-v-ko/gxg_agents_v2` @ `f1c4d6d` (клон только для чтения): `docs/current/telegram-ingestion.md`,
`docs/specs/platform/capability-hub/connectors/telegram.md` (CON-TG-01…13), `mastra/src/mastra/connectors/telegram/`.

## Что есть у существующего сервиса (шаг 0)

| Что | Где в gxg_agents_v2 | Что берём |
| --- | --- | --- |
| Session-owner, `PrimarySessionLock`, flood-wait без сна (`retry_after_seconds`) | `telegram_ingest/`, `docs/current/telegram-ingestion.md` | Коннектор сессию не открывает и Telegram-библиотек не тянет. Ошибки `busy`, `flood_wait`, `auth_required`, `chat_forbidden`, `peer_inaccessible`, `provider_error` — классы порта |
| Scope Connection: `chats[{chat_ref, since?}]`, `folders`, `global_search` (CON-TG-02) | `connection-scope.ts` | Внешняя граница. Grants Hub её только сужают |
| `chat_ref` = `tgc1_` + 32 base64url, привязан к Connection, peer ID не раскрывает (CON-TG-09) | `chat-ref.ts`, `connection-scope.ts:4` | `<id>` в RID чата: `telegram:<connection>/chat/<chat_ref>` |
| Поиск по архиву: `{connection_id, query 1..1000, chat_refs 1..128, limit 1..100}` → `items[{chat_ref, message_id, sent_at, edited_at, text, caption, reply_to, media_type, rank}]`, `complete`, `partial_reasons ⊂ {result_limit, semantic_pending, sync_gap, not_synced}`, `chats[{chat_ref, synced_at}]` | `archive-repository.ts` `telegramArchiveSearchInputSchema`, `search()` | Метод порта `search` — тот же контракт |
| История чата, сбор по cursor | нет; целевой контракт — `thread.read` «archive + live tail», Chat folders collection с позицией `(archived_us, message_id)` | Методы порта `read_thread`, `collect` по целевому контракту CON-TG; позиция сбора — та же пара |
| Сетевой порт для внешнего клиента | **нет**: только in-process TS (`PostgresTelegramArchiveRepository`), HTTP — admin-маршруты Capability Hub | См. «Решение по порту» |

**Решение по порту.** У сервиса нет сетевого API для чтения архива, а менять gxg_agents_v2 нельзя. Поэтому порт — Python `Protocol` `TelegramService` в коннекторе с контрактом из таблицы. Единственная реализация в этом цикле — `FakeTelegramService` (fixtures, счётчики запросов, счётчик сессий, режимы отказа). HTTP-клиент к несуществующему endpoint не пишется. Вопрос владельцу — `pending_owner_decision` в `reports/M11-ESCALATION.md`: где открыть порт (HTTP-маршрут Capability Hub в gxg_agents_v2 или TS-коннектор на MCP TypeScript SDK по mcp-toolkit-v1). По умолчанию — HTTP-адаптер к маршруту сервиса, отдельной задачей после его появления. Прогон на тестовом аккаунте Telegram — тоже `pending_owner_decision`, по умолчанию не делается.

## Вопросы к ultra: что из задания не нужно

| Пункт | Решение |
| --- | --- |
| HTTP-клиент к сервису | Не нужен: endpoint нет. Порт + fake (см. выше) |
| Live tail в `thread.read`, semantic search | Задача сервиса (CON-TG «Tail top-up», CON-TG-07), не фасада. Коннектор передаёт `partial_reasons` сервиса |
| Вынос конвейера вызова (`DemoTool.run`) в kit | Не сейчас: трогает M1/M3 и их mutmut. Копия ~60 строк с `shortcut`, вынос — при третьем коннекторе или генераторе mcp-toolkit |
| HMAC-cursor в `collect` | Не нужен: Signals хранит cursor долго, а ключ процесса после рестарта другой. Позиция не секрет, чат проверяется заново на каждом вызове |
| Cursor в `thread.read` | Не нужен: `before_message_id` (целое). Чат проверяется на каждом вызове |
| `telegram.message.propose` | Этап E, не M11 |
| Отдельный стенд | Нет: коннектор `telegram` добавляется в стенд M5 (`compose.yaml`) |

## Места, которых касается изменение

1. `connectors/telegram/` (новый, как `demo`): `gxg-manifest.yaml` (3 tool), `schemas/*.json`, `src/telegram_connector/{service.py, fake.py, tools.py, server.py, testctl.py, __main__.py}`, `fixtures/{service.json, grants/t-*.yaml}`, `tests/test_telegram.py`, `Dockerfile`, `pyproject.toml`, `uv.lock`, `README.md`.
2. `pyproject.toml` (корень): путь к коннектору, `testpaths`, mypy_path, isort.
3. `compose.yaml`: сервис `telegram` в сети `connectors` (10.250.0.11), без порта. `SSRF_ALLOWED_NETWORKS` расширить до адреса `telegram`.
4. `stand/init.py`: bundle из grants обоих коннекторов (слияние файлов tenant'а) и обоих manifest; `stand/bootstrap.py`: gateway `telegram-<tenant>`, connectors-map плагина; `stand/unified_pdp.rules.json`: allow для telegram-tools; `stand/run.sh`: сборка образа `telegram`.
5. `stand/tests/test_telegram.py`: SC-03, SC-04, SC-05, SC-09, SC-10, MC-02, MC-03 и AC-M11-02…05 через ContextForge; бэклог M5 (2) — заголовок `Authorization` агента не доходит до коннектора; (3) — роль member не может переименовать tool или зарегистрировать gateway.
6. `stand/report.py` / `reports/conformance/M11.json`.
7. `policies/platform/registry.example.yaml`: запись `telegram`, если `gxg-policy build` её требует.
8. `scripts/ci/mutmut.sh`, `.github/workflows/ci.yml`: mutmut и тесты нового коннектора.

## Лестница по частям

| Часть | Ступень | Почему |
| --- | --- | --- |
| Identity, grants, bundle, ошибки, редакция | 2: kit (`ContextForgeVerifier`, `BundleStore`, `GxgError`, `redact`) | Как `demo` |
| MCP-сервер, `/healthz`, `/readyz` | 4: FastMCP (уже в lockfile `demo`) | — |
| Конвейер вызова | 2→6: копия `DemoTool.run` (`shortcut`) | См. ultra |
| Порт сервиса | 3: `typing.Protocol` | — |
| Экранирование управляющих символов | 3: `unicodedata.category` (Cc, Cf) → `\uXXXX` | Своё одна функция; CON-TG-08 |
| Cursor поиска | 2: HMAC-схема `demo` | Тот же вид пагинации |
| Fake-сервис | 6: минимальный код поверх JSON-fixtures | Готового fake Telegram-архива нет; telethon/pyrogram — это сессия, ровно то, чего нельзя |
| Стенд и живые тесты | 2: `stand/` M5, `standlib.Stand` | Только параметризация по коннектору |

## DoD-6: готовые решения для своего кода

| Компонент | Рассмотрено | Решение |
| --- | --- | --- |
| `TelegramService` (порт) | TS-репозиторий сервиса (in-process, Node), MCP-сервер сервиса (нет), Capability Hub HTTP (только admin, без чтения архива) | Свой Protocol по контракту сервиса: сетевого порта нет |
| `FakeTelegramService` | `scripts/telegram_mock.py` gxg_agents_v2 (мок CLI Python-процесса с сессией, не архива), telethon test servers (настоящая сессия), `demo.Backend` | Свой fake: мок сервиса ниже уровня порта не подходит, а `demo.Backend` — другой домен |
| Экранирование текста | `str.isprintable`, `unicodedata`, `bleach` (HTML) | `unicodedata.category`: stdlib |
| Конвейер вызова | `DemoTool` (копия), FastMCP middleware | Копия: middleware FastMCP не видит итог для аудита одной строкой и даёт обход при регистрации tool без него |
| Слияние grants стенда | `gxg-policy build` с несколькими каталогами (нет), yq | 10 строк в `init.py`: PyYAML уже есть |

## Как проверяются AC

| AC | Тест |
| --- | --- |
| AC-M11-01 | `stand/tests/test_telegram.py` через ContextForge: `test_sc03_*`, `test_sc04_*`, `test_sc05_*`, `test_sc09_*`, `test_sc10_*`, `test_mc02_*`, `test_mc03_*`; дубль в процессе — `connectors/telegram/tests/test_telegram.py`. На fake-порту; тестовый аккаунт — `pending_owner_decision` |
| AC-M11-02 | Счётчик активных сессий fake-сервиса до и после всего набора (`/_test/service/stats`) не меняется; в зависимостях коннектора нет Telegram-клиентов (тест по `uv.lock`) |
| AC-M11-03 | Вызовы с неразрешённым чатом (нет grant, истёкший grant, чат другого tenant'а, подделанный `chat_ref`) во всех трёх tools: счётчик запросов к порту = 0, ответ `not_found` |
| AC-M11-04 | Fake-порт отвечает `flood_wait` с N ∈ {1, 7, 3600}: `rate_limited`, `retry_after_s` = N (± 1), `retryable` |
| AC-M11-05 | 1000 вызовов (смесь tools, агентов, отказов), затем поиск в логах коннектора всех текстов, peer ID и строк сессии fixtures: 0 вхождений |

## Что сознательно не делается

- Реальный Telegram-аккаунт и чаты: только fake. Прогон с тестовым аккаунтом — по решению владельца.
- HTTP-адаптер к сервису — после появления endpoint.
- Одна Telegram Connection на tenant (`shortcut`, как у `demo`).
