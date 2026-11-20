# ADR-0001: Как identity агента доходит до коннектора

**Статус:** Accepted — вариант **C** (свой identity-адаптер, плагин `tool_pre_invoke` с JWS Ed25519). Решение по итогам спайка M4 на ContextForge `v1.1.1`, 2026-11-20. Вариант A отклонён: проваливает критерии 1–3.
**Связано:** [SEC-03, SEC-04](../specs/security-v1.md#identity), [AS-08](../specs/approval-service-v1.md#аутентификация-сервисов), [Roadmap → GO/NO-GO](../specs/roadmap-v1.md#go--no-go).

## Проблема

Коннектор должен знать проверенные `tenant_id` и `principal` вызывающего агента, чтобы проверять права на ресурс (GR) и создавать intents (AS-08). Агент обращается к ContextForge, а не к коннектору. Нужен механизм, который нельзя подделать ни агенту, ни соседнему сервису.

## Что должен дать любой вариант

Коннектор получает объект `VerifiedIdentity`:

```python
@dataclass(frozen=True)
class VerifiedIdentity:
    tenant_id: str        # = team ContextForge
    principal: str        # agent:<tenant>:<name>, неизменяемый
    issued_at: datetime
    expires_at: datetime
    request_id: str
    assertion: str | None # исходная проверяемая assertion, если есть (для AS-08)
    mode: Literal["signed_assertion", "per_tenant_instance"]
```

Критерии приёмки (все обязательны):
1. Агент не может повлиять на `tenant_id` и `principal` ни аргументами, ни заголовками, ни `_meta`.
2. Запрос в обход ContextForge не получает identity.
3. Повтор перехваченной identity через > 60 с отклоняется.
4. Identity работает на всех путях: прямые tools, virtual servers, алиасы.
5. Механизм переживает обновление ContextForge (проверяется тестом MC-05).

## Варианты

### A. Подписанное identity propagation ContextForge

ContextForge сам добавляет к downstream-запросу подписанные заголовки с пользователем и team (документировано, выключено по умолчанию, HMAC-подпись).

- Плюсы: ноль своего кода в пути, один экземпляр коннектора на всех tenant'ов.
- Минусы: HMAC — общий секрет, то есть коннектор и ContextForge могут подписывать друг за друга. Формат и состав полей нужно проверить. Неизвестно, есть ли защита от повтора.
- Проверить: какие заголовки, что входит в подпись (метод, путь, тело, время?), есть ли `team`/`tenant_id`, есть ли время и nonce, на всех ли путях вызова.

### B. Экземпляр коннектора на tenant

Для каждого tenant'а — отдельный экземпляр коннектора, зарегистрированный только в team этого tenant'а. ContextForge обращается к нему со своим service credential. `tenant_id` задаётся конфигурацией экземпляра. `principal` берётся из подписанной propagation (A) или, если её нет, остаётся `unknown_agent_of_tenant`.

- Плюсы: tenant-изоляция не зависит от заголовков.
- Минусы: N экземпляров на N tenant'ов. Без A нельзя различать агентов внутри tenant'а, то есть права на ресурс на уровне агента не работают.

### C. Свой identity-адаптер

Тонкий плагин ContextForge (`tool_pre_invoke` / `http_pre_request`) или sidecar. Он берёт аутентифицированного пользователя из контекста ContextForge и добавляет к вызову JWS (Ed25519, `aud` = коннектор, `exp` 60 с, `jti`, хеш тела), подписанный ключом, который есть только у адаптера.

- Плюсы: асимметричная подпись, защита от повтора, наш формат, проверка в `gxg-connector-kit`.
- Минусы: свой код в ContextForge-плагине, нужно проверять при каждом обновлении. Если плагин не срабатывает, вызов отклоняется коннектором (fail closed), но это всё равно отказ сервиса.

## Предварительное предпочтение

**A, если она проходит все 5 критериев; иначе C.** B — только как дополнительный слой для tenant'ов с особыми требованиями к изоляции.

## Протокол спайка

1. Поднять закреплённый ContextForge с PostgreSQL, два team, по два агента, включить identity propagation с подписью.
2. Поднять echo-MCP-сервер, логирующий все заголовки и `_meta`.
3. Для каждого пути (прямой tool, virtual server, алиас) записать заголовки и проверить подпись.
4. Попробовать подделать: заголовки от агента, `_meta`, повтор запроса, прямой вызов echo-сервера.
5. Записать результат по каждому критерию (passed / failed + доказательство) в этот ADR, сменить статус на Accepted с выбранным вариантом.
6. Перенести конкретный формат в [Security](../specs/security-v1.md#identity) вместо пометки VERIFY. Реализовать `IdentityVerifier` в `gxg-connector-kit`.

## Решение

**Вариант C.** Identity к коннектору доставляет наш плагин ContextForge `gxg_identity` (хук `tool_pre_invoke`). Он добавляет заголовок `X-GxG-Identity` с JWS Ed25519, ключ подписи есть только у адаптера. Коннектор проверяет JWS публичным ключом (`kid`), подделать подпись он не может. Встроенное identity propagation ContextForge (вариант A) не используется для авторизации: в compose оно выключено, как и было. Вариант B не нужен, его можно добавить как слой изоляции для отдельных tenant'ов.

Стенд, скрипты и доказательства: [`spikes/m4-contextforge/`](../../spikes/m4-contextforge/README.md). Прогон воспроизводится командой `spikes/m4-contextforge/run_spike.sh`.

### Закреплённая версия

| Компонент | Образ |
| --- | --- |
| ContextForge | `ghcr.io/ibm/mcp-context-forge:v1.1.1@sha256:16c7a8985c1a3528ba49fcab32951e06c9a41a171fc5c41d24b085283c4556a5` (OCI index; amd64 — `sha256:88479e8d…ba40`). Тег `v1.1.1` = коммит [`dbc643be`](https://github.com/IBM/mcp-context-forge/blob/dbc643befb6f412e62584179c8f0ebb0207d44fb), релиз 2026-11-10, он же `latest` на дату спайка |
| PostgreSQL | `postgres:18@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a` |
| Echo (только стенд) | `python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b` |

Почему `v1.1.1`: последний релиз (более новых тегов в `ghcr.io` нет), а [SECURITY.md](https://github.com/IBM/mcp-context-forge/blob/main/SECURITY.md) поддерживает только последнюю версию, без backport'ов. Тега `1.1.1` без `v` в реестре нет.

### Security advisories IBM на 2026-11-20

- [Security Bulletin 7296490](https://www.ibm.com/support/pages/node/7296490), CVE-2026-110279 (CVSS 8.1, `X-Upstream-Authorization` подменяет credential upstream): затронуты v1.0.0–v1.1.0, исправлено в **v1.1.1** ([PR #7341](https://github.com/IBM/mcp-context-forge/pull/7341)).
- [GitHub Security Advisories](https://github.com/IBM/mcp-context-forge/security/advisories): 19 GHSA, все прочитаны. У всех patched-версия ≤ v1.0.8: [GHSA-qmfj-jjw4-8qrq](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-qmfj-jjw4-8qrq), [GHSA-9qxh-fpx5-vxvw](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-9qxh-fpx5-vxvw), [GHSA-5424-f25v-29r8](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-5424-f25v-29r8), [GHSA-8pcq-mx48-hjvj](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-8pcq-mx48-hjvj), [GHSA-gv3m-3mvj-w6f2](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-gv3m-3mvj-w6f2), [GHSA-55gx-p285-xw7f](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-55gx-p285-xw7f), [GHSA-hxq2-5h3j-jqjv](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-hxq2-5h3j-jqjv), [GHSA-w8vq-gj3v-h664](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-w8vq-gj3v-h664), [GHSA-gj7g-7r6g-jc8v](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-gj7g-7r6g-jc8v), [GHSA-578v-pgw5-36gv](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-578v-pgw5-36gv), [GHSA-x39c-q2jx-f325](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-x39c-q2jx-f325), [GHSA-9hgc-g3w5-67cm](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-9hgc-g3w5-67cm), [GHSA-m8rv-5m6m-32ff](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-m8rv-5m6m-32ff), [GHSA-vwf3-4xxj-qg6h](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-vwf3-4xxj-qg6h), [GHSA-xm98-3vcf-fph7](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-xm98-3vcf-fph7), [GHSA-c7vv-9h9c-fvj4](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-c7vv-9h9c-fvj4), [GHSA-rj22-3ww8-gmv4](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-rj22-3ww8-gmv4), [GHSA-vgf8-3685-66j9](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-vgf8-3685-66j9), [GHSA-x6gc-rm5j-55mw](https://github.com/IBM/mcp-context-forge/security/advisories/GHSA-x6gc-rm5j-55mw).
- [OSV](https://osv.dev/) для PyPI `mcp-contextforge-gateway==1.1.1`: уязвимостей нет.
- Итог: по опубликованным диапазонам ни одно advisory не затрагивает v1.1.1. Диапазоны указаны мейнтейнерами, по коду мы их не сверяли. Оговорки: часть GHSA-c7vv про gateway/A2A помечена как «source-evident», GHSA-x6gc относится к отдельному пакету UI.

### Стенд

2 team (`t-alpha`, `t-beta`), в каждой 2 агента: пользователь `<name>@<tenant>.spike.example.com` с ролью `member` и API-токеном, выпущенным с `team_id` этой team. На каждую team — свой gateway на echo-MCP (`/mcp/<tenant>`, visibility `team`) и virtual server. Алиас: tool t-alpha переименован через `customName`. PostgreSQL 18, `AUTH_REQUIRED=true`, `ENABLE_HEADER_PASSTHROUGH=false` (по умолчанию).

### Что делает ContextForge v1.1.1 (вариант A), по коду и вживую

- Заголовки: `X-Forwarded-User-{Id, Email, Full-Name, Teams, Admin, Auth-Method}` + `X-Forwarded-User-Claims-Signature`; в `_meta.user` — те же поля без подписи. Захвачено: [`evidence/variant-a.echo-received.jsonl`](../../spikes/m4-contextforge/evidence/variant-a.echo-received.jsonl).
- Подпись: `hex(HMAC-SHA256(secret, f"{user_id}:{email}"))` — [`identity_propagation.py#L121-L124`](https://github.com/IBM/mcp-context-forge/blob/dbc643befb6f412e62584179c8f0ebb0207d44fb/mcpgateway/utils/identity_propagation.py#L121-L124). Teams, admin, время, nonce, audience, метод, путь и тело не подписаны. Секрет по умолчанию — `JWT_SECRET_KEY` ([#L58-L69](https://github.com/IBM/mcp-context-forge/blob/dbc643befb6f412e62584179c8f0ebb0207d44fb/mcpgateway/utils/identity_propagation.py#L58-L69)): тогда любой, кто может проверить подпись, может выпускать JWT ContextForge, в том числе admin.
- Документация расходится с кодом: [identity-propagation.md#L126-L128](https://github.com/IBM/mcp-context-forge/blob/dbc643befb6f412e62584179c8f0ebb0207d44fb/docs/docs/manage/identity-propagation.md#L126-L128) обещает подпись канонического JSON всех claims и `claims_signature` в `_meta`. В коде этого нет.
- Заголовки ставятся после passthrough ([`tool_service.py#L6334-L6337`](https://github.com/IBM/mcp-context-forge/blob/dbc643befb6f412e62584179c8f0ebb0207d44fb/mcpgateway/services/tool_service.py#L6334-L6337)), поэтому поддельный `X-Forwarded-User-*` от агента перезаписывается. `_meta.user` тоже перезаписывается ([#L181](https://github.com/IBM/mcp-context-forge/blob/dbc643befb6f412e62584179c8f0ebb0207d44fb/mcpgateway/utils/identity_propagation.py#L181)), остальные ключи `_meta` агента проходят как есть.

### Как устроен вариант C

- Плагин [`plugin/gxg_identity/plugin.py`](../../spikes/m4-contextforge/plugin/gxg_identity/plugin.py). Хук `tool_pre_invoke` выполняется после identity- и passthrough-заголовков; плагин может заменить заголовки целиком ([`tool_service.py#L6941-L6970`](https://github.com/IBM/mcp-context-forge/blob/dbc643befb6f412e62584179c8f0ebb0207d44fb/mcpgateway/services/tool_service.py#L6941-L6970)). `fail_on_plugin_error: true`, `mode: enforce`.
- Отказ (`PluginViolation`, вызов не уходит из ContextForge): нет аутентифицированного пользователя; токен не привязан ровно к одной team или эта team не совпадает с team tool'а (`metadata.tool.team_id`); team не зарегистрирована как tenant; email вне домена агентов tenant'а; gateway не зарегистрирован как коннектор.
- JWS: header `{"alg":"EdDSA","typ":"JWT","kid":…}`, claims `iss`=`gxg-identity-adapter`, `aud`=`gxg-connector:<gateway>`, `sub`=`agent:<tenant>:<name>`, `tenant_id`, `team_id`, `iat`, `exp`=`iat+60`, `jti`, `request_id`, `ahs`=sha256(канонический JSON `arguments`).
- Проверка коннектором ([`verify_jws.py`](../../spikes/m4-contextforge/verify_jws.py)): alg только EdDSA, известный `kid`, подпись, `iss`, `aud`, `iat`/`exp` с допуском 5 с и `exp−iat ≤ 60`, `ahs`, одноразовый `jti`, `sub` внутри `tenant_id`.

### Результаты по критериям

| # | Критерий | A (HMAC ContextForge) | C (наш плагин, выбран) |
| --- | --- | --- | --- |
| 1 | Агент не влияет на `tenant_id`/`principal` | **failed** | **passed** |
| 2 | В обход ContextForge identity нет | **failed** | **passed** |
| 3 | Повтор через > 60 с отклоняется | **failed** | **passed** |
| 4 | Все пути: прямой, virtual server, алиас | passed | **passed** |
| 5 | Переживает обновление ContextForge | passed (репетиция) | **passed** (репетиция; автотест MC-05 — этап C) |

**1. Подмена identity агентом.** Агент t-alpha прислал `X-Forwarded-User-Id/Email/Teams/Admin/Claims-Signature`, `X-Tenant-Id`, `X-GxG-Identity`, `_meta.user` и `_meta.tenant_id` с данными t-beta и `tenant_id`/`principal` в аргументах (`spike.py attacks`).
- A: заголовки и `_meta.user` перезаписаны настоящими значениями, но teams и admin не входят в подпись. Подпись остаётся валидной после смены `X-Forwarded-User-Teams` и `-Admin` (`verify_hmac.py`: `signature_still_valid_after_changing_teams_and_admin: true`). Значит, проверить tenant коннектор не может. Кроме того, токен platform admin (не агент, без team) доходит до tool'а tenant'а с `Admin: true` и без `Teams` (`admin_call`). → failed.
- C: во всех доставленных вызовах `sub`/`tenant_id` в JWS — настоящего агента. Поддельный `X-GxG-Identity` перезаписан. Вызов admin отклонён плагином: `GXG_IDENTITY_DENIED: caller token must be scoped to exactly the tool's team`. Прогон с `ENABLE_HEADER_PASSTHROUGH=true` и всеми identity-заголовками в allowlist (`compose.passthrough-misconfig.yaml`) не довёл до коннектора ни одного заголовка агента, даже `X-Tenant-Id`. Значит, этот прогон перезапись не доказывает, причина не выяснена в рамках timebox. Перезапись гарантирует порядок в коде: плагин выполняется после passthrough и ставит `X-GxG-Identity` последним. Подделку без ключа адаптера коннектор отклоняет в любом случае (критерий 2). Доказательства: [`variant-c.observations.jsonl`](../../spikes/m4-contextforge/evidence/variant-c.observations.jsonl), [`variant-c-passthrough-misconfig.*`](../../spikes/m4-contextforge/evidence/). → passed.
- Остаток риска: аргументы и прочие ключи `_meta` проходят к коннектору как есть. Коннектор обязан брать identity только из проверенного JWS (SEC-01).

**2. Обход ContextForge.** Запросы напрямую на echo `127.0.0.1:9000` (`spike.py replay`).
- A: без заголовков identity нет. Но перехваченные заголовки, отправленные напрямую, несут ту же подпись: она детерминирована, у каждого пользователя одна подпись на всё время (`distinct_signatures_per_user: 1`). Коннектор принимает такой запрос. → failed.
- C: `missing X-GxG-Identity`; JWS, подписанный чужим ключом, → `bad signature`; перехваченный JWS повторно → `replayed jti`; с подменёнными аргументами → `arguments hash mismatch` ([`variant-c.verify_jws.jsonl`](../../spikes/m4-contextforge/evidence/variant-c.verify_jws.jsonl)). → passed. Сетевое ограничение «только от ContextForge» (SEC-04) остаётся обязательным вторым слоем.

**3. Повтор через > 60 с.** Перехваченный `tools/call` повторён на echo через 65 с.
- A: подпись валидна, в заголовках нет времени, отличить повтор нельзя ([`variant-a.replay.log`](../../spikes/m4-contextforge/evidence/variant-a.replay.log), [`variant-a.verify_hmac.json`](../../spikes/m4-contextforge/evidence/variant-a.verify_hmac.json)). → failed.
- C: `expired or not yet valid` ([`variant-c.replay.log`](../../spikes/m4-contextforge/evidence/variant-c.replay.log)). → passed.

**4. Пути вызова.** `/rpc` (прямой), `/servers/{id}/mcp/` (virtual server), алиас через `/rpc` и через virtual server: у A заголовки есть на всех путях, у C валидный JWS на всех путях, для всех 4 агентов. Глобальный `/mcp/` в v1.1.1 отказывает team-scoped токену участника: `Streamable HTTP RBAC denied: permission=tools.execute` ([`streamablehttp_transport.py#L1290`](https://github.com/IBM/mcp-context-forge/blob/dbc643befb6f412e62584179c8f0ebb0207d44fb/mcpgateway/transports/streamablehttp_transport.py#L1290)). До коннектора вызов не доходит (fail closed), поэтому агенты используют `/rpc` или virtual server. Cross-team вызов tool'а t-beta агентом t-alpha отклоняется на всех путях: `Tool not found` / `Access denied` / HTTP 403. → passed.

**5. Обновление.** Стенд с плагином поднят на `v1.1.0` (`sha256:7eb21fb9…cff1c`), настроен, вызовы приняты. Затем образ заменён на закреплённый `v1.1.1` на том же томе PostgreSQL, без повторной настройки: прошли 3 миграции alembic, плагин загрузился, все вызовы после апгрейда несут валидный JWS, вызов admin по-прежнему отклоняется ([`variant-c-upgrade.verify_jws.jsonl`](../../spikes/m4-contextforge/evidence/variant-c-upgrade.verify_jws.jsonl)). → passed как репетиция. Тест MC-05 из Conformance Matrix должен прогонять то же самое при каждом обновлении. Плагин опирается на `GlobalContext.user_context`, `metadata["tool"].team_id` и `ToolPreInvokePayload.headers` из `cpex.framework`, и MC-05 должен ловить их изменение.

### Что дальше

- `IdentityVerifier` в `gxg-connector-kit` по образцу `verify_jws.py`: JWKS с `kid`, общий кеш `jti` на время `exp` (при нескольких репликах коннектора — общий store), fail closed.
- Плагин — в отдельный пакет с тестами. Ключ — из secret store, ротация через `kid`. Отображение team → tenant и gateway → `aud` — из реестра платформы, а не из YAML.
- `IDENTITY_PROPAGATION_ENABLED` и `IDENTITY_SIGN_CLAIMS` оставить `false`. Если когда-нибудь включать, то только с отдельным `IDENTITY_CLAIMS_SECRET` и никогда не как источник полномочий.
