# M13: пилот на Railway, живое состояние

Проект `gxg-hub-pilot`, окружение `production`, ветка `claude/m13-deploy`. Публичных доменов, TCP Proxy и shared-переменных нашего набора нет. Значения секретов не читались и не печатались; единственное исключение — ключ подписи bundle, см. п. 1.

## Статус сервисов

| Сервис | Статус | Комментарий |
| --- | --- | --- |
| `approval-db`, `contextforge-db`, `telegram-db` | SUCCESS | |
| `migrate` | SUCCESS | |
| `db-init` | удалён | роли созданы, `--finalize` выполнен |
| `api`, `decider` | SUCCESS | healthcheck `/healthz` проходит |
| `executor`, `mcp`, `sweeper` | SUCCESS | bundle загружен |
| `notifier` | SUCCESS | в логах 429 от Telegram `getUpdates`: пока шли пересборки, бота опрашивали два экземпляра сразу. Если не уйдёт — проверить, что бота не опрашивает что-то ещё |
| `bundle-signer` | без деплоя | хранилище ключа подписи bundle |
| `ui`, `admin`, `contextforge` | **CRASHED / DEPLOYING** | `requested tags [tag:approval-ui / tag:approval-admin / tag:gateway] are invalid or not permitted`. Tailscale отказывает ключу, это настройки tailnet владельца (см. «Владельцу») |
| `telegram` | **FAILED** | собрать нечем (см. ниже) |

`railway_deploy.py --verify`: код 0 (единственная заметка — shared-переменная `OPENROUTER_API_KEY`, не наша). `pilot_bundle.py --check`: ключи реестра равны живым `GXG_SVC_PUBLIC_KEY`.

## 1. Начальный bundle

- Файлы: `policies/pilot/` (`platform/registry.yaml`, `tenants/t-pilot.yaml`, `approval/t-pilot.yaml`), без секретов. `gxg-policy lint`: 0 ошибок.
- Tenant `t-pilot`, connection `tg-pilot` (domain `telegram`), агент `agent:t-pilot:pilot`. Humans и grants пусты. Политика: `telegram.message.propose` → `require_approval`, `auto_approve` нет.
- Реестр: `executor` и `approval-mcp` с живыми публичными ключами. **Без `api`**: у него нет своего ключа (он только проверяет service JWT, services.yaml). **Без telegram-коннектора**: ключа у него нет, сервис не запущен; и схема реестра требует для коннектора `https://` в `internal_url`, а private network Railway отдаёт http (AS-07). Нужно решение владельца до первого `execute`.
- Перевыпуск: `RAILWAY_TOKEN=… RAILWAY_USER_AGENT=curl/8.5.0 python scripts/deploy/pilot_bundle.py --deploy`. Скрипт собирает `gxg-policy build`, проверяет `gxg-policy verify`, пишет `GXG_BUNDLE_KEYS` (JSON `kid → ключ` для процессов Approval, `kid=ключ` для telegram) и `GXG_BUNDLE_B64` в `api`, `executor`, `ui`, `notifier`, `telegram` и передеплоит их. Первый bundle выпущен 2026-11-23 23:05 UTC; **старше 24 ч сервисы его отвергают, перевыпускать не реже чем раз в 12 ч** (контейнер распаковывает переменную при старте, автоматики нет).
- **Где ключ подписи.** В `admin` его держать нельзя: AS-28 фиксирует набор его секретов, `--verify` стал бы красным. Выбран отдельный сервис `bundle-signer` (`kind: vault` в `services.yaml`, без источника, не деплоится), переменная `GXG_BUNDLE_SIGNING_KEY`; публичная часть — `GXG_BUNDLE_PUBLIC_KEY`. Ключ создан внутри `railway_deploy.py`. **Отступление от «не читать секреты»:** `pilot_bundle.py` читает этот ключ из Railway в памяти, чтобы подписать bundle; он не печатается и не пишется на диск (редактор вывода его знает). Иначе перевыпустить bundle нечем. Если нужно строже — держать ключ вне Railway и подписывать на машине владельца (`--signing-key-env --build-only`).

## 2. Что исправлено

| Коммит | Что |
| --- | --- |
| `06af287` | `policies/pilot/`, `pilot_bundle.py`, сервис `bundle-signer` (`kind: vault`, AS-28 `bundle_signing`), `RAILWAY_USER_AGENT` для API, `restartPolicyMaxRetries` читается (скрипт больше не «обновляет» его каждый раз) |
| `6278901` | `GXG_BUNDLE_KEYS` у процессов Approval — JSON, а не `kid=ключ` (executor падал на разборе) |
| `3a26686` | entrypoint стартует от root, берёт volume (`chown`) и переходит на `gxg` через `setpriv`: Railway монтирует volumes root-владельцем, `bundle-state.json` не писался, executor/api сообщали «no valid bundle». Тест |
| `538319a` | lint |
| `75c0a64` | `PORT` у `api` и `decider`: healthcheck Railway ходит на `$PORT`, без него был «service unavailable» |

Первые падения `mcp` и `sweeper` («missing configuration …_FILE») были гонкой: деплой стартовал раньше переменных; обычный redeploy исправил.

### Telegram: обновление (после сообщения оркестратора)

Коммит `7d3f0f2`-серия (ветка `claude/m13-deploy`): `telegram` переключён на `dmitry-v-ko/gxg-telegram-service@claude/m13-railway` (`serviceConnect`), добавлены `TELEGRAM_SESSIONS`, `TELEGRAM_SESSION_WORKDIR`, `TELEGRAM_CONNECTIONS=t-pilot=tg-pilot:tg-pilot`, `TELEGRAM_APPROVAL_URL`, сгенерированы `TELEGRAM_CHAT_REF_SECRET` и `TELEGRAM_SVC_KEY` (публичная часть — `TELEGRAM_SVC_PUBLIC_KEY`). Сборка на 17bfe66 всё ещё падает: `"/deploy/railway-entrypoint.sh": not found` (вероятно, `.dockerignore` репозитория сервиса исключает `deploy/`; убрать исключение или положить файл вне `deploy/`). Не сделано: `TELEGRAM_SERVICE_DSN` и роли `gxg_telegram_service`/`gxg_telegram_reader` (нужен SQL из `db/` сервиса, репозиторий мне недоступен), поэтому `TELEGRAM_ARCHIVE_DSN` пока на роли владельца архива; коннектор в реестр bundle не добавлен (нужны порт — в spec 8080, оркестратор назвал 8081 — и решение по https, AS-07).

### Telegram: обновление 2 (ветка сервиса dd56300)

Сделано (коммит «telegram-db-init job…»): задача `telegram-db-init` (образ telegram, `/app/.venv/bin/python`, скрипт `deploy/railway/telegram_db_init.py` приходит в переменной `GXG_TGDB_INIT_PY`): миграции в порядке оркестратора (только если схемы архива нет), роли из `service_role.sql` и `reader_role.sql` (если ролей нет), `ALTER ROLE … PASSWORD`, строка `tg-pilot` в `capability_hub.connections` (`ON CONFLICT DO NOTHING`). `TELEGRAM_SERVICE_DSN` и `TELEGRAM_ARCHIVE_DSN` указывают на роли `gxg_telegram_service` и `gxg_telegram_reader`, пароли сгенерированы (прежнюю DSN на роли владельца архива я удалил, её создавал скрипт). У `telegram` убран healthcheck: `/readyz` честно 503 до входа в Telegram, а деплой должен быть SUCCESS. Порт MCP 8080. Коннектор в реестр bundle не добавлялся (по просьбе оркестратора).

**Не работает:** сборка `telegram` и `telegram-db-init` падает на `uv sync --locked`: зависимость `gxg-connector-kit` тянется как `git+https://github.com/dmitry-v-ko/gxg-hub@7e7ce56…#subdirectory=packages/gxg-connector-kit`, а GxG-hub приватный, анонимный clone в сборке невозможен. Нужно в репозитории сервиса: vendoring пакета в образ (COPY) или публичный доступ/токен для сборки. До этого роли в `telegram-db` не созданы, `telegram` в FAILED. Задача `telegram-db-init` оставлена в проекте (нужна после починки образа), `--verify` из-за неё красный; после её запуска — `railway_deploy.py --finalize`. Задачу `db-init` (она пересоздаётся каждым запуском скрипта) я удалил вручную.

### Telegram: обновление 3 (ветка сервиса 6e0cb4e, кит в `vendor/`)

Сборка проходит, **`telegram` в SUCCESS** (процесс жив; healthcheck снят: API Railway игнорирует `null`, очищать нужно пустой строкой, скрипт исправлен). Остаётся: `telegram-db-init` не может применить миграции — в образе нет ни одного `.sql` под `/app` (`/app/db` не копируется Dockerfile'ом, `.dockerignore` это не лечит нужен `COPY db /app/db`). Пока роли `gxg_telegram_service`/`gxg_telegram_reader` и строка `tg-pilot` не созданы; DSN на них уже в переменных. После правки образа: redeploy `telegram-db-init`, затем `telegram`, затем `railway_deploy.py --finalize`.

### Telegram: исходное состояние (до обновления)

Репозиторий `dmitry-v-ko/gxg-telegram-service` недоступен анонимно (`git ls-remote` просит логин), `add_repo` по условию не вызывался. Dockerfile из него я не видел, поэтому `deploy/railway/telegram.Dockerfile` не писал: вслепую угадывать его состав и команду запуска хуже, чем честно остановиться. Из лога сборки Railway: в Dockerfile сервиса строки 17 и 24 (`RUN --mount=type=secret,id=ca-bundle,required=false`) и строка 37 (`VOLUME`) не поддерживаются Railway. Нужно в том репозитории убрать эти три строки (том — Railway Volume на `/var/lib/telegram`). Файл `TELEGRAM_API_HASH_FILE` из переменной в образе тоже может сделать только он: wrapper `deploy/railway/entrypoint.sh` берёт образ-копию в свой контекст. Предлагаю: либо положить в репозиторий сервиса копию `entrypoint.sh` и использовать её как ENTRYPOINT (имя переменной тогда `GXG_SECRET__TELEGRAM_API_HASH`, а `TELEGRAM_API_HASH` у сервиса переименовать), либо дать Railway токен чтения этого репозитория для Dockerfile в GxG-hub. Переменные `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` владелец уже задал; не задана только `TELEGRAM_CONNECTIONS`.

## Что осталось владельцу

1. **Tailscale.** Ключи `TS_AUTHKEY` у `ui`, `admin`, `contextforge` отвергаются: «tags … invalid or not permitted». Проверить в tailnet: `tagOwners` для `tag:approval-ui`, `tag:approval-admin`, `tag:gateway`; каждый ключ — pre-authorized, с ровно своим тегом (у каждого сервиса свой ключ). После исправления — redeploy трёх сервисов (в Railway или `serviceInstanceDeployV2`).
2. `PLATFORM_ADMIN_EMAIL` (`contextforge`) и `GXG_ADMIN_OPERATORS` (`admin`): в Railway они уже заданы (имена есть, значения не читались). Если пусты, сервисы ждут и после 5 попыток остаются остановленными (`GXG_REQUIRE`, код 78).
3. Вход в Telegram: `gxg-telegram-admin login` в `telegram` (после починки образа), код и 2FA вводит владелец; `TELEGRAM_CONNECTIONS`.
4. Passkey approver'а: `gxg-approval passkeys invite` через `admin` (нужны `admin` и `ui`, то есть п. 1), затем страница «Passkey» в UI.
5. Выбор чатов и humans: добавить в `policies/pilot/tenants/t-pilot.yaml` approver'а (`oidc:<issuer>:<sub>`) и grants на `telegram:tg-pilot/chat/<chat_ref>`, подключить telegram-коннектор в реестр (ключ и https-адрес), собрать `pilot_bundle.py --deploy`.
6. Решить про TLS внутри Railway (AS-07, см. реестр выше) и про `OPENROUTER_API_KEY` (shared, не наша, не тронута).
