# Dev-стенд M5: read-only PoC через ContextForge

Только локальная разработка, только синтетические данные. Стенд — корневой [`compose.yaml`](../compose.yaml). Веха — [M5](../docs/implementation-plan-v1.md#m5--read-only-poc-через-contextforge). Результат прогона — [`reports/conformance/M5.json`](../reports/conformance/M5.json).

## Что поднимается

| Сервис | Образ | Сети | Порт наружу |
| --- | --- | --- | --- |
| `contextforge` | `ghcr.io/ibm/mcp-context-forge:v1.1.1@sha256:16c7a898…56a5` (SEC-22) | `edge`, `db`, `connectors` (10.250.0.2) | `127.0.0.1:4444` |
| `postgres` | `postgres:18@sha256:eb63372c…e1a` | `db` (internal) | нет |
| `demo` | `connectors/demo/Dockerfile` с `KEEP_TEST_CONTROLS=1` | `connectors` (internal, 10.250.0.10) | нет |

- **Сеть (SEC-04, SEC-13, AC-M5-03).** Агенты — в `edge`: там только ContextForge. `connectors` — `internal: true`, `inhibit_ipv4`: у хоста Docker на этом мосту нет адреса, поэтому нет и маршрута к `demo`. `demo` знает только ContextForge.
- **Identity (ADR-0001, вариант C).** Плагин [`gateway/contextforge/gxg_identity`](../gateway/contextforge/gxg_identity) (`tool_pre_invoke`) подписывает `X-GxG-Identity` (JWS EdDSA, `typ` `gxg-identity+jwt`) ключом адаптера. `demo` проверяет его через `gxg_connector_kit.cf_identity.ContextForgeVerifier` (`DEMO_IDENTITY_KEYS`). `PLUGINS_FAIL_ON_PLUGIN_ERROR=true`: если плагин не загрузился, ContextForge не стартует.
- **Политика tools.** `unified_pdp` из образа, движок `native`, `default_decision: deny`. Правила — [`unified_pdp.rules.json`](unified_pdp.rules.json): allow только для точного email агента и точного имени tool. Deny для `*execute*`/`*reconcile*` срабатывает раньше любого allow. Плагин PDP стоит первым (priority 10), адаптер identity — после него (1000).
- **SSRF (SEC-15).** `SSRF_ALLOW_PRIVATE_NETWORKS=false`. Исключение — `10.250.0.8/30` (адрес `demo`). В `extra_hosts` два имени, которые резолвятся в metadata и RFC1918: на них проверяется шаг DNS (SC-07).
- **Tenants.** Team `t-alpha`, `t-beta` (private). В каждой агенты `reader` и `nobody` (`<name>@<tenant>.agents.gxg.example.com`, роль `member`, один API-токен с `team_id`). Gateway `demo-<tenant>` → `http://demo:8080/mcp` (visibility `team`), virtual server `vs-<tenant>`. Алиас `alpha_boards` у `demo.boards.list` t-alpha. REST-tool `demo.message.execute` в t-alpha — мишень правила EFF-02.
- **Grants.** Bundle из `connectors/demo/fixtures/grants/`, подписан ключом стенда.

## Запуск

Нужны Docker с Compose v2 и `uv`.

```bash
stand/run.sh                 # всё: init, build, up, bootstrap, тесты, рестарт, тесты, пересоздание, тесты, отчёт
KEEP=1 stand/run.sh          # то же, стенд остаётся запущенным
```

По шагам:

```bash
uv run python stand/init.py       # .env и stand/secrets/: секреты, ключи, bundle (git-ignored, один раз)
docker build --build-arg KEEP_TEST_CONTROLS=1 -t gxg-demo:stand -f connectors/demo/Dockerfile .
docker compose up -d --wait
uv run python stand/bootstrap.py  # идемпотентно: team, агенты, токены, gateways, servers, конфиг плагина
uv run pytest stand/tests -p no:cacheprovider --force-enable-socket -o pythonpath="stand/tests stand gateway/contextforge"
docker compose down               # остановить; -v удалит и БД
```

В песочнице с TLS-прокси сборке нужен CA: `DOCKER_BUILD_FLAGS="--network host --secret id=ca-bundle,src=<ca.pem>" stand/run.sh`.

## Эксплуатация (runbook)

| Ситуация | Что делать |
| --- | --- |
| ContextForge не стартует, в логе `Plugin initialization failed` | Плагин не загрузился: нет ключа `stand/secrets/adapter_ed25519.pem`, сломан `stand/secrets/plugins.yaml` или правила. Так задумано (fail closed). Исправить файл, затем `docker compose up -d` |
| Все вызовы агентов → `unauthenticated` от `demo` | Ключ адаптера не совпадает с `DEMO_IDENTITY_KEYS` в `.env` (например, после ротации). Сверить `kid` |
| Вызовы → `PDP_DENY` | Нет правила allow для этого email и имени tool. Правила — `stand/unified_pdp.rules.json`, после правки — `docker compose restart contextforge` |
| `demo` `/readyz` = 503, вызовы → `temporarily_unavailable` | Нет валидного bundle: `stand/secrets/bundle/` повреждён или старше 24 ч (GR-12, GR-13). Пересобрать: удалить каталог и запустить `stand/init.py` |
| Новый tool у `demo` | После `bootstrap.py` добавить имя ContextForge (`demo-<tenant>-<tool>`) в правила нужных агентов. По умолчанию он запрещён |
| Отозвать токен агента | `DELETE /v1/tokens/{id}` от admin. Действует через ≤ `AUTH_CACHE_REVOCATION_TTL` (на стенде 5 с) |
| Ротация ключа адаптера | Новый `kid` добавить в `DEMO_IDENTITY_KEYS` (через запятую), сменить ключ и `kid` в `plugins.yaml`, перезапустить ContextForge, затем убрать старый `kid` и пересоздать `demo` (MC-04) |
| Рестарт хоста | `docker compose up -d`: политики, gateways и токены лежат в PostgreSQL, конфиг плагина и bundle — в `stand/secrets/`. Повторного bootstrap не нужно (AC-M5-04) |

## Тесты

[`stand/tests/`](tests): SC-01…SC-10 ([`test_sc.py`](tests/test_sc.py)) и MC-01…MC-04 ([`test_mc.py`](tests/test_mc.py)) через ContextForge. Тестовые контроли `demo` доступны только изнутри контейнера: `docker compose exec demo` → `127.0.0.1:8090`. Прямые запросы в обход ContextForge отправляет одноразовый контейнер в нужной сети (`standlib.Stand.probe`). Корневой `pytest` эти тесты не запускает: им нужен стенд и сокеты. В CI их запускает job `m5-stand`.

## Что сознательно не сделано

- Ключ адаптера и секреты лежат в файлах, а не в secret store; отображение team → tenant генерирует `bootstrap.py`, а не реестр платформы. `shortcut`, для production.
- Хранилище `jti` в процессе `demo`: одна реплика.
- TLS нет: стенд слушает только `127.0.0.1`.
