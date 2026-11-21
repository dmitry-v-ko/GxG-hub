# M6, цикл 1 — план (ponytail ultra)

База: `origin/claude/m5-readonly-poc` (стенд ContextForge M5). Ветка `claude/m6-kb`.

## Блокер

Read-доступ к `dmitry-v-ko/gxg_agents_v2` отклонён (`reports/M6-ESCALATION.md`). Gap-аудит (AC-M6-01), пустой diff KB (AC-M6-03) и отказ записи служебным ключом **реальной** KB (AC-M6-04) в этом цикле не выполнить. Делаем каркас wrapper'а и проверку изоляции через ContextForge (AC-M6-02) на fake-KB.

## Что вычеркнуто (ultra: потребность не оправдывает)

| Часть | Почему не делаем |
| --- | --- |
| MCP/HTTP-клиент к KB MCP | Протокол KB неизвестен до аудита: клиент по догадке будет переписан. fake-KB живёт в процессе wrapper'а за портом `KbBackend`, как `demo.Backend`. После аудита меняется только этот модуль |
| fake-KB со служебными ключами read/write и тест «запись read-ключом отклонена» | Тест проверял бы наш fake, а не KB: AC-M6-04 этим не выполняется. План теста — в аудите, статус `blocked_no_access` |
| Отдельный контейнер fake-KB | Нужен только с сетевым клиентом (см. выше) |
| `kb.documents.create/update` | Write — не в этой вехе (задание) |
| Отдельная операция KB для `kb.context.get` | Неизвестно, есть ли она в KB. `kb.context.get` собирается в wrapper'е из того же поиска с теми же проверками прав, `pending_owner_decision` |
| Вынос общего runner'а tools (`DemoTool.run`, `BundleFiles`, `parse_keys`) в `gxg-connector-kit` | Требует fastmcp в зависимостях kit (его используют approval и policy) и правки M3/M5-кода. Это второй коннектор; правило трёх — выносить с генератором toolkit. `shortcut` в коде |

## Места изменения

| Где | Что | Ступень лестницы |
| --- | --- | --- |
| `connectors/kb/gxg-manifest.yaml`, `schemas/*.json` | 3 read tools, схемы входа/выхода; поиск — общий `schemas/search-result.schema.json` | уже есть в репозитории (формат demo) |
| `connectors/kb/src/kb_connector/backend.py`, `fixtures/kb.json` | fake-KB: коллекции и документы двух tenant'ов, поиск только по переданным коллекциям, счётчик вызовов | минимальный код (копия формы demo) |
| `connectors/kb/src/kb_connector/tools.py` | identity → схема → bundle → `allowed()` до запроса, `check()` на каждый элемент, `not_found` для чужого/нет grant'а, курсор HMAC, аудит | уже есть в репозитории (`demo/tools.py`), копия с сокращением |
| `connectors/kb/src/kb_connector/server.py`, `__main__.py` | FastMCP + `/healthz`, `/readyz` | уже есть (demo) |
| `connectors/kb/Dockerfile`, `pyproject.toml`, `uv.lock`, `README.md` | образ, lockfile | уже есть (demo) |
| `connectors/kb/fixtures/grants/t-*.yaml` | reader t-alpha: `sales`; истёкший grant на `hr`; reader t-beta: свой `sales` | уже есть (формат GR) |
| `connectors/kb/tests/test_kb.py` | юнит-тесты изоляции, SC-05/SC-10 на KB | pytest |
| `pyproject.toml` (корень) | kb в testpaths, sources, mypy path, isort | одна строка на место |
| `stand/init.py` | bundle из grants всех `connectors/*/fixtures/grants` (слияние по tenant'у) + manifest kb | минимальный код |
| `compose.yaml` | сервис `kb` в сети `connectors` (10.250.0.12), SSRF allow `10.250.0.8/29` | конфиг |
| `stand/bootstrap.py` | gateway `kb-<tenant>`, его tools в virtual server, `connectors` плагина: gateway → `kb` | цикл по `CONNECTORS` |
| `stand/unified_pdp.rules.json` | allow kb tools reader'ам | конфиг |
| `stand/tests/test_kb.py` | AC-M6-02 через ContextForge: только `sales`, чужой документ `not_found` побайтно, чужой tenant, истёкший grant, `nobody` → PDP deny, ноль вызовов KB при отказе | pytest на стенде |
| `stand/report.py`, `stand/run.sh` | M6 отчёт из того же прогона | минимальная правка |
| `docs/audits/kb-gap-audit.md` | шаблон аудита: таблица на каждый tool, что неизвестно, допущения wrapper'а, план AC-M6-04 | — |
| `connectors/README.md` | статус KB | одна строка |

## DoD-6: готовые решения для своего кода

| Компонент | Рассмотрено | Решение |
| --- | --- | --- |
| Сервер tools | FastMCP (закреплён в demo) | используем FastMCP |
| Identity, grants, bundle, ошибки, редакция | `gxg-connector-kit` | используем kit |
| Runner tool'а (порядок проверок, аудит) | `demo_connector.tools.DemoTool`; вынос в kit | копия (см. «вычеркнуто»), `shortcut` |
| Курсор пагинации | stdlib `hmac` | stdlib, как в demo |
| fake-KB | SQLite/Postgres, Supabase CLI | без кода KB схема неизвестна: in-memory fixtures, как demo |
| Слияние grants для стенда | `gxg-policy` (собирает по файлу на tenant) | 10 строк в `init.py`: инструмента для слияния нет |

## AC в этом цикле

| AC | План |
| --- | --- |
| AC-M6-01 | `blocked_no_access`: шаблон аудита с перечнем неизвестного |
| AC-M6-02 | Через ContextForge на fake-KB: `stand/tests/test_kb.py` |
| AC-M6-03 | `blocked_no_access`: репозиторий KB не клонирован, изменений нет по построению |
| AC-M6-04 | `blocked_no_access` |
