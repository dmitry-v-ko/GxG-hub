# M3 cycle 1 — plan (ponytail ultra)

Задача: коннектор `demo` read-only локально по [demo-connector-v1](../../docs/specs/demo-connector-v1.md), AC-M3-01…11 и DoD-1…6.
База: `origin/claude/integration` (M0, M0.1, M1, M2, M4) + свежие спеки и план отдельным коммитом без правок.

## Что оспорено (ultra) и чем кончилось

| Вопрос | Решение |
| --- | --- |
| Свой HTTP/MCP-сервер? | Нет: FastMCP `4.1.8` (линия 4.1, вышла 2026-11-10), Streamable HTTP, `stateless_http` + `json_response`. Health — `custom_route` FastMCP |
| Генерировать tools из сигнатур Python? | Нет: AC-M3-02 требует совпадения схем с файлами `connectors/demo/schemas/`. Tool — подкласс `fastmcp.tools.Tool` с `parameters`/`output_schema` из этих файлов. Таймаут tool'а — поле `Tool.timeout` FastMCP (MCC-08) |
| Файлы с сотнями заметок в fixtures? | Нет: `fixtures/boards.json` описывает tenant'ов, доски, число заметок и особые заметки; заметки генерируются `random.Random(seed)` (спека: «детерминированная генерация с фиксированным seed») |
| Настоящий поисковый индекс? | Нет: `index` — заранее посчитанный lowercase-текст заметки, `scan` — тот же поиск с ленивым lowercase и искусственной задержкой 2 с. Спеке нужны два метода и fallback, а не качество поиска |
| Своя подпись cursor'а? | stdlib `hmac` (SHA-256, ключ процесса из `secrets`), 6 строк. PyJWT есть транзитивно, но cursor не токен identity, а stdlib стоит ступенью выше |
| Свой загрузчик/резолвер bundle? | Нет: `BundleStore` кита (подпись, схема, откат, 24 ч, `check`/`allowed`). Сборка bundle в тестах и Docker smoke — `gxg-policy build` |
| Свой верификатор identity? | Нет: `StubIssuer`/`StubVerifier` кита (только `GXG_PROFILE=test`). Production-верификатора варианта C в ките пока нет (ADR-0001 «Что дальше»), поэтому вне тестового профиля коннектор отклоняет каждый вызов `unauthenticated` (fail closed). Это не AC M3, см. «Риски» |
| Отдельный реестр платформы для fixtures? | Нет: `policies/platform/registry.example.yaml` уже содержит `demo` с `tenants: [t-alpha, t-beta]`. Тест собирает временный корень политик: `tenants/` ← `fixtures/grants/t-*.yaml`, `platform/` ← пример реестра |
| Внутренний порт 8081, fake provider, SQLite | Не делаем: это этап C (M8). В M3 нет effect tools, поэтому `/readyz` не смотрит Approval Service и JWKS (MCC-09) |
| `DEMO_PROVIDER_TOKEN` | Коннектор M3 его не читает: провайдер появится в M8. AC-M3-09 проверяет, что значение из окружения процесса не попадает в ответы и логи |
| Отдельный сервер для `/_test/*` | Да, минимально: Starlette-приложение только с `POST /_test/index/fail`, поднимается на `127.0.0.1:8090` только при `GXG_PROFILE=test` и `DEMO_TEST_CONTROLS=1`; в production-образе модуля нет (`rm` в Dockerfile) |
| Отклонять лишние аргументы (`tenant_id`, `agent_id`)? | Нет: AC-M3-06 требует «не меняет результат», поэтому входные схемы не запрещают лишние поля, код читает только свои. SC-03 допускает «игнорируется» |

## Scope и лестница ponytail

| Часть | Файлы | Ступень |
| --- | --- | --- |
| Зависимости | `connectors/demo/pyproject.toml`, `uv.lock`; корневой `uv.lock` | установленные зависимости: `fastmcp==4.1.8` (тянет `mcp`), кит через `[tool.uv.sources] path`, `gxg-policy` (dev, editable) |
| Manifest | `connectors/demo/gxg-manifest.yaml` | уже есть: `schemas/examples/demo.gxg-manifest.yaml` без `demo.message.propose` (до M8, AC-M3-02) и с output-схемами |
| Схемы tools | `connectors/demo/schemas/*.json` | JSON Schema-файлы; выход поиска — `schemas/search-result.schema.json` репозитория (путь из manifest) |
| Fixtures | `connectors/demo/fixtures/boards.json`, `fixtures/grants/t-alpha.yaml`, `t-beta.yaml` | данные + stdlib `random` |
| Backend | `src/demo_connector/backend.py` | минимальный код: доски/заметки по tenant'у, `index`/`scan`, счётчики вызовов, режим отказа `index` |
| Tools | `src/demo_connector/tools.py` | минимальный код поверх кита: identity → grants (`allowed` до backend, `check` на каждом элементе, GR-16/17) → backend; ошибки — `GxgError.to_dict()` как `isError` + `structuredContent` (MCC-05) |
| Сервер | `src/demo_connector/server.py`, `__main__.py` | FastMCP + uvicorn (уже в зависимостях FastMCP); bundle с диска, перезагрузка при смене mtime; проверка профиля при старте (AC-M3-11) |
| Логи | там же | stdlib `logging` одной JSON-строкой через `redact` кита (SEC-18) |
| Test controls | `src/demo_connector/testctl.py` | Starlette (зависимость FastMCP), 1 route |
| Docker | `connectors/demo/Dockerfile`, `.dockerignore` | официальный образ `uv` + `python:3.12-slim` по digest; сборка из корня |
| Тесты | `connectors/demo/tests/` | pytest + официальный MCP SDK клиент через `httpx2.ASGITransport` (без TCP: корневой `--disable-socket`) |
| Docker smoke (AC-M3-01, AC-M3-11 на образе) | `connectors/demo/docker_smoke.sh`, job `demo-image` в CI | bash + `docker`, bundle собирает `gxg-policy build` |
| Мутации | `[tool.mutmut]` в `connectors/demo/pyproject.toml`, `scripts/ci/mutmut.sh` | mutmut 3.8.1 (уже в репозитории) на `tools.py`, `backend.py` |
| Отчёт | `reports/conformance/M3.json` | формат прошлых вех |

## DoD-6: готовые решения для своего кода

| Свой код | Рассмотрено | Почему не подошло |
| --- | --- | --- |
| Tool-класс со схемами из файлов (`tools.py`) | `@mcp.tool` FastMCP (схема из сигнатуры); `Tool.from_function(..., output_schema=…)` | входная схема генерируется из сигнатуры и не равна файлу (AC-M3-02); подкласс `Tool` — штатная точка расширения FastMCP, 20 строк |
| Валидация аргументов | валидация FastMCP | у собственного `Tool` FastMCP её не делает; `jsonschema` уже установлен китом, ошибка → наш `invalid_input` (MCC-05) |
| Подпись cursor'а | PyJWT (транзитивно), `itsdangerous` (не установлен) | stdlib `hmac` короче, cursor не токен identity |
| Поиск `index`/`scan` | SQLite FTS, whoosh | спеке нужен синтетический двухметодный backend с отказом и задержкой, а не поиск |
| Генерация заметок | Faker | новая зависимость ради 10 строк `random.Random(seed)` |
| JSON-логи | `python-json-logger`, structlog | новая зависимость ради `json.dumps(redact(record))` |
| Health endpoints | FastMCP `custom_route` | используется он; своё — только тело ответа |
| Перезагрузка bundle | watchfiles (есть транзитивно) | фоновый наблюдатель ради сравнения `st_mtime_ns` при `/readyz` и вызове tool'а |
| Docker smoke | testcontainers | новая зависимость; `docker run` + `curl` в 40 строк bash, как `bundle_smoke.sh` M2 |

## Не делаем, сознательно

- `demo.message.propose`, `internal.py`, `provider.py`, порт 8081, SQLite fake provider — этап C (M8), AC-M3-02 требует 3 tools.
- Production `IdentityVerifier` (вариант C, JWS `X-GxG-Identity`) — не в «Делаем» M3; кит его ещё не содержит.
- Сетевая изоляция «только от ContextForge» (SEC-04) — M5.
- Шаблон `templates/python-fastmcp/` — отдельная веха toolkit.

## Найденные пробелы спек (не блокируют M3, вынесены в отчёт)

1. AC-M3-10 говорит о журналах fake provider и Approval Service, которых в M3 нет. Проверяем, что propose-tool'а нет, коннектор не открывает исходящих соединений (pytest-socket) и backend видит только чтения.
2. Вне `GXG_PROFILE=test` у коннектора нет верификатора identity: каждый вызов `unauthenticated`. До M5 нужен вариант C в ките.
3. Спека указывает `fixtures/grants/t-alpha.yaml`, а `gxg-policy` читает корень вида `tenants/…`, `platform/…`. Файлы лежат как в спеке, тест и smoke собирают временный корень.
