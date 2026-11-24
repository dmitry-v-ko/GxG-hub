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

## TLS executor → Telegram-коннектор (AS-07)

Состояние: все сервисы SUCCESS, `railway_deploy.py --verify` rc=0 (после `--finalize` обоих transient-задач).

- **Внутренний CA** (`railway_deploy.py`, раздел `pki` в `services.yaml`): ECDSA P-256, CA на 1 год с NameConstraints `railway.internal` (CA может поручиться только за внутренние имена), серверный сертификат на 1 год, SAN `telegram.railway.internal`, EKU serverAuth. Добавлены SKI/AKI: Python 3.13 проверяет строго и без них отвергает цепочку (поймано тестом). Ключ CA живёт только внутри вызова и не хранится; перевыпуск создаёт новый CA и заменяет всё множество. Частично заданный PKI скрипт отвергает, а не достраивает.
- **Куда легло:** `telegram.TELEGRAM_EXECUTE_TLS_KEY` (секрет), `telegram.TELEGRAM_EXECUTE_TLS_CERT` (сертификат), `executor.GXG_CONFIG__SSL_CERT` (сертификат CA). Entrypoint делает из последней файл и экспортирует `SSL_CERT_FILE`: executor создаёт `httpx.Client(base_url=…)` с проверкой по умолчанию, а httpx берёт `SSL_CERT_FILE` (проверено: в контексте ровно 1 доверенный CA). Кода Approval менять не пришлось. Значения нигде не печатаются.
- **Реестр bundle:** добавлен `telegram` (`connector`, ключ из `TELEGRAM_SVC_PUBLIC_KEY`, `tenants: [t-pilot]`, `internal_url: https://telegram.railway.internal:8081`); `pilot_bundle.py --check` зелёный. Bundle перевыпущен 2026-11-24 11:00 UTC и раздан (`--deploy`): **следующий перевыпуск не позже 23:00 UTC**. Отступление от AS-07 (http) больше не нужно.
- **Тесты** (`tests/deploy/test_pki.py`, офлайн, 39 passed всего): цепочка и SAN; настоящий TLS-handshake в памяти — проходит для `telegram.railway.internal`, падает для другого имени и для чужого CA; повторный запуск ничего не меняет; ключ не попадает в stdout/stderr; частично заданный PKI отвергается.
- **Побочное:** каждый запуск `railway_deploy.py` пересоздаёт transient-задачи `db-init` и `telegram-db-init` (они отработали идемпотентно с теми же паролями), после чего я запускал `--finalize`.

### Не сделано: TLS внутри `gxg-telegram-service`

В репозиторий сервиса пушить нельзя (доступ только к GxG-hub, `add_repo` запрещён), исходников сервера я не видел. Что нужно внести там (контракт, имена переменных те, что уже лежат в Railway):

1. `deploy/railway-entrypoint.sh`: добавить `TELEGRAM_EXECUTE_TLS_KEY` и `TELEGRAM_EXECUTE_TLS_CERT` к материализуемым переменным (файлы 0600 в `/dev/shm`, экспорт `TELEGRAM_EXECUTE_TLS_KEY_FILE` / `TELEGRAM_EXECUTE_TLS_CERT_FILE`, исходные переменные снять, как для `TELEGRAM_SVC_KEY`).
2. Сервер порта 8081 (execute): запускать uvicorn с `ssl_keyfile` и `ssl_certfile` из этих файлов; без файлов execute на plaintext не поднимать (отказ на старте). Порт 8080 (MCP за ContextForge) не менять.
3. После этого вывести `telegram` из режима обслуживания (startCommand я не трогал, как просили). До этого `https://telegram.railway.internal:8081` ничего не слушает, но `execute` в пилоте и не вызывается: нет grants и humans.

## UI пустой

**Причина: коллизия имён в tailnet, а не приложение.** `ui` и `admin` регистрируются в tailnet не под `gxg-approval-ui` / `gxg-approval-admin`, а под `gxg-approval-ui-7` / `gxg-approval-admin-6`: базовые имена заняты прежними узлами (каждый деплой и каждая попытка запуска оставляли узел, Tailscale добавляет суффикс `-2`, `-3`, …). Владелец открывает `https://gxg-approval-ui.tail9fb746.ts.net/`, то есть адрес старого узла: до контейнера запрос не доходит, поэтому в логах `ui` ни одной строки и страница пуста. Приложение, прокси и сертификат в порядке (по самопроверке из контейнера):

| Проверка (лог `selfcheck:`) | Результат |
| --- | --- |
| `tailscale serve status` | `https://gxg-approval-ui-7.tail9fb746.ts.net (tailnet only)`, `/` → `http://127.0.0.1:8443`, Funnel нет |
| `CertDomains` | заполнен, HTTPS-сертификаты в tailnet включены |
| `ui` `GET /healthz` и `/login` на loopback | 200 и 302 (редирект на IdP) |
| `admin` `GET /healthz` | 200 |
| запрос к собственному tailnet-имени через SOCKS5 | `cannot complete SOCKS5 connection`: узел не может позвонить сам себе в userspace-режиме, проверка неинформативна (оставлена в логе, на вывод не влияет) |

**Что сделано** (коммиты «tailnet self-check…», «tailnet self-check uses the real node name…»):
- `entrypoint.sh` для сервисов с `TS_HOSTNAME`: в лог `tailscale status`, `serve status`, имя tailnet и `CertDomains` из `status --json`, запрошенное и фактическое имя узла с предупреждением `WARNING the node did not get the requested name`, через 15 с коды ответа приложения и tailnet-проверка. Только имена и коды, без значений. SOCKS5 `localhost:1055` включён на loopback (отключается `GXG_TS_SELFCHECK=0`). В образы `approval` и `ui` добавлен `curl`; тесты офлайн.
- `overlapSeconds=0` для `ui`, `admin`, `contextforge` (старый контейнер уходит до старта нового) и то же в `railway_deploy.py` для любого сервиса с `TS_HOSTNAME`.
- Гостевая страница `/` у `ui`: заголовок, пояснение и кнопка «Войти через Google» (`/login`). `/login` у `ui` отдаёт 302: вход работает. Access log uvicorn уже включён, query вырезается (`NoQuery` в `cli.py`); новых строк не было, потому что запросы не доходили.
- `contextforge`: приложение запущено (gunicorn, плагины загружены); строки самопроверки в обрезанном логе не просмотрены.

**Что нужно владельцу** (нужен доступ к Tailscale admin, у меня его нет):
1. В Machines удалить старые `gxg-approval-ui*`, `gxg-approval-admin*`, `gxg-gateway*`. Тогда после redeploy узлы получат базовые имена, под которыми их ждёт `GXG_UI_PUBLIC_URL`.
2. Ключи `TS_AUTHKEY` делать **ephemeral** (по runbook), иначе устаревшие узлы копятся: похоже, ключи сейчас не ephemeral.
3. До этого страница открывается по фактическому имени (`https://gxg-approval-ui-7.tail9fb746.ts.net/`), но редирект OIDC и WebAuthn привязаны к `GXG_UI_PUBLIC_URL`, поэтому вход по такому адресу не завершится.

### UI пустой: состояние узла tailscale на volume

Коммиты «tailscale node state on the service volume…» и «entrypoint drops privileges with python…».

- `GXG_TS_STATE=/var/lib/gxg/tailscale` у `ui`, `admin`, `contextforge` (у `admin` и `contextforge` добавлен volume `/var/lib/gxg`): `tailscaled` пишет состояние в `tailscaled.state` (каталог 0700, владелец — пользователь процесса). Повторный старт: `tailscale up` без ключа («rejoined the tailnet with the saved node state»); если control plane узел забыл, откат на `TS_AUTHKEY`. Без `GXG_TS_STATE` состояние остаётся в памяти. Тесты офлайн: первый старт с ключом, второй без ключа, откат на ключ, режим `mem:`.
- ContextForge стартует от root только чтобы взять volume и сразу переходит на пользователя 10001 (`GXG_RUN_AS`); в его образе нет `setpriv`, поэтому сброс прав делает python (проверен вживую на uid 65534).
- **Результат по selfcheck:** `gxg-gateway` получил базовое имя `gxg-gateway.tail9fb746.ts.net`. `ui` и `admin` стабильно живут как `gxg-approval-ui-1` и `gxg-approval-admin-1` и после рестартов остаются под тем же именем («rejoined»), `healthz` 200, `/login` у `ui` 302. Базовые имена `gxg-approval-ui` и `gxg-approval-admin` занимают старые узлы.
- **Осталось** (нужен Tailscale API или владелец): удалить из tailnet **все** машины `gxg-approval-ui*` и `gxg-approval-admin*`, включая текущие `-1`. Контейнеры тогда не найдут свой узел в control plane, откатятся на `TS_AUTHKEY` и зарегистрируются под базовыми именами (после этого redeploy `ui` и `admin`; сработает при этом только reusable/non-ephemeral ключ, если ключ одноразовый, понадобится новый). **Ключи делать не ephemeral:** узел с сохранённым состоянием не должен исчезать при оффлайне.

**Итог по именам (2026-11-24, после удаления старых узлов оркестратором и редеплоя по одному):** selfcheck `ui`, `admin`, `contextforge` — запрошенное имя равно фактическому: `gxg-approval-ui`, `gxg-approval-admin`, `gxg-gateway` на `tail9fb746.ts.net`; все три SUCCESS. Узлы сохраняются на volume. Пункт «Осталось» выше про удаление старых машин закрыт.
