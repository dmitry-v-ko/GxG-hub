# ADR-0002: Выбор MCP-шлюза: ContextForge, ToolHive или Docker MCP Gateway

**Статус:** Proposed — решение принимает владелец. Спайк M4.1, 2026-11-21.
**Связано:** [ADR-0001](0001-identity-propagation.md) (вариант C на ContextForge, Accepted), [SEC-03, SEC-04](../specs/security-v1.md#identity), [Roadmap → GO/NO-GO](../specs/roadmap-v1.md#go--no-go). Стенды: [`spikes/m4.1-gateways/`](../../spikes/m4.1-gateways/README.md).

## Рекомендация

**Оставить ContextForge с вариантом C.** ToolHive держать запасным кандидатом, Docker MCP Gateway отклонить.

- **ContextForge** — единственный из трёх, где identity доходит до коннектора, проходит все атаки M4 и где fail closed проверен на стенде. Переход ничего не стоит: работа M4 уже сделана.
- **ToolHive vMCP** изолирует tenant'ов лучше ContextForge: Cedar с deny by default, фильтрация `tools/list` по политике, скрытый tool нельзя вызвать по имени. Всё это проверено на стенде. Но multi-tenant модели у него нет: tenant — это claim IdP плюс политики, которые мы пишем сами. Своего механизма identity для коннектора у ToolHive тоже нет. Ближе всего `token_exchange` (RFC 8693): он даёт подписанный IdP токен, но повтор, подмена аргументов и другой tool с ним проходят (проверено). Вариант C на vMCP — только наш Go-код, собранный в свой бинарник vMCP. Стоимость перехода — ~8–12 дней (ниже), выигрыш — Cedar вместо `unified_pdp` ContextForge.
- **Docker MCP Gateway** не поддерживает ни multi-tenant, ни identity вызывающего: один общий bearer-токен на инстанс, один `mcp.Server` на все сессии. Политики вне Docker Desktop разрешают всё. Перехватчики не умеют менять запрос. Все релизы помечены pre-release. Вариант C без форка невозможен.

**Когда пересмотреть.** Гибрид (ToolHive вместо ContextForge при сохранении кита и формата JWS) имеет смысл, если в M5 ContextForge не выполнит условие 4 GO/NO-GO: `unified_pdp`, deny by default на всех путях. Тогда часть работы уже сделана: `IdentityVerifier` и формат JWS не меняются, меняется только сторона подписи.

## Закреплённые версии

| Кандидат | Образ | Источник |
| --- | --- | --- |
| ToolHive vMCP | `ghcr.io/stacklok/toolhive/vmcp:v0.67.0@sha256:6416a07efaec881c1eee208f1818dcb997374ee3270a31487a47907d877c2be1` | тег [`v0.67.0`](https://github.com/stacklok/toolhive/tree/884ee1fbc4e6b8059797bc0a523b3908c2955ce0) = `884ee1fb`, релиз 2026-11-20, отмечен Latest |
| ToolHive thv / proxyrunner (не запускались) | `ghcr.io/stacklok/toolhive:v0.67.0@sha256:32579286003a97e67ab5fe324757d4945950e4aa25d7e84b3a96c8a9e16ef292`, `ghcr.io/stacklok/toolhive/proxyrunner:v0.67.0@sha256:7e6598e490947e0cb0f0c68d7a869165af4b93aa96ec56304bf769676ca51d1c` | тот же тег |
| Docker MCP Gateway | `docker/mcp-gateway:v0.44.3@sha256:bae58d801fd4b73f6ad91e609b8ce5ec8868713825f33a2bb20a98f956d0bd46` | тег [`v0.44.3`](https://github.com/docker/mcp-gateway/tree/7e0f34277c91855ad861eeff536b4fdc77420593) = `7e0f3427`, 2026-10-26, **pre-release** (как и все релизы на страницах 1–2). Тег `latest` в Docker Hub — образ от 2026-07-23, старше `v0.44.3`, не используется |
| Апгрейд-репетиция | `vmcp:v0.66.0@sha256:4f5ac6d239c6e7de6d6b141cb9e406bd38c3144dca8f618011fe491c7777f352`, `mcp-gateway:v0.44.2@sha256:bc104370585ec26e2e7243ace354caa8ba7b05d9f8cea01b177016b0598dbcb1` | предыдущие теги |
| Только стенд | mock IdP `ghcr.io/navikt/mock-oauth2-server:3.0.1@sha256:4f3660e5f3586b81fe2e0d42d0c9fcd3c8c64268db0d3ade261dadd70d27020b`; echo на образе ContextForge из M4 | — |

Ссылки на код ниже — на эти коммиты: `TH/` = `stacklok/toolhive@884ee1fb`, `DG/` = `docker/mcp-gateway@7e0f3427`.

## Security advisories на 2026-11-21

- **ToolHive.** [GitHub Security Advisories](https://github.com/stacklok/toolhive/security/advisories): 60 GHSA, опубликованы с 2025-05-12 по 2026-11-06, то есть все до релиза `v0.67.0` (2026-11-20). У 10 самых свежих patched-версия ≤ `0.62.0`: [GHSA-9r8f-m39c-5rv5](https://github.com/stacklok/toolhive/security/advisories/GHSA-9r8f-m39c-5rv5), [GHSA-gxq5-f6xj-f7w2](https://github.com/stacklok/toolhive/security/advisories/GHSA-gxq5-f6xj-f7w2), [GHSA-4rg8-mr2x-646g](https://github.com/stacklok/toolhive/security/advisories/GHSA-4rg8-mr2x-646g) (0.62.0), [GHSA-x93c-fqj5-f398](https://github.com/stacklok/toolhive/security/advisories/GHSA-x93c-fqj5-f398) (0.57.0), [GHSA-2x65-h9w3-h7hc](https://github.com/stacklok/toolhive/security/advisories/GHSA-2x65-h9w3-h7hc) (0.55.2), [GHSA-m5gh-q587-rjvv](https://github.com/stacklok/toolhive/security/advisories/GHSA-m5gh-q587-rjvv), [GHSA-2f8h-v47r-j3c3](https://github.com/stacklok/toolhive/security/advisories/GHSA-2f8h-v47r-j3c3), [GHSA-r928-c58c-5ppq](https://github.com/stacklok/toolhive/security/advisories/GHSA-r928-c58c-5ppq) (0.55.0), [GHSA-pg92-mx73-6f5w](https://github.com/stacklok/toolhive/security/advisories/GHSA-pg92-mx73-6f5w) (0.54.0), [GHSA-756x-wh7m-3xcv](https://github.com/stacklok/toolhive/security/advisories/GHSA-756x-wh7m-3xcv) (0.53.0). У остальных 50 (страницы 2–6) списки patched-версий не показывают, и по одному мы их не открывали. [OSV](https://osv.dev/) для Go `github.com/stacklok/toolhive@v0.67.0`: 0 уязвимостей; всего в OSV 3 записи, исправлены в ≤ 0.31.0 (GHSA-pph6, GHSA-pr64, GHSA-qg2g). Важно для нас: большая часть GHSA — обходы авторизации и фильтров (например, [GHSA-hxx3-f8cg-r974](https://github.com/stacklok/toolhive/security/advisories/GHSA-hxx3-f8cg-r974), [GHSA-hqg7-qjgg-q779](https://github.com/stacklok/toolhive/security/advisories/GHSA-hqg7-qjgg-q779), [GHSA-rxgh-62mp-jq63](https://github.com/stacklok/toolhive/security/advisories/GHSA-rxgh-62mp-jq63), [GHSA-wj8g-gmv2-mpmv](https://github.com/stacklok/toolhive/security/advisories/GHSA-wj8g-gmv2-mpmv) — fail-open webhooks). Их быстро чинят, но поверхность атаки большая.
- **Docker MCP Gateway.** [GitHub Security Advisories](https://github.com/docker/mcp-gateway/security/advisories): 10 GHSA, у всех patched ≤ `0.44.1`, `v0.44.3` не затронута: [GHSA-qg99-jrvf-j9fh](https://github.com/docker/mcp-gateway/security/advisories/GHSA-qg99-jrvf-j9fh) и [GHSA-jfp6-4vv7-6m6c](https://github.com/docker/mcp-gateway/security/advisories/GHSA-jfp6-4vv7-6m6c) (0.44.1); [GHSA-g879-4j4f-6vj7](https://github.com/docker/mcp-gateway/security/advisories/GHSA-g879-4j4f-6vj7) (critical, неаутентифицированный доступ к tools в container mode), [GHSA-mqq5-qh4g-2g8g](https://github.com/docker/mcp-gateway/security/advisories/GHSA-mqq5-qh4g-2g8g) (critical, bind mount через dynamic tools), [GHSA-6m8f-w97w-99h7](https://github.com/docker/mcp-gateway/security/advisories/GHSA-6m8f-w97w-99h7), [GHSA-m5m2-mrxf-7j7q](https://github.com/docker/mcp-gateway/security/advisories/GHSA-m5m2-mrxf-7j7q), [GHSA-6pq5-p7fc-7xhq](https://github.com/docker/mcp-gateway/security/advisories/GHSA-6pq5-p7fc-7xhq), [GHSA-625j-rw87-4ghr](https://github.com/docker/mcp-gateway/security/advisories/GHSA-625j-rw87-4ghr) (0.43.1); [GHSA-r2xf-7jw5-pjg6](https://github.com/docker/mcp-gateway/security/advisories/GHSA-r2xf-7jw5-pjg6) (0.42.2); [GHSA-46gc-mwh4-cc5r](https://github.com/docker/mcp-gateway/security/advisories/GHSA-46gc-mwh4-cc5r) (0.28.0). OSV для `github.com/docker/mcp-gateway@v0.44.3`: 0.
- Оговорка как в ADR-0001: диапазоны указаны мейнтейнерами, по коду мы их не сверяли.

## Стенд и атаки

Оба кандидата прогнаны тем же набором, что M4: `spikes/m4.1-gateways/attacks.py` импортирует `spike.py` (HTTP, MCP-сессия, `tools/call` с аргументами `{"text": "привет", "lang": "ru"}`, поддельный JWS) и `check_verdicts.EXPECTED` из M4, echo-MCP с верификатором варианта C — тот же `echo/` M4. Каждый `tools/call`, дошедший до echo, получает вердикт верификатора при приёме.

- **ToolHive:** один vMCP на оба tenant'а, static backends `alpha` → `/mcp/t-alpha`, `beta` → `/mcp/t-beta`, `hidden` → `/mcp/t-hidden` (tool скрыт фильтром агрегации). Incoming — OIDC от mock IdP, claim `tenant_id`; 4 агента (client_credentials), platform admin = токен с `roles: [admin]` без `tenant_id`. Cedar: `call_tool` на `alpha_echo` только при `principal.claim_tenant_id == "t-alpha"`, на `beta_echo` и `hidden_echo` — при `t-beta` ([`vmcp.yaml`](../../spikes/m4.1-gateways/toolhive/vmcp.yaml)). Варианты: `token_exchange` к обоим коннекторам; STS недоступен; невалидная политика; апгрейд с `v0.66.0`.
- **Docker MCP Gateway:** identity вызывающего у шлюза нет, поэтому tenant = инстанс: `gw-alpha` (echo-alpha + echo-hidden, `--tools=echo-alpha:echo`), `gw-beta`, и `gw-shared` — один инстанс на двоих, чтобы показать, что он даёт двум tenant'ам. Транспорт `streaming`, токен `MCP_GATEWAY_AUTH_TOKEN` на инстанс (оба агента tenant'а держат один и тот же), `--static`: без него шлюз в контейнере ходит в Docker API и падает при старте (`guessing network: Cannot connect to the Docker daemon`). Admin-роли у шлюза нет.

### Результаты атак M4

| Атака | ContextForge + C (M4) | ToolHive vMCP | Docker MCP Gateway |
| --- | --- | --- | --- |
| Вызов агентом своего tool | принят коннектором, identity = агент | дошёл; без identity (`missing X-GxG-Identity`); с `token_exchange` — Bearer JWT IdP с `sub`, `tenant_id` | дошёл; без identity, оба агента tenant'а неразличимы |
| Поддельные `X-Forwarded-User-*`, `X-Tenant-Id`, `X-GxG-Identity` | до коннектора не дошли | не дошли (заголовки клиента не передаются без `passthroughHeaders`) | не дошли |
| Поддельный `_meta.tenant_id`/`_meta.user`, аргументы `tenant_id`/`principal` | прочие ключи `_meta` и аргументы проходят; identity только из JWS | **проходят к коннектору как есть** (`forged_meta_reached: true`) | **проходят как есть** |
| Без токена | отказ | 401 `authorization header required` | 401 |
| Platform admin | отказ плагина `GXG_IDENTITY_DENIED` | 403 `call denied by authorization policy` (нет разрешающей политики) | n/a: ролей нет, любой держатель токена инстанса равен любому другому |
| Чужой tenant | `Tool not found` / 403 на всех путях | 403 `call denied by authorization policy` | свой инстанс: 401; **общий инстанс: вызов t-beta агентом t-alpha дошёл** |
| Скрытый tool по имени | не проверялось в M4 | `tool "hidden_echo" not found`, хотя Cedar его разрешает | `unknown tool "hidden__echo"` |
| Прямо на коннектор: без identity, повтор, подмена аргументов, другой коннектор, другой tool, через 65 с | отклонены с ожидаемой причиной | `missing X-GxG-Identity` (своего механизма нет); с `token_exchange` **перехваченный токен проходит проверку IdP во всех повторах**, включая подмену аргументов, другой tool и через 65 с | `missing X-GxG-Identity` — шлюз ничего не передаёт |
| Подделка JWS чужим ключом | `InvalidSignatureError` | n/a: JWS нет | n/a: JWS нет |

Доказательства: `toolhive/evidence/{default,token-exchange}/summary.json`, `observations.jsonl`, `echo-received.jsonl`, `token-exchange/verify_tx.json`; `docker-mcp-gateway/evidence/default/…`. Ожидаемые причины M4 (`check_verdicts.EXPECTED`) на кандидатах не получены ни разу. Это ожидаемо: нашего адаптера на них нет, а свой проверяемой identity к коннектору ни один из двух не даёт.

## Матрица

Статусы: `passed`, `failed`, `n/a`, `not_run` (не проверено вживую, с причиной). Где статус выведен из кода, это сказано.

| # | Пункт | ContextForge v1.1.1 (ADR-0001, M4.json) | ToolHive vMCP v0.67.0 | Docker MCP Gateway v0.44.3 |
| --- | --- | --- | --- | --- |
| 1 | Изоляция tenant'ов | **passed**: team, cross-team отказ на всех путях | **passed** (своей модели нет: tenant = claim + Cedar) | **failed**: нет multi-tenant; только инстанс на tenant |
| 2 | Проверяемая identity к коннектору | встроенная A — **failed**; C — **passed** | встроенной нет; `token_exchange` — **failed** по M4; C — только свой Go-код в vMCP (**not_run**) | **failed**: identity вызывающего нет; C без форка невозможна |
| 3 | Fail closed | **passed** при `PLUGINS_FAIL_ON_PLUGIN_ERROR=true` | **passed**: плохая политика — не стартует; STS недоступен — вызов не уходит | **n/a**: механизма нет; перехватчики: ошибка → отказ, HTTP 500 без тела → пропуск (по коду) |
| 4 | Скрытые tools; deny by default | частично: скрытые не проверялись, `unified_pdp` — M5 | **passed**: скрытый не вызывается; deny by default при включённом Cedar | **failed**: `--tools` скрывает, но политики вне Desktop — allow all; `mcp-add` по умолчанию |
| 5 | Пути вызова и identity на каждом | **passed** (`/rpc`, virtual server, алиас) | **passed** для агрегированного пути с префиксом; прокси `thv run` и overrides — **not_run** | **failed**: identity нет ни на одном пути; `mcp-exec`/code-mode обходят перехватчики |
| 6 | Обновление версии | **passed** (репетиция v1.1.0 → v1.1.1) | **passed** (репетиция v0.66.0 → v0.67.0, тот же конфиг) | **passed** (репетиция v0.44.2 → v0.44.3) |
| 7 | Зрелость и безопасность | Apache-2.0; 19 GHSA + бюллетень IBM, все до v1.1.1 | Apache-2.0; релизы еженедельно, pre-1.0, с breaking changes; 60 GHSA, все до v0.67.0 | MIT; **все релизы pre-release**; 10 GHSA, все до v0.44.3; продукт для Docker Desktop |
| 8 | Эксплуатация | PostgreSQL; конфиг плагинов YAML; аудит и метрики в M4 не оценивались | нет PostgreSQL (файлы, CRD, Redis); YAML/CRD; аудит JSON (проверен); OTel/Prometheus | нет БД (файлы + SQLite); аудит только в Desktop; OTel через Docker CLI |
| 9 | Стоимость перехода | 0 (сделано в M4) | ~8–12 дней | не оценивается: нужен форк |

### 1. Изоляция tenant'ов

- **ToolHive — passed, со своей моделью tenant'ов.** Tenant, team или org в коде нет. Группа (`groupRef`) — один набор backend'ов на инстанс vMCP. Разным клиентам разное видно только через Cedar: admission фильтрует списки и отклоняет вызовы по identity (`TH/pkg/vmcp/core/admission.go`, `FilterTools`, `AllowToolCall`). На стенде один vMCP на двоих: `tools/list` агентов t-alpha → `["alpha_echo"]`, t-beta → `["beta_echo"]`, admin → `[]` ([`evidence/default/visibility.json`](../../spikes/m4.1-gateways/toolhive/evidence/default/visibility.json)). Вызов `beta_echo` агентом t-alpha → 403. Изоляция держится на политике, которую генерируем мы: ошибка в политике = утечка между tenant'ами. Политика ссылается на имя tool после префикса (`alpha_echo`), поэтому смена `prefixFormat` молча ломает её в сторону отказа, не разрешения.
- **Docker MCP Gateway — failed.** Один `mcp.Server` на все HTTP-сессии: `NewStreamableHTTPHandler(func(_ *http.Request) *mcp.Server { return g.mcpServer })` (`DG/pkg/gateway/transport.go#L59-L61`). Аутентификация — один общий токен (`DG/pkg/gateway/auth.go#L34-L47`). На общем инстансе агент t-alpha вызвал `beta__echo` и дошёл до коннектора t-beta. С инстансом на tenant изоляция есть (чужой токен → 401), но агентов внутри tenant'а различить нельзя. Кроме того, dynamic tools включены по умолчанию, и `mcp-add` меняет общий `serverNames` для всех клиентов (`DG/pkg/gateway/mcpadd.go#L81-L85`). На стенде они выключены флагом `--servers`.

### 2. Проверяемая identity к коннектору и путь к варианту C

- **ToolHive, встроенное.** Подписанного identity-заголовка или identity в `_meta` нет. Стратегии outgoing auth vMCP: `unauthenticated`, `header_injection` (статичное значение или env), `token_exchange`, `upstream_inject`, `aws_sts`, `obo` (заглушка «enterprise required», `TH/pkg/vmcp/auth/obo.go#L17-L22`), `xaa` (`TH/pkg/vmcp/auth/types/types.go#L26-L59`).
- **ToolHive, `token_exchange` вживую.** Коннектор получает `Authorization: Bearer` — JWT, подписанный IdP, с `sub`, `email`, `tenant_id`, `jti`, `exp`. Все 5 вызовов через шлюз проходят проверку по JWKS IdP. Токен кешируется на пару (пользователь, backend): `{backend}:{hash(subject_token)}:{audience}` (`TH/pkg/vmcp/cache/cache.go`). Отсюда результат по критериям ADR-0001 ([`verify_tx.json`](../../spikes/m4.1-gateways/toolhive/evidence/token-exchange/verify_tx.json)):
  - Критерий 1: `tenant_id` и `sub` агент не подделает, их подписывает IdP. Но `_meta` и аргументы агента проходят рядом.
  - Критерий 2 провален: перехваченный токен, отправленный напрямую на коннектор, проходит проверку (`replay_immediate: true`).
  - Критерий 3 провален: через 65 с токен тоже проходит (`replay_after_65s: true`). Время жизни задаёт IdP, а не шлюз (на стенде 3600 с), привязки к вызову нет.
  - Подмена аргументов и другой tool тоже проходят: токен не привязан ни к tool, ни к аргументам.
  - Повтор на другой коннектор проходит: mock IdP не ставит `aud` при exchange, хотя vMCP передаёт `audience` (`TH/pkg/vmcp/auth/strategies/tokenexchange.go#L163`). Это свойство mock IdP, а не ToolHive: с IdP, который ставит `aud`, этот повтор отклонялся бы.
- **ToolHive, вариант C.**
  - **vMCP:** только Go-код, собранный в свою сборку. Список middleware прокси — фиксированная map (`TH/pkg/runner/middleware.go#L38-L45`), неизвестный тип → `unsupported middleware type` (`TH/pkg/runner/runner.go#L411-L424`). Точка расширения vMCP — `Strategy.Authenticate(ctx, *http.Request, …)`: вызывается на каждом запросе к backend, в контексте есть identity, в теле — `tools/call`. Свою стратегию можно подставить через `auth.RegisterOBOStrategy` из `init()` внешнего пакета (`TH/pkg/vmcp/auth/obo.go#L51-L55`, «intended to be called exactly once during init() in an out-of-tree package») или встроив vMCP как библиотеку. Форк не нужен, но нужна своя сборка бинарника и образа. Стратегия считает `ahs` по телу, подписывает JWS тем же форматом, что в ADR-0001, и ставит `X-GxG-Identity`. На стенде это **not_run**: адаптер под кандидата мы не писали.
  - **Прокси `thv run`:** mutating webhook получает principal и `mcp_request` (`TH/pkg/webhook/types.go`), но патч ограничен `/mcp_request/` (`TH/pkg/webhook/mutating/patch.go#L61-L66`). Значит, JWS можно положить только в `_meta`, не в заголовок. В `pkg/vmcp` webhooks не подключены (grep: только комментарий о Kubernetes admission webhooks в `config/composite_validation.go`), то есть на агрегированном пути этого способа нет.
- **Docker MCP Gateway — failed.**
  - Identity вызывающего нет: единственное, что шлюз знает о клиенте, — `clientInfo.name` из `initialize`, его выбирает сам клиент.
  - К remote-серверу уходят статичные `remote.headers` из каталога или OAuth-токен самого шлюза. Заголовки клиента не передаются, `_meta` клиента передаётся как есть: `Meta: req.Params.Meta` (`DG/pkg/gateway/handlers.go#L52-L56`).
  - Перехватчики (`--interceptor before|after:exec|docker|http`) не меняют запрос. Непустой вывод `before` подменяет **результат** вместо вызова backend (`DG/pkg/interceptors/interceptors.go#L109-L116`). Вживую на stdio: перехватчик получил `{"Session":{},"Params":{"_meta":{…},"name":"alpha__echo","arguments":{…}},"Extra":null}`, а его вывод вернулся клиенту вместо вызова ([`evidence/interceptors/`](../../spikes/m4.1-gateways/docker-mcp-gateway/evidence/interceptors/)).
  - На транспорте `streaming` любой `before`-перехватчик ломает вызов: `marshalling request: json: unsupported type: func(mcp.CloseSSEStreamArgs)` (проверено вживую).
  - Вариант C возможен только форком (middleware, который меняет `_meta` или заголовок) плюс собственная аутентификация, иначе подписывать нечего.

### 3. Fail closed

- **ToolHive — passed.** Невалидная Cedar-политика: vMCP не стартует (`failed to parse policy 3`, [`vmcp-bad-cedar-policy.log`](../../spikes/m4.1-gateways/toolhive/evidence/fail-closed/vmcp-bad-cedar-policy.log)). STS для `token_exchange` недоступен: backend не инициализируется (`authentication failed`), tools пропадают, вызовы получают `tool not found`, до коннектора ничего не доходит ([`vmcp-sts-down.log`](../../spikes/m4.1-gateways/toolhive/evidence/fail-closed/vmcp-sts-down.log), `token-exchange-sts-down/observations.jsonl`). Ошибка стратегии варианта C пойдёт тем же путём (`Authenticate` возвращает ошибку) — это вывод по коду, вживую не проверено.
- Оговорки ToolHive: без секции `authz` vMCP разрешает всё: `if authzCfg == nil { return allowAllAdmission{}, nil }` (`TH/pkg/vmcp/core/admission.go#L85-L87`), поэтому Cedar обязателен в конфиге, а его наличие нужно проверять тестом. У webhook есть `failure_policy: ignore`, то есть fail open (`TH/pkg/webhook/types.go#L53-L59`).
- **Docker MCP Gateway — n/a.** Механизма identity нет, ломаться нечему. Перехватчики: non-zero exit или невалидный JSON → ошибка вызова; HTTP-перехватчик со статусом 500 и пустым телом пропускает вызов (fail open, по коду `interceptors.go`, вживую не проверено).

### 4. Скрытые tools и deny by default

- **ToolHive — passed.** `CallTool` сначала авторизует вызов, затем отклоняет имена, которых нет в объявленном наборе (`TH/pkg/vmcp/core/core_calls.go#L62-L66`). Вживую: `hidden_echo` разрешён Cedar для t-beta, но скрыт фильтром → `tool "hidden_echo" not found`. С политиками Cedar работает deny by default: admin без политики → 403. Оговорка: composite tools могут вызывать скрытые tools — мы их не используем.
- **Docker MCP Gateway — failed.** `--tools` не регистрирует отфильтрованные tools, вызов по имени → `unknown tool "hidden__echo"`. Но политика вне Docker Desktop — `NoopClient`, который всегда разрешает (`DG/pkg/policy/policy.go#L81-L87`, `#L104-L112`). На вход политики caller identity не передаётся вовсе. RBAC нет.

### 5. Пути вызова

- **ToolHive.** Агрегированный путь с префиксом `{workload}_` проверен вживую с identity (`token_exchange`) и Cedar. Overrides (переименование tool) и прокси одного сервера (`thv run` / proxyrunner) — **not_run**: `thv run` управляет контейнерами через docker socket и в этом стенде не поднимался. По коду прокси одного сервера — reverse proxy, заголовки клиента доходят до backend, `strip-auth` убирает только `Authorization`, `Cookie` и `Proxy-Authorization`. Значит, на этом пути поддельный `X-GxG-Identity` агента дойдёт до коннектора. Защита та же, что в ADR-0001: подпись, коннектор не примет JWS без ключа адаптера.
- **Docker MCP Gateway — failed.** Identity нет ни на одном пути. `mcp-exec` и code-mode вызывают handler напрямую, перехватчики видят только внешний вызов (`DG/pkg/gateway/mcpexec.go`, `codemode.go`). Для prompts и resources перехватчиков нет.

### 6. Обновление версии

Все три — репетиции, не MC-05. ToolHive: тот же `vmcp-tx.yaml` на `v0.66.0`, затем на `v0.67.0`, результаты вызовов и атак совпали ([`upgrade-from-v0.66.0/summary.json`](../../spikes/m4.1-gateways/toolhive/evidence/upgrade-from-v0.66.0/summary.json)). Состояния vMCP не хранит, миграций нет. Риск для C на ToolHive — точка `RegisterOBOStrategy`/`Strategy` во внутреннем Go API pre-1.0 проекта с еженедельными релизами: release notes `v0.67.0` содержат breaking changes. Docker MCP Gateway: тот же каталог на `v0.44.2` и `v0.44.3`, результаты совпали ([`previous-v0.44.2/summary.json`](../../spikes/m4.1-gateways/docker-mcp-gateway/evidence/previous-v0.44.2/summary.json)).

### 7–8. Зрелость и эксплуатация

- **ToolHive.** Apache-2.0; `SECURITY.md`: GHSA или security@stacklok.com. Образы подписаны (`.github/workflows/image-build-and-publish.yml`). Хранилище — файлы RunConfig, CRD/ConfigMap в Kubernetes, Redis для сессий vMCP. PostgreSQL нет. Аудит: JSON-события с `subjects.user` проверены вживую (`evidence/default/vmcp.log`, `"level":"AUDIT"`). OTel-трейсы и метрики, Prometheus `/metrics` на 9464 (по конфигу, не запускались).
- **Docker MCP Gateway.** MIT; `.github/SECURITY.md`: security@docker.com. Конфиг — файлы `~/.docker/mcp/`, профили в SQLite. Аудит уходит только в policy client Docker Desktop, вне него это no-op. Телеметрия — через Docker CLI. Шлюз рассчитан на Docker Desktop: без него приходится ставить `DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1` (снимает все SSRF-проверки для всех remote-серверов) и `--static`.

### 9. Стоимость перехода

| Что из M1/M4 | ToolHive vMCP | Docker MCP Gateway |
| --- | --- | --- |
| `IdentityVerifier` кита, формат JWS (SEC-03) | переиспользуется без изменений | переиспользуется, но подписывать нечего |
| Плагин `gxg_identity` (Python, cpex) | переписать на Go как стратегию vMCP (`Authenticate`: claims из identity, `ahs` по телу, JWS Ed25519), ~3–4 дня с тестами | форк шлюза + своя аутентификация |
| Модель tenant'ов (team ContextForge) | IdP с claim `tenant_id` (Keycloak или аналог), генерация Cedar-политик из реестра платформы, ~3–4 дня | инстанс на tenant, агенты неразличимы |
| Сборка и поставка | свой образ vMCP с нашей стратегией, пересборка на каждый релиз ToolHive, ~1–2 дня + постоянно | — |
| Стенд и атаки M4 | переиспользованы (этот спайк) | переиспользованы |
| Итого | **~8–12 дней**, плюс постоянная пересборка | не оценивается: нужен форк, отклонён |

## Что не проверено

- ToolHive: вариант C вживую (адаптер не писали), прокси `thv run` / proxyrunner и mutating webhook, overrides, Kubernetes-оператор, Redis-сессии, `token_exchange` с IdP, который ставит `aud`.
- Docker MCP Gateway: перехватчики на `sse`, HTTP-перехватчик со статусом 500, профили (feature flag), режим с docker socket.
- Patched-версии 50 GHSA ToolHive со страниц 2–6 (все опубликованы до релиза `v0.67.0`).
