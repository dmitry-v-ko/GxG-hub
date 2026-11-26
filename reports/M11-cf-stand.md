# M11.3 / M11.4 на живом ContextForge v1.1.1: проверка стенда

Дата: 2026-11-26. База Hub: `origin/claude/m11-hub-integration@d6f3c27` (сначала `claude/m114-hub-policy-c1d0fa@3ecd9d3`, затем перенесено на интеграционную ветку и всё прогнано заново). Ветка: `claude/m11-cf-stand`.

## Что поднималось

- Стенд M5 (корневой `compose.yaml`) + оверлей [`stand/m11cf/compose.yaml`](../stand/m11cf/compose.yaml), compose-проект `gxg-m11cf` (`gxg-stand` не тронут).
- ContextForge `ghcr.io/ibm/mcp-context-forge:v1.1.1@sha256:16c7a898…56a5` (digest из `compose.yaml`), плагины `unified_pdp` + `gxg_identity` как на стенде.
- `gxg-telegram-service:m11-4-local` (`sha256:45695859c82d…`, ветка сервиса `claude/m11-telegram-integration@8816c08`) — `tgsvc` на `10.250.0.8`, внутри `SSRF_ALLOWED_NETWORKS 10.250.0.8/30`. Своя БД `tgsvc-db`: миграции `db/` из образа, одна синтетическая чат-запись Connection `svc-test`.
- **Реального Telegram нет.** `TELEGRAM_SESSIONS=svc-test=m11cf_fake` без файла сессии, `api_hash` случайный, сети internal: сессия `error`, `/readyz` 503, живые чтения отвечают `temporarily_unavailable`. Сессия нужна сервису, чтобы опубликовать `ext.*` и `participants.collect` (без неё сервис их не регистрирует).
- Tenant: `policies/tenants/example-ext-test.yaml` как есть, только placeholder-чат (`tgc1_EXAMPLEtestChat…`, 34 символа — не проходит шаблон `chat_ref`) заменён на `chat_ref`, выданный `ChatRefs` сервиса. Bundle подписан ключом стенда.
- Агенты `reader`, `collector` команды `t-ext-test` (токен команды, SEC-02). Gateway `telegram-t-ext-test` → `http://tgsvc:8080/mcp`. Virtual servers и правила `unified_pdp` — из `gateway/contextforge/ext_roles.py` по файлу tenant'а.

Воспроизведение: `KEEP=1 stand/m11cf/run.sh` (init, build, up, bootstrap, `stand/tests`, `stand/m11cf/checks.py all`). Доказательства (без токенов) пишутся в `stand/secrets/m11cf/evidence.json`.

## Вердикты

| # | Проверка | Вердикт |
| --- | --- | --- |
| 1 | ContextForge регистрирует `ext.search/describe/call` и `participants.collect` | **passed** |
| 2 | Общая схема `ext.call` принимается, валидный вызов без `-32603` | **passed** |
| 3 | MC-05 на стенде | **passed с оговорками** (часть «обновление ContextForge» не проверяется; 1 нестабильный тест) |
| 4 | Лимит запросов и `TOOL_TIMEOUT` действуют на `ext.call` | **passed** (механизм); **нужно решение** по значениям |
| 5 | Два virtual server на агентов, pii скрыт без гранта | **passed** (с оговоркой про `/rpc`) |
| 6 | ADR-0007: `hub.execute` регистрируется как обычный MCP-tool и вызывается | **passed** для ContextForge; **конфликт с политикой** EFF-02 — нужно решение |

### 1. Регистрация — passed

`POST /v1/gateways {"name":"telegram-t-ext-test","url":"http://tgsvc:8080/mcp","transport":"STREAMABLEHTTP","teamId":…,"visibility":"team"}` → 200. Tools gateway (`GET /v1/tools`):

```text
telegram.ext.call              -> telegram-t-ext-test-telegram-ext-call
telegram.ext.describe          -> telegram-t-ext-test-telegram-ext-describe
telegram.ext.search            -> telegram-t-ext-test-telegram-ext-search
telegram.participants.collect  -> telegram-t-ext-test-telegram-participants-collect
(+ messages.search, thread.read, messages.collect, message.get)
```

Имена совпали с правилом `<gateway>-<tool с точками как дефисами>`, которое `ext_roles.py` предполагал без проверки. `inputSchema`/`outputSchema` в `tools/list` совпадают с файлами `schemas/ext-*.json` сервиса.

### 2. Общая схема `ext.call` — passed

`tools/list` отдаёт схему как есть: `{name: string pattern …, maxLength 100; arguments: object, default {}}`, `required: [name]`. Вызовы агента `reader` по трём путям (`/rpc`, virtual server MCP, virtual server SSE):

| Аргументы | Ответ (все три пути) |
| --- | --- |
| `{"name":"chats.get_info","arguments":{"chat_ref":…}}` | `isError`, `structuredContent.code = temporarily_unavailable`, `retryable: true` (сессия не `ok`) — ответ сервиса, не шлюза |
| `{"name":"chats.history","arguments":{"chat_ref":…,"x":{"y":[1,null]}}}` | то же: произвольный вложенный объект проходит |
| `{"name":"chats.get_info"}` (без `arguments`) | `invalid_input`, `details.path = /chat_ref` (сервис) |
| `{"name":"no.such_entry","arguments":{}}` | `not_found` (сервис) |

Ни одного `-32603` на валидных вызовах. Аудит сервиса на такой вызов: `{"tool":"telegram.ext.call","tenant":"t-ext-test","principal":"agent:t-ext-test:collector","service_error":"unavailable","outcome":"temporarily_unavailable","ext_entry":"chats.get_info","ext_entry_version":"1.0.0","ext_group":"ext:read:info",…}` — имя записи есть (MC-10), аргументов и peer ID нет.

Попутно (известное ограничение M5-ESCALATION №2, подтверждено для `ext.call`): если сам `name` не проходит схему, шлюз отвечает сам и по-разному: `/rpc` — `{"error":{"code":-32603,"message":"Internal error"}}`; virtual server MCP — `isError` с текстом `Invalid arguments for tool …`; SSE — пустой `result: {}`. До сервиса вызов не доходит. Поэтому общая схема и должна быть минимальной — что и сделано.

### 3. MC-05 — passed с оговорками

MC-05: «обновление ContextForge и FastMCP, virtual servers, алиасы tools → все SC и EX проходят снова». На стенде с зарегистрированным сервисом, третьей командой, двумя новыми virtual server и правилами `ext_roles` прогнан весь `stand/tests` (SC-01…SC-10, MC-01…MC-04, EX-01, M11; алиас `alpha_boards` остаётся):

```text
uv run pytest stand/tests ...   ->  1 failed, 54 passed in 807 s
  test_sc 20/20, test_mc 10/10, test_ex 4/4, test_telegram 20/21
FAILED test_telegram.py::test_mc03_timeout_after_the_request_was_sent
  httpx2.RemoteProtocolError: Server disconnected without sending a response.
повтор этого теста отдельно: 1 passed (17.5 s), ещё раз: 1 passed (17.4 s)
```

Падение — обрыв соединения ContextForge на один запрос, воспроизвести не удалось; к `ext.*` тест не относится (Hub-коннектор `telegram`, блокировка таблицы архива). Считаю нестабильным, не исправлял.

Не проверено: сама часть «обновление» — новее v1.1.1 версии на проверку нет (вывод M10, `reports/conformance/M10-mc05.md`); перепроверить список релизов отсюда не удалось (GitHub API для `IBM/mcp-context-forge` в сессии закрыт, листинг тегов GHCR пагинируется). Прогон «после рестарта / после пересоздания» (как в `stand/run.sh`) не делался — только свежий.

### 4. Лимит и `TOOL_TIMEOUT` на `ext.call` — passed (механизм), значения — на решение

ContextForge временно перезапущен с `TOOL_TIMEOUT=5`, `RATE_LIMIT_MEDIUM_RPM=6`, `RATE_LIMIT_MEDIUM_BURST=0`, `RATE_LIMIT_LOCKOUT_ENABLED=false`; после проверки — значения стенда (`25`, `3000`) возвращены.

**Лимит.** 8 подряд `tools/call ext.call` на virtual server одного агента:

```text
200 200 200 200 200 429 429 429
HTTP 429, Retry-After: 60, X-RateLimit-Limit: 6
{"error":"Rate limit exceeded","message":"Maximum 6 requests per minute for MEDIUM tier endpoints.","limit":6,"reset_in_seconds":60}
```

Из исходников образа (`mcpgateway/middleware/rate_limit_middleware.py`) и вживую:
- лимит не на tool, а на **класс пути** × (IP, пользователь, team): все записи `ext.call` и все остальные tools делят один бюджет — как и предполагал дизайн;
- ответ — HTTP 429 шлюза, а не MCC-05 `rate_limited` в `structuredContent`; `Retry-After` в заголовке;
- **`/rpc` не входит в MEDIUM** (`^/(mcp|tools|prompts|resources|servers|gateways|llmchat)`): попадает в LOW, `X-RateLimit-Limit: 5000` (на интеграционной ветке `RATE_LIMIT_LOW_RPM=5000`). Агент обходит MEDIUM-лимит, вызывая `ext.call` через `/rpc`. В Railway `GXG_TS_SERVE` публикует на 443 только `/servers` и `/mcp`, но на 8443 — весь шлюз, включая `/rpc`.

**Таймаут.** `docker pause tgsvc`, затем `ext.call`: ответ через 6.3 с (virtual server) и 5.1 с (`/rpc`) — `{"result":{"content":[{"text":"MCP server error: "}],"isError":true}}`, без кода (известное ограничение M5). `participants.collect` при паузе — так же, 5.1 с.

**Пер-tool таймаута для MCP-tools в v1.1.1 нет.** `PUT /v1/tools/<ext.call> {"timeout_ms":45000}` → 200, но `GET` показывает `timeoutMs: null`: поле только для REST-tools (`schemas.py`: `timeout_ms` «only allowed for integration_type 'REST'»; `tool_service.py`: `effective_timeout = timeout_ms/1000 if timeout_ms else settings.tool_timeout`).

**Расхождение значений (нужно решение, не правил).** Один `TOOL_TIMEOUT` на все tools, на стенде и в `deploy/railway/services.yaml` — 25 с. Сервис: `ext.call` `timeout_s: 30`, запись `users.profile_photo_get` `timeout_s: 40`, `participants.collect` `timeout_s: 120`. Правило дизайна (`reports/design/m11-3-hub-ext-grants.md`, 3.4): «таймаут `ext.call` в ContextForge ≥ максимума по записям + запас». Сейчас ContextForge обрывает `ext.call` на 25 с без кода, до таймаута сервиса (30 с) и записи (40 с); `participants.collect` — на 25 из 120 с. Варианты: поднять общий `TOOL_TIMEOUT` (≥ 125 с, затрагивает все tools и тесты MC-03, которые опираются на «коннектор 20 с < шлюз 25 с»), или сократить таймауты сервиса ниже 25 с (`participants.collect` — короткими страницами по курсору). Это решение владельца/спеки, поэтому значения не менял. Замечание для сервиса: `timeout_s: 40` у `users.profile_photo_get` недостижим — `ext.call` сам обрывает вызов на 30 с.

### 5. Virtual servers по агентам — passed

`tools/list` на своём virtual server:

```text
reader    vs-tg-t-ext-test-ext         : ext.call, ext.describe, ext.search, messages.search, thread.read
collector vs-tg-t-ext-test-ext-people  : ext.call, ext.describe, ext.search, messages.collect, messages.search,
                                         participants.collect, thread.read
```

(имена ContextForge с префиксом `telegram-t-ext-test-telegram-`), ровно как `ext_roles.build` по файлу tenant'а. Два virtual server одной команды работают независимо.

pii: `ext.search` (сервис) — `reader` видит 7 записей (`chats.get_info/history/members/pinned`, `messages.get`, `messages.reactions`, `users.get_info`), `collector` с `ext:read:pii` — 9 (плюс `users.profile_photos`, `users.profile_photo_get`). `reader` → `ext.call users.profile_photos` → `permission_denied` («this entry needs ext:read:pii on this chat»); `collector` проходит проверку pii (дальше `not_found` на синтетический `user_ref`). `reader` → `participants.collect` → `-32602 Plugin Violation: … Native RBAC: no matching allow rule (fail closed)` (`PDP_DENY`).

Оговорка: virtual server ограничивает только список. `tools/list` на `/rpc` показывает агенту **все tools команды** (у `reader` там есть `participants.collect`, `messages.collect`, `message.get`); вызвать их не даёт `unified_pdp`. Значит «агент видит только свои tools» верно для virtual server, а не для `/rpc`. Если видимость на `/rpc` важна — отдельная команда на набор tools или закрыть `/rpc` для агентов (решение). Ещё: `ext_roles.py` не знает `telegram.message.get` (сервис его публикует), ни одному агенту он не выдан — проверить, так ли задумано.

### 6. ADR-0007: `hub.execute` — passed для ContextForge, конфликт с EFF-02

Минимальный MCP-сервер ([`stand/m11cf/hubexec_server.py`](../stand/m11cf/hubexec_server.py), FastMCP 4.1.8, один tool `hub.execute`, ничего не исполняет) на `10.250.0.11`, gateway `hubexec-t-ext-test` в команде `t-ext-test`, в `gxg_identity` — обычный коннектор (`aud: hub-executor`).

- Регистрация штатная: `POST /v1/gateways` → 200, tool `hubexec-t-ext-test-hub-execute` (`originalName: hub.execute`, `integrationType: MCP`, `enabled: true`), схема `{script: string}`. Доработки ContextForge не нужно.
- Вызов агентом `collector` с правилами стенда + явный allow на этот tool:
  `{"error":{"code":-32602,"message":"Plugin Violation: [all_must_allow] EFF-02, security-v1 step 2: execute and reconcile are never callable through ContextForge","data":{…,"plugin_error_code":"PDP_DENY"}}}` — правило `deny:agents-never-execute` (`tools.invoke.*execute*`) срабатывает на имя `hub-execute`.
- Тот же вызов, правила без `deny:agents-never-execute` (временно, только для проверки): `{"result":{"structuredContent":{"ran":false,"script_len":8},"isError":false}}`.

Итог: условие 2 ADR-0007 по ContextForge выполнимо, но имя `hub.execute` попадает под запрет EFF-02 «агенту никогда не вызывать `*execute*`». Нужно решение: другое имя фасада (например `hub.script.run`) или исключение в правиле (ослабление SEC/EFF-02 — хуже). Правила стенда не менялись; после проверки gateway удалён, контейнер остановлен, правила и `plugins.yaml` восстановлены.

## Что изменено в Hub (ветка `claude/m11-cf-stand`)

- `stand/tests/standlib.py`: имена docker-сетей из `COMPOSE_PROJECT_NAME` (по умолчанию `gxg-stand`, как было), чтобы набор стенда шёл под другим compose-проектом.
- `stand/m11cf/` (новое): `compose.yaml` (оверлей: сервис, его БД, правила PDP, ручки лимита/таймаута для проверки 4), `setup.py` (`init`: секреты, bundle, SQL, правила из `ext_roles`; `bootstrap`: команда, агенты, gateway, virtual servers, `plugins.yaml`), `checks.py` (проверки 1, 2, 4, 5, 6), `hubexec_server.py`, `run.sh`.
- Исправлений кода Hub или конфигурации шлюза не понадобилось: проверки 1, 2, 5 прошли на конфигурации ветки как есть.

## Решения, которые нужны (не применялись)

1. `TOOL_TIMEOUT` 25 с против `ext.call` 30 с / записи 40 с / `participants.collect` 120 с; пер-tool таймаута для MCP-tools в v1.1.1 нет.
2. Лимит ContextForge — на класс пути, `/rpc` в LOW (5000/мин): MEDIUM-лимит обходится через `/rpc`; ответ — HTTP 429, а не MCC-05 `rate_limited`.
3. Имя фасада `hub.execute` против `deny:agents-never-execute`.
4. Видимость tools на `/rpc` — все tools команды, не только набор агента.

## Не проверено

- Реальные данные Telegram, flood-wait, `max_results`/`max_bytes` записей (нет сессии) — MC-06…MC-13 на сервисе остаются за прогоном сервиса.
- Часть MC-05 «обновление ContextForge/FastMCP»; прогон после рестарта и пересоздания контейнеров.
- Условия `unified_pdp` по аргументам `ext.call` (не использовались).
- Образы `gxg-demo:stand` и `gxg-telegram:stand` пересобраны из этой ветки под теми же тегами (общие для всех проектов на этом Docker).
