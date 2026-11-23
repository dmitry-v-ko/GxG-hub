# M13: набор развёртывания на Railway (офлайн)

Ветка `claude/m13-deploy` от `origin/claude/integration` (445395b). В Railway, Tailscale, Telegram и Supabase ничего не создавалось и не менялось. Токена у сессии нет, скрипт против живого API **не запускался**.

## Что сделано

| Файл | Что |
| --- | --- |
| `deploy/railway/services.yaml` | 13 сервисов: `contextforge`, `contextforge-db`, `approval-db`, Approval `api`, `mcp`, `executor`, `decider`, `ui`, `notifier`, `sweeper`, jobs `migrate` и `db-init` (временный), `telegram`. Для каждого: образ или Dockerfile, команда, порт, volume, переменные (литералы / владелец / сгенерированные), секреты с привязкой к строке AS-28, private-хост `<имя>.railway.internal` |
| `deploy/railway/entrypoint.sh` | обёртка контейнера: `GXG_SECRET__X` / `GXG_CONFIG__X` → файлы в `/dev/shm` и `X_FILE` (процессы Approval читают секреты только файлами), переменные снимаются до старта процесса; при `TS_HOSTNAME`: `tailscaled --tun=userspace-networking`, `tailscale up` (ключ из `TS_AUTHKEY` через временный файл), `tailscale serve --https`, отказ от старта при включённом Funnel |
| `deploy/railway/{contextforge,approval,ui,notifier}.Dockerfile` | образы Railway. ContextForge: тот же digest, что в `compose.yaml`, плагин `gxg_identity`, tailscale. `ui`/`notifier`: оригинальная сборка + обёртка (тест следит за дрейфом) |
| `deploy/railway/db_init.py`, `contextforge/*` | job ролей БД (AS-27); начальные `plugins.yaml` и deny-правила ContextForge |
| `scripts/deploy/railway_deploy.py` | `--dry-run`, применение, `--verify` (AC-M14-11), `--finalize` (удалить `db-init`) |
| `tests/deploy/` | 25 тестов без сети: мок-GraphQL, entrypoint со stub-бинарниками tailscale, дрейф Dockerfile |
| `scripts/ci/check-all.sh`, `pyproject.toml` | `tests/deploy` в pytest и mypy `--strict`, `--dry-run` в check-all |
| `docs/runbooks/m13-pilot-deploy.md` | порядок, руки владельца, проверки |

Точки входа в tailnet (ADR-0003): `contextforge` — `:443` только пути `/servers`, `/mcp`, `/health` (остальное, включая `/admin`, не проксируется), `:8443` — всё, для операторов; `ui` — `:443` на loopback-процесс. Funnel нигде не включается.

## Тесты

`uv run pytest tests/deploy`: 25 passed. `ruff check`, `ruff format --check`, `mypy --strict scripts tests/deploy`, gitleaks tree: чисто. Полный `uv run pytest`: 1251 passed, 75 error — все 75 в `connectors/telegram/tests/test_telegram.py` (в песочнице нет расширения pgvector для PostgreSQL 16: `extension "vector" is not available`), к набору не относятся; failed — 0. `scripts/ci/check-all.sh` целиком не запускался (нужны Docker и клон gxg_agents_v2). mutmut не нужен (решение владельца).

- **Идемпотентность:** второй прогон даёт 0 мутаций API и не меняет ни одного значения (секреты не ротируются); правка литерала откатывается, секрет не трогается; частично заданный keyset отклоняется, а не ротируется.
- **Утечки:** по маркерам — все значения секретных переменных, которые дошли до мока (≥ 15), не встречаются в stdout и stderr трёх прогонов; токен и текст ошибки API с токеном редактируются. Контрольная мутация (убрать редактор и печатать значения) краснит оба теста.
- **`--verify`:** зелёный на чистом развёртывании; красный на публичном домене Railway, custom domain, TCP Proxy, постороннем секрете у executor (AS-28), `*.up.railway.app` вместо private URL, оставшемся `db-init`, shared-переменных. Применение само закрывает домен, добавленный кем-то вручную.
- **entrypoint:** файлы 0600, переменные не видны процессу, ключ не попадает в argv и вывод, serve-правила и userspace-режим, отказ без `TS_AUTHKEY` и при включённом Funnel.

## `--dry-run`

```text
warning: The `UV_NATIVE_TLS` environment variable is deprecated and will be removed in a future release. Use `UV_SYSTEM_CERTS` instead.
project gxg-hub-pilot, environment production, tenant t-pilot
public exposure: none (no Railway domain, no custom domain, no TCP proxy); Funnel off
- contextforge-db [postgres] ghcr.io/railwayapp-templates/postgres-ssl:16
    host: contextforge-db.railway.internal
    volume: /var/lib/postgresql/data
    literals: PGDATA, POSTGRES_DB, POSTGRES_USER
    secret POSTGRES_PASSWORD: generated (random)
- approval-db [postgres] ghcr.io/railwayapp-templates/postgres-ssl:16
    host: approval-db.railway.internal
    volume: /var/lib/postgresql/data
    literals: PGDATA, POSTGRES_DB, POSTGRES_USER
    secret POSTGRES_PASSWORD: generated (random)
- contextforge [app] dmitry-v-ko/GxG-hub deploy/railway/contextforge.Dockerfile
    host: contextforge.railway.internal:4444
    literals: AUTH_CACHE_REVOCATION_TTL, AUTH_REQUIRED, EMAIL_AUTH_ENABLED, ENABLE_HEADER_PASSTHROUGH, GXG_TS_SERVE, HOST, IDENTITY_PROPAGATION_ENABLED, JWT_AUDIENCE, JWT_ISSUER, LOG_LEVEL, MCPGATEWAY_ADMIN_API_ENABLED, MCPGATEWAY_DIRECT_PROXY_ENABLED, MCPGATEWAY_UI_ENABLED, PLATFORM_ADMIN_FULL_NAME, PLUGINS_CONFIG_FILE, PLUGINS_ENABLED, PLUGINS_FAIL_ON_PLUGIN_ERROR, PORT, SSRF_ALLOWED_NETWORKS, SSRF_ALLOW_LOCALHOST, SSRF_ALLOW_PRIVATE_NETWORKS, SSRF_PROTECTION_ENABLED, TOOL_TIMEOUT, TS_HOSTNAME, TS_TAGS
    secret DATABASE_URL: generated (tpl)
    secret JWT_SECRET_KEY: generated (random)
    secret AUTH_ENCRYPTION_SECRET: generated (random)
    secret PLATFORM_ADMIN_EMAIL: owner sets it in Railway
    secret PLATFORM_ADMIN_PASSWORD: generated (random)
    secret DEFAULT_USER_PASSWORD: generated (random)
    secret BASIC_AUTH_PASSWORD: generated (random)
    secret GXG_SECRET__ADAPTER_KEY: generated (ed25519_pem)
    secret TS_AUTHKEY: owner sets it in Railway
    owner variable APP_DOMAIN
    owner variable CF_START
    generated/seeded once GXG_CONFIG__PLUGINS_YAML
    generated/seeded once GXG_CONFIG__UNIFIED_PDP_RULES
    generated/seeded once GXG_IDENTITY_KEYS
- migrate [job] dmitry-v-ko/GxG-hub deploy/railway/approval.Dockerfile
    host: migrate.railway.internal, start: gxg-approval migrate
    literals: -
    secret GXG_SECRET__GXG_DB_DSN: generated (db_dsn), AS-28 db_dsn
- db-init [job, transient] dmitry-v-ko/GxG-hub deploy/railway/approval.Dockerfile
    host: db-init.railway.internal, start: python /opt/gxg/db_init.py
    literals: -
    secret GXG_SECRET__GXG_SUPERUSER_DSN: generated (superuser_dsn), AS-28 bootstrap
    secret GXG_SECRET__GXG_ROLE_PASSWORDS: generated (role_passwords), AS-28 bootstrap
- api [app] dmitry-v-ko/GxG-hub deploy/railway/approval.Dockerfile
    host: api.railway.internal:8090, start: gxg-approval api
    volume: /var/lib/gxg
    literals: GXG_BUNDLE_STATE_FILE, GXG_LISTEN, GXG_SERVICE_ID
    secret GXG_SECRET__GXG_DB_DSN: generated (db_dsn), AS-28 db_dsn
    owner variable GXG_BUNDLE_KEYS
    owner variable GXG_BUNDLE_B64
    generated/seeded once GXG_CONFIG__GXG_EXEC_JWKS
    generated/seeded once GXG_CONFIG__GXG_PAYLOAD_PUBLIC_KEYS
- mcp [app] dmitry-v-ko/GxG-hub deploy/railway/approval.Dockerfile
    host: mcp.railway.internal, start: gxg-approval mcp
    literals: GXG_API_AUDIENCE, GXG_API_URL, GXG_SERVICE_ID
    secret GXG_SECRET__GXG_SVC_KEY: generated (ed25519_seed), AS-28 svc_key
    generated/seeded once GXG_SVC_PUBLIC_KEY
- executor [app] dmitry-v-ko/GxG-hub deploy/railway/approval.Dockerfile
    host: executor.railway.internal:9101, start: gxg-approval executor
    volume: /var/lib/gxg
    literals: GXG_BUNDLE_STATE_FILE, GXG_METRICS_LISTEN, GXG_SERVICE_ID
    secret GXG_SECRET__GXG_DB_DSN: generated (db_dsn), AS-28 db_dsn
    secret GXG_SECRET__GXG_SVC_KEY: generated (ed25519_seed), AS-28 svc_key
    secret GXG_SECRET__GXG_EXEC_SIGNING_KEYS: generated (keyset), AS-28 exec_signing
    secret GXG_SECRET__GXG_PAYLOAD_PRIVATE_KEYS: generated (keyset), AS-28 payload_args
    owner variable GXG_BUNDLE_KEYS
    owner variable GXG_BUNDLE_B64
    generated/seeded once GXG_SVC_PUBLIC_KEY
- sweeper [app] dmitry-v-ko/GxG-hub deploy/railway/approval.Dockerfile
    host: sweeper.railway.internal, start: gxg-approval sweeper
    literals: GXG_RETENTION_DAYS, GXG_SWEEP_INTERVAL_S
    secret GXG_SECRET__GXG_DB_DSN: generated (db_dsn), AS-28 db_dsn
- decider [app] dmitry-v-ko/GxG-hub deploy/railway/approval.Dockerfile
    host: decider.railway.internal:8093, start: gxg-approval decider
    literals: GXG_LISTEN, GXG_UI_PUBLIC_URL
    secret GXG_SECRET__GXG_DB_DSN: generated (db_dsn), AS-28 db_dsn
- ui [app] dmitry-v-ko/GxG-hub deploy/railway/ui.Dockerfile
    host: ui.railway.internal:8443
    volume: /var/lib/gxg
    literals: GXG_BUNDLE_STATE_FILE, GXG_DECIDER_URL, GXG_LISTEN, GXG_TS_SERVE, TS_HOSTNAME, TS_TAGS
    secret GXG_SECRET__GXG_DB_DSN: generated (db_dsn), AS-28 db_dsn
    secret GXG_SECRET__GXG_UI_SESSION_KEY: generated (random), AS-28 ui_secrets
    secret GXG_SECRET__GXG_OIDC_CLIENT_SECRET: owner sets it in Railway, AS-28 ui_secrets
    secret GXG_SECRET__GXG_PREVIEW_KEYS: generated (keyset), AS-28 payload_preview
    secret TS_AUTHKEY: owner sets it in Railway, AS-28 tailscale
    owner variable GXG_UI_PUBLIC_URL
    owner variable GXG_OIDC_ISSUER
    owner variable GXG_OIDC_CLIENT_ID
    owner variable GXG_BUNDLE_KEYS
    owner variable GXG_BUNDLE_B64
- notifier [app] dmitry-v-ko/GxG-hub deploy/railway/notifier.Dockerfile
    host: notifier.railway.internal
    volume: /var/lib/gxg
    literals: GXG_BUNDLE_STATE_FILE, GXG_TELEGRAM_API_URL, GXG_UI_PUBLIC_URL
    secret GXG_SECRET__GXG_DB_DSN: generated (db_dsn), AS-28 db_dsn
    secret GXG_SECRET__GXG_TELEGRAM_TOKEN: owner sets it in Railway, AS-28 telegram_bot_token
    secret GXG_SECRET__WEBHOOK__signals: generated (hmac), AS-28 webhook_secrets
    owner variable GXG_BUNDLE_KEYS
    owner variable GXG_BUNDLE_B64
- telegram [app] dmitry-v-ko/gxg-telegram-service Dockerfile
    host: telegram.railway.internal:8080
    volume: /var/lib/telegram
    literals: TELEGRAM_IDENTITY_KEYS
    secret TELEGRAM_API_ID: owner sets it in Railway
    secret TELEGRAM_API_HASH: owner sets it in Railway
    secret TELEGRAM_ARCHIVE_DSN: owner sets it in Railway
    secret TELEGRAM_WEBHOOK_HMAC: generated (hmac)
    owner variable TELEGRAM_CONNECTIONS
    owner variable TELEGRAM_BUNDLE_KEYS
    owner variable GXG_BUNDLE_B64
dry-run: no API calls made
```

## Репозиторий `gxg-telegram-service`

`add_repo` не вызывался. Сервис описан по ADR-0005, `service-integration-v1.md` и `connectors/telegram/README.md`. **Имена переменных (`TELEGRAM_*`), путь Dockerfile (`Dockerfile`), способ чтения bundle и identity-ключей, команда `gxg-telegram-admin login` не сверены с его README.** Сверить до первого развёртывания.

## Открытые вопросы

1. **API Railway не проверен вживую.** Запросы и поля (`serviceCreate` с `source.repo`/`branch`, `serviceInstanceUpdate`, `variables(unrendered)`, `domains`, `tcpProxies`, `volumeCreate`) написаны по публичной схеме и покрыты только моком. Первый запуск: `--dry-run`, затем на пустом проекте; расхождение схемы вернёт `Railway API <операция>: <сообщение>`.
2. **Postgres.** Вместо Postgres template — образ `ghcr.io/railwayapp-templates/postgres-ssl:16` как обычный сервис (имена и переменные детерминированы, TCP Proxy не создаётся). Тег, не digest. Если нужен именно template, поменять `kind: postgres` и учесть, что он создаёт TCP Proxy (скрипт его удалит).
3. **Закрыто: команда ContextForge.** Из конфигурации образа по digest (registry API ghcr.io): `Entrypoint` нет, `Cmd` `./docker-entrypoint.sh`, `WorkingDir` `/app`, пользователь `10001`. Записано в Dockerfile, переменная владельца не нужна.
4. **Tailscale.** Образ `tailscale/tailscale:v1.84.2` закреплён тегом, не digest (SEC-22). Нужна поддержка `--auth-key=file:`. `policies/network/tailscale-acl.hujson` в репозитории нет (M14): теги `tag:gateway`, `tag:approval-ui`, группы и запрет `funnel` в `nodeAttrs` владелец задаёт в tailnet.
5. **Railway private networking слабее AS-29.** Compose разводил процессы по парам сетей; в Railway любой сервис проекта достижим для любого. `decider`, `api`, `telegram` защищены только своей аутентификацией (service JWT, passkey, identity). Egress-allowlist (SEC-15, AS-29) Railway не даёт. Внутренний трафик — http поверх сети Railway, AS-07 просит TLS. `SSRF_ALLOWED_NETWORKS=["fd12::/16"]` у ContextForge — гипотеза про адреса private network, проверить при первой регистрации gateway и в AC-M14-03.
6. **Закрыто: `mcp`.** Подключён `ContextForgeVerifier` кита (вариант C): ключи `GXG_IDENTITY_KEYS` (ссылка на `contextforge.GXG_IDENTITY_KEYS`), проверка привязана к вызову (`tool`, хеш аргументов). Gateway в ContextForge регистрируется на `http://mcp.railway.internal:8091/mcp`, audience в `connectors` плагина — `approval-mcp`. Тесты: `approval/tests/test_mcp_identity.py`.
7. **`admin` и приглашения passkey.** `admin` Approval (`tag:approval-admin`) не входит в список задания, в наборе его нет. `gxg-approval passkeys invite` ходит через admin API: для шага 7 runbook'а `admin` нужен, либо приглашение выдаёт другой способ.
8. **Bundle.** Доставляется переменной `GXG_BUNDLE_B64` (tar.gz, base64); лимит размера переменной не проверен. Освежать нужно раз в ≤ 12 ч (GR-13): автоматики с токеном Railway в наборе нет.
9. **Ротация ключей Approval.** `gxg-approval keys rotate` — файловая операция хоста. На Railway ключи — переменные, ротации там нет; процедура нужна к M14. Скрипт ключи не ротирует и отклоняет частично заданную пару.
10. **Секрет вебхука Signals.** `GXG_SECRET__WEBHOOK__signals` создаётся скриптом; значение Signals владелец переносит из Railway UI, имя должно совпасть с `secret://signals` подписки.
11. **База архива Telegram** — третий Postgres `telegram-db` (pgvector, образ стенда), DSN генерируется. Схему и миграции накатывает сам `gxg-telegram-service`.
12. **`db-init`.** Роли и `ALTER DATABASE … OWNER TO gxg_owner` не проверены на настоящем PostgreSQL (в тестах его нет); сверить с миграциями. `db-init` держит пароли всех ролей, поэтому временный и удаляется `--finalize`; `--verify` красный, пока он есть.
13. **AS-28 и `api`.** В таблице у `api` свой ключ service JWT, в `compose.yaml` его нет (`api` только проверяет). Набор следует compose.
14. **Bootstrap ContextForge** (`stand/bootstrap.py`) под Railway не адаптирован: шаг 6 runbook'а ручной.
15. **Healthcheck** задан для `contextforge`, `api`, `decider`, `telegram` (пути взяты из кода и README; `telegram` не проверен).
