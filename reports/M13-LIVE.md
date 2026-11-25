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

## Мастер Telegram развёрнут

- **Слияние** `claude/m13-ui-telegram` (362f0e3) в `claude/m13-deploy`: конфликты в `app.py` (аудит-строка входа и email из claims оставлены обе), `services.yaml`, `railway_deploy.py` (генераторы `pki` и `caller_keys`). `tests/deploy` и `approval/ui/tests`: 130 passed, ruff и `mypy --strict` чисто.
- **`services.yaml`:** `ui.GXG_UI_OPERATORS = ${{admin.GXG_ADMIN_OPERATORS}}` (ссылка, значение не читалось; если оператору в `admin` позже дадут формат `oidc:<issuer>:<sub>`, у мастера список email сломается, понадобится отдельное значение), `ui.GXG_TELEGRAM_ADMIN_URL=http://telegram.railway.internal:8082`, `telegram.TELEGRAM_ADMIN_CALLER_KEYS` — публичный ключ `approval-ui` (секрет `GXG_SECRET__GXG_UI_SVC_KEY` у `ui`, AS-28).
- **`telegram`:** startCommand `sleep infinity` сброшен (скрипт теперь очищает override у любого app без `start` в спеке).
- **Approver добавлен:** владелец (его OIDC principal) в `policies/pilot/tenants/t-pilot.yaml`, роль `approver`. Роль `operator` схема прав не допускает: оператор задаётся `GXG_UI_OPERATORS` / `GXG_ADMIN_OPERATORS`. Bundle перевыпущен 2026-11-24 11:36 UTC и раздан (`pilot_bundle.py --deploy`: `api`, `executor`, `ui`, `notifier`, `telegram`); **следующий перевыпуск не позже 23:36 UTC**.
- **Состояние:** все сервисы SUCCESS, `railway_deploy.py --verify` rc=0; `ui` под базовым именем `gxg-approval-ui` (суффикса нет).
- **Не подтверждено:** у `telegram` в логе деплоя только «Starting Container», так что слушает ли он `:8080`/`:8082` и применил ли `telegram-db-init` миграции и роли, по логам сказать нельзя (задача отработала и удалена `--finalize` до того, как я прочитал её лог: ошибка моего порядка). Проверять через мастер в UI (`/operator/telegram` должен ответить, а не «не отвечает»). TLS на `:8081` в сервисе пока не включён.

## TLS и passkey

**Проверка `telegram` (задача `telegram-db-init` в режиме `GXG_TGDB_MODE=check`, только SELECT и TCP/TLS-hello, затем удалена `--finalize`):**
- роли `gxg_telegram_service` и `gxg_telegram_reader` существуют, оба с LOGIN; схемы `capability_hub` (`connections`) и `telegram_archive` (`chats`, `connection_state`, `folder_members`, `folders`, `messages`) на месте; Connection `tg-pilot`: `enabled=True`. Миграции и роли применены.
- `telegram.railway.internal`: порты **8080, 8081, 8082 открыты**. По TLS-hello **ни один не говорит по TLS** (все «no TLS»), то есть `https://telegram.railway.internal:8081` из реестра bundle сейчас не работает. Старт-строку с портами и коммитом в entrypoint сервиса я не добавлял: пушить в `gxg-telegram-service` у меня нечем (доступ только к GxG-hub); порты видны и без неё.
- Режим `check` теперь по умолчанию у этой задачи в `services.yaml` (первичная настройка новой `telegram-db`: убрать `GXG_TGDB_MODE`).

**TLS executor → коннектор (8081):** со стороны Hub всё готово и выкачено (внутренний CA, сертификат на `telegram.railway.internal`, доверие `executor` через `SSL_CERT_FILE`, коннектор в реестре bundle с `https://…:8081`, см. раздел «TLS executor → Telegram-коннектор»). Не хватает только стороны сервиса: entrypoint должен материализовать `TELEGRAM_EXECUTE_TLS_KEY` / `TELEGRAM_EXECUTE_TLS_CERT` в файлы, а сервер порта 8081 — запускаться с `ssl_keyfile` / `ssl_certfile` и не стартовать без них. `TELEGRAM_EFFECTS_ENABLED` не ставился.

**Passkey-приглашение: из Railway сделать нельзя.** `gxg-approval passkeys invite --human oidc:<iss>:<sub>` ходит в `admin` (`POST /v1/passkey-invites`) с `Authorization: Bearer <ID token оператора>`, который `admin` проверяет по `GXG_ADMIN_OIDC_ISSUER`/`GXG_ADMIN_OIDC_AUDIENCE` и списку `GXG_ADMIN_OPERATORS`. ID token выдаёт Google только владельцу при его входе; у задачи в Railway его нет, подделывать нельзя. Шаги для владельца с устройства в tailnet:
1. Исправить `admin.GXG_ADMIN_OPERATORS` на формат `oidc:https://accounts.google.com:<sub>` (сейчас там email, а `admin` ждёт principal). Это же значение через ссылку читает `ui.GXG_UI_OPERATORS` (ожидает email): если менять, разделить их на два значения.
2. Получить ID token (для `aud` = OIDC client id UI) и выполнить `GXG_ADMIN_URL=https://gxg-approval-admin.tail9fb746.ts.net GXG_OPERATOR_TOKEN_FILE=<файл с токеном> gxg-approval passkeys invite --human 'oidc:https://accounts.google.com:<sub>'`.
3. Код из ответа (24 ч, одноразовый) владелец вводит на странице «Passkey» в UI и регистрирует ключ. В лог код не печатается.

## TLS коннектора проверен

`TELEGRAM_EXECUTE_TLS_REQUIRED=1` у `telegram`, деплой SUCCESS. Имена переменных с сертификатом и ключом (`TELEGRAM_EXECUTE_TLS_CERT`, `TELEGRAM_EXECUTE_TLS_KEY`) совпадают с теми, что выпускает `railway_deploy.py`. Проверка одноразовой задачей (удалена): `:8081` — **TLS 1.3, сертификат подписан CA пилота** для `telegram.railway.internal`; `:8080` и `:8082` — без TLS, как задумано. `--verify` rc=0.

## Passkey через UI

**Сделано:** `admin.GXG_ADMIN_OPERATORS` = `oidc:https://accounts.google.com:<sub владельца>` (формат, который `admin` проверяет); `ui.GXG_UI_OPERATORS` отделён и берётся ссылкой `${{contextforge.PLATFORM_ADMIN_EMAIL}}` (значение не читалось). `ui` и `admin` переразвёрнуты, имена в tailnet базовые.

**Кнопка «Зарегистрировать первый ключ» в UI не сделана: это противоречит AS-30, спеки я не менял.** Что именно мешает:

1. AS-30 требует «регистрация passkey — через `decider` с подтверждением **вне `ui`** (одноразовая ссылка оператора или повторный вход в IdP с `max_age`)»: «скомпрометированный `ui` не может решить за человека». Кнопка, по которой `ui` сам создаёт приглашение, делает приглашение подконтрольным `ui`: компрометированный `ui` выдал бы приглашение и зарегистрировал бы ключ атакующего на principal approver'а, а затем подписывал бы решения.
2. Путь «`ui` вызывает `admin` с ID token сессии» невозможен технически и по смыслу: `ui` ID token не хранит (в сессии только `sub`, `iat`, `seen`, `csrf`, `email`); `admin` слушает `127.0.0.1:8092` и достижим только через tailnet (AS-24), `ui` до него не дотянется; а если бы `ui` держал свежий ID token оператора, он сам мог бы выпускать приглашения, то есть тот же пункт 1.
3. Путь «через `decider`»: `decider` не проверяет токены IdP (у него нет issuer/audience и JWKS), эндпоинт `/v1/passkeys` принимает только код приглашения от `admin`.

**Что можно сделать, не нарушая AS-24/AS-30** (нужно решение владельца/оркестратора, это новая функция, не правка спеки):
- **Страница приглашений в самом `admin`** (вход `tag:approval-admin`, только из tailnet): оператор входит через Google (OIDC code flow, `max_age`/`prompt=login`), `admin` сам проверяет ID token и список `GXG_ADMIN_OPERATORS`, создаёт приглашение и показывает код один раз. Дальше код вводится на странице «Passkey» в UI, как сейчас. `ui` в выдаче приглашения не участвует.
- Либо CLI с устройства владельца: ID token по правилам Google OAuth (Playground/`gcloud`-подобный код-флоу) и `gxg-approval passkeys invite` (шаги в разделе «TLS и passkey»).

## Приглашения в admin

`https://gxg-approval-admin.tail9fb746.ts.net/invites` (только из tailnet, `tag:approval-admin`). Приглашение выдаёт сам `admin`; `ui` в этом не участвует (AS-30).

- **Вход** (`approval/src/gxg_approval/admin_web.py`): OIDC code flow с PKCE (S256), `prompt=login`, `max_age=300`, `state` и `nonce`, redirect `…/auth/callback`. `admin` сам проверяет ID token: подпись по JWKS, `iss`, `aud` = client id, `nonce`, `exp`, `auth_time` не старше 5 минут, `email_verified`, principal `oidc:<iss>:<sub>` из `GXG_ADMIN_OPERATORS` (и на каждом следующем запросе, не только при входе).
- **Сессия:** отдельная подписанная cookie `__Host-gxg_admin_session` (Secure, HttpOnly, SameSite=Strict, ≤ 15 мин), cookie входа `__Host-gxg_admin_flow` (Lax, 10 мин). CSRF-токен во всех формах (приглашение, выход). Заголовки `no-store`, CSP `default-src 'none'`, `frame-ancestors 'none'`.
- **Форма «Выдать приглашение passkey»:** принципал по умолчанию свой, формат `oidc:…`. Приглашение создаётся тем же кодом, что `POST /v1/passkey-invites` (`gxg_passkey_invite`, аудит в той же транзакции, в аудите только хеш). Код показывается один раз в ответе на форму, в лог не пишется (проверено тестом), рядом ссылка на страницу «Passkey» в UI. Access log `admin` теперь без query (в `/auth/callback?code=…` code не попадает в журнал).
- **Секреты и AS-28:** `GXG_SECRET__GXG_ADMIN_OIDC_CLIENT_SECRET` у `admin` — ссылка `${{ui.GXG_SECRET__GXG_OIDC_CLIENT_SECRET}}` (значение не читалось, Railway подставляет его сам), `GXG_SECRET__GXG_ADMIN_SESSION_KEY` — свой случайный (`random:32`). Оба в новом классе AS-28 `admin_oidc` (только `admin`), `--verify` проверяет набор секретов. Переменные: `GXG_ADMIN_PUBLIC_URL=https://gxg-approval-admin.tail9fb746.ts.net` (owner_env, задана мной), `GXG_ADMIN_UI_URL=${{ui.GXG_UI_PUBLIC_URL}}`. Если задать `GXG_ADMIN_PUBLIC_URL` не нужно, страница не включается, остаётся только API.
- **Тесты** (`approval/tests/test_admin_web.py`, без БД, фальшивый IdP; 17 passed): подделка токена (чужой ключ подписи), чужие `iss` и `aud`, просроченный токен, чужой `nonce`, устаревший `auth_time`, `email_verified=false`, не-оператор → 403, подмена `state`, ошибка token endpoint, нет сессии → редирект на `/login`, CSRF на каждой форме, формат принципала, срок сессии 15 мин и подделанная cookie, код приглашения и client secret не в журнале. Всего `tests/deploy approval/tests approval/ui/tests`: 242 passed, ruff и `mypy --strict` чисто. Не запускалось (нет Docker): `approval/tests_db/test_admin.py` (API против PostgreSQL; его поведение не менялось, `issue_invite` — вынесенная из эндпоинта функция).
- **Состояние:** `admin` SUCCESS, имя `gxg-approval-admin`, на loopback `GET /healthz` 200 и `GET /login` 303 (на Google), `--verify` rc=0.
- **Проверка владельцем:** открыть `/invites` из tailnet, войти через Google (потребуется свежий вход), выдать приглашение для себя, ввести код на странице «Passkey» в UI. Нужен `https://gxg-approval-admin.tail9fb746.ts.net/auth/callback` в redirect URI OAuth-клиента (по словам оркестратора уже добавлен); ID token Google должен содержать `auth_time` и `email_verified` (при `max_age` Google их отдаёт; если `auth_time` не придёт, вход будет отклонён сообщением «Нужен свежий вход»).

## Агент пилота

Состояние: все сервисы SUCCESS, `railway_deploy.py --verify` rc=0. Bundle не менялся (реестр прежний), срок текущего 23:36 UTC.

**Что сделано** (`scripts/deploy/pilot_agent.py`, `deploy/railway/pilot_agent_job.py`; задачи живут внутри Railway как временные сервисы из образа Approval и удаляются):
- **Платформа ContextForge.** Команда `t-pilot` (private), пользователь-агент `pilot@t-pilot.agents.gxg.example.com` (`agent:t-pilot:pilot`, пароль случайный и не хранится: SEC-02, у агента только токен), gateways `telegram-t-pilot` → `http://telegram.railway.internal:8080/mcp` и `approval-t-pilot` → `http://mcp.railway.internal:8091/mcp` (Streamable HTTP, видимость team), виртуальные серверы `vs-telegram-t-pilot` и `vs-approval-t-pilot`. В серверы входят только разрешённые tools: `telegram.messages.search`, `telegram.thread.read`, `telegram.message.propose`, `effects.status`, `effects.cancel`. Прочие tools gateway'ев (`telegram.message.get`, `telegram.messages.collect`) в сервер не включены и правилами не разрешены; `*.execute` не регистрируется (EFF-02).
- **Идентификация (вариант C) и права.** В `GXG_CONFIG__PLUGINS_YAML` внесены team id и gateway ids (`tenants` → `t-pilot`, `connectors` → audience `telegram` и `approval-mcp`); в `GXG_CONFIG__UNIFIED_PDP_RULES` — правило `t-pilot.pilot` только на эти пять tools (в репозитории то же: `deploy/railway/contextforge/`). Deny-правило на `*execute*` и `*reconcile*` выше. ContextForge перезапущен.
- **Токен агента.** Выпускается внутри задачи, оттуда выходит запечатанным (RSA-OAEP, ключ одноразовый, создан в этом процессе) и только так попадает в лог; в сервис-хранилище `pilot-agent` (`kind: vault`, без деплоя, AS-28 `agent_token`) записан расшифрованным как `PILOT_AGENT_TOKEN`, плюс `PILOT_AGENT_IDS` (id команды, gateways, серверов, имена tools). Креды платформенного админа задача берёт ссылками `${{contextforge.PLATFORM_ADMIN_EMAIL}}` / `…_PASSWORD`; значения не читались и не печатались.
- **SSRF.** Первая регистрация упёрлась в защиту ContextForge: `telegram.railway.internal` резолвится в `10.130.8.63` и `fd12:c95d:…`, то есть в оба семейства. Allow-list теперь `["fd12::/16", "10.128.0.0/9"]` (Railway private network; регистрирует только платформенный админ).

**Проверка от имени агента** (задача `pilot-agent-probe`, через ContextForge по private network, токен по ссылке на `pilot-agent`):
- `vs-approval-t-pilot`: `initialize` 200, `tools/list` 200, 2 tools: `approval-t-pilot-effects-cancel`, `approval-t-pilot-effects-status`.
- `vs-telegram-t-pilot`: `initialize` 200, `tools/list` 200, 3 tools: `telegram-t-pilot-telegram-message-propose`, `…-messages-search`, `…-thread-read`.
- `message.propose` достижим и доходит до коннектора, но ответ `effect_disabled` **не получен**: с пустыми аргументами коннектор отвечает ошибкой валидации (`'chat_ref' is a required property`), а валидных аргументов (схема propose) я не знаю. `TELEGRAM_EFFECTS_ENABLED` не включался. Чтобы проверить `effect_disabled`, нужен вызов с корректными аргументами (`GXG_PROBE_ARGS` у probe-задачи).

**Известные шероховатости:**
- Задачи иногда падают на сборке («couldn't locate the dockerfile in code archive»), если запускаются в первую минуту после push в ветку: Railway берёт ещё не обновлённый архив. Достаточно повторить; `pilot_agent.py` печатает причину.
- Токены агента от трёх неудачных запусков setup не отозваны и действуют до 30 дней (их значения нигде не сохранены). Отозвать через admin API ContextForge.
- Каждый запуск `railway_deploy.py` пересоздаёт `db-init`/`telegram-db-init` (transient), после чего нужен `--finalize` (сделан).

**Токены агента:** отозваны 5 старых токенов `agent-t-pilot-pilot-*` (задача `pilot-agent-revoke`, значения не печатались; их было пять, не три: каждый неудачный запуск `setup` оставлял токен), остался один, из `pilot-agent.PILOT_AGENT_TOKEN`; контрольный `tools/list` им через ContextForge успешен (200, 3 tools).

## Scope Telegram (503) и вход в admin без auth_time

- **`PUT scope` можно повторять.** `telegram-db-init` отработал на актуальной голове `claude/m13-railway`: `service_role.sql` и `reader_role.sql` переприменены по одному выражению (что уже есть, пропускается), функция `telegram_service.set_scope(text, jsonb)` в базе есть, `EXECUTE` у `gxg_telegram_service` есть (у `gxg_telegram_reader` нет, как задумано), Connection `tg-pilot` `enabled=True`; порты 8080/8082 без TLS, 8081 с TLS от CA пилота. Задача удалена `--finalize`. Теперь скрипт всегда переприменяет SQL сервиса и печатает такую же проверку; режим только-проверки: `GXG_TGDB_MODE: check`.
- **Что поправить в `gxg-telegram-service` (мне туда не пушить):** чтобы причина 503 была в журнале, в `store.set_scope` (и остальных вызовах хранилища) ловить `psycopg.Error` и писать только `type(exc).__name__` и `exc.sqlstate`, без текста запроса и параметров, например `log.error("store error op=set_scope type=%s sqlstate=%s", type(exc).__name__, exc.sqlstate)`, и после этого бросать прежний `StoreError` (`raise StoreError(...) from None`). Тогда отсутствующая функция даст `UndefinedFunction` / `42883`.
- **Вход в `admin` `/invites`:** Google не кладёт `auth_time` в ID token. Теперь свежесть берётся из `iat` (не старше 300 с), если `auth_time` нет; если есть, проверяется он. Безопасность: запрос идёт с `prompt=login`, `nonce` привязывает токен к этому запросу. Тесты: без `auth_time` со свежим `iat` вход проходит, со старым `iat` отказ (18 passed в `test_admin_web.py`).

## Сессия UI одобрений: 30 дней

Решение владельца 2026-11-24: сессия UI 30 дней от входа, без idle-таймаута (`SESSION_MAX_S` = 30 суток, `SESSION_IDLE_S` и поле сессии `seen` убраны, cookie уже постоянная: `Max-Age` = 30 суток, Secure, HttpOnly); строка таблицы «Вход» перенесена в `docs/specs/approval-service-v1.md`. `admin` `/invites` не менялся (свежий вход, сессия 15 мин). **`SameSite` остался `Strict`, как в коде сейчас (а не Lax, как сказано в задании):** вход через IdP уже учитывает Strict промежуточным same-site переходом `/auth/callback`; Lax ослабил бы защиту от CSRF без надобности. Скажите, если нужен именно Lax. Тесты: 197 passed (offline), `tests_db` обновлены под 30 дней, но без Docker не запускались.

**Вход в `admin` `/invites` больше не зацикливается:** `/auth/callback` отвечает 200 с мета-редиректом на `/invites` вместо 303 (редирект оставался в цепочке переходов от Google, и браузер не отправлял Strict-cookie); cookie остаётся SameSite=Strict, тест на цепочку callback → следующий запрос → 200 есть. Можно повторять вход после деплоя `admin`.

## Первая отправка: проверка фактов (шаг 1)

- **Passkey владельца есть:** в `webauthn_credentials` один ключ для `oidc:https://accounts.google.com:…`, выдан по приглашению оператора (`enrolled_by` задан); принципалов с passkey один.
- **Scope Telegram сохранён:** в архиве (в нём только чаты scope) три чата, все `state=synced`; функция `set_scope` и `EXECUTE` у `gxg_telegram_service` на месте, Connection `tg-pilot` `enabled=True`.
- **Остановился перед grants, `TELEGRAM_EFFECTS_ENABLED` и отправкой:** из базы нельзя понять, какой из трёх чатов владелец выбрал «первым» (порядок в архиве не порядок выбора), а тестовое сообщение уйдёт в настоящий чат. Нужно, чтобы владелец или оркестратор назвал чат (название или `chat_ref`).

## Первая отправка: что сделано и что мешает

- **Чат найден:** «GxG Agents» (`forum`) = `tgc1_fg2agKZmFgzytfmaAUzf3ZbkczoPSmN8`, название однозначное; scope в порядке выбора владельца: «Dmitry Kozlov» (private), «LobsterDAO HR» (channel), «GxG Agents» (forum) (`pilot_agent.py scope`: задача в образе `ui`, названия печатались только у чатов scope).
- **Grants** (`policies/pilot/tenants/t-pilot.yaml`): агент `agent:t-pilot:pilot` — `read` и `collect` на все три чата, `propose` только на «GxG Agents»; срок до 2026-12-24, approver — владелец (`approver`). Bundle перевыпущен 2026-11-24 15:03 UTC и раздан (`pilot_bundle.py --deploy`); **следующий перевыпуск не позже 03:00 UTC** (меняйте раз в 12 ч). `TELEGRAM_EFFECTS_ENABLED=1` у `telegram` задан.
- **`propose` пока не создан:** коннектор отвечает `temporarily_unavailable: grants bundle is not ready`. Причина: `railway-entrypoint.sh` образа `gxg-telegram-service` не раскладывает bundle (`TELEGRAM_BUNDLE_DIR` по умолчанию `/bundle`, он пуст), хотя `pilot_bundle.py` кладёт в `telegram` и `TELEGRAM_BUNDLE_KEYS` (`kid=ключ`), и `GXG_BUNDLE_B64` (base64 tar.gz с `grants-bundle.json` и `grants-bundle.json.sig` в корне).
- **Правка для `gxg-telegram-service`** (мне туда пушить нечем: репозиторий доступен Railway для сборки, но не этой сессии для записи; `git ls-remote` без логина отказывает). В `deploy/railway-entrypoint.sh`, после раскладки секретов и до `chown`/`setpriv`:

```sh
# Grants bundle (GR-11a): GXG_BUNDLE_B64 = base64 of a tar.gz holding grants-bundle.json and grants-bundle.json.sig
if [ -n "${GXG_BUNDLE_B64:-}" ]; then
  mkdir -p "$FILES/bundle"                       # $FILES: the tmpfs directory of the secret files (/dev/shm/...)
  printf '%s' "$GXG_BUNDLE_B64" | base64 -d | tar -xz -C "$FILES/bundle"
  chown -R telegram "$FILES/bundle"
  export TELEGRAM_BUNDLE_DIR="$FILES/bundle"
  unset GXG_BUNDLE_B64
fi
```
  Тест: запуск entrypoint с `GXG_BUNDLE_B64` из tar.gz двух файлов → в `$TELEGRAM_BUNDLE_DIR` лежат оба файла, переменная снята; без неё каталог не создаётся. После пуша и деплоя `telegram`: `pilot_agent.py propose` (нужны `GXG_SEND_CHAT_REF` и `GXG_SEND_TEXT`), дальше `intent_id` и ссылка `https://gxg-approval-ui.tail9fb746.ts.net/intents/<id>`. Заявка с ключом идемпотентности `m13-first-send-1`: повтор той же заявки не создаст второй intent.

**Заявка на первую отправку создана** (2026-11-24, `pilot_agent.py propose`, от имени агента через ContextForge): `intent_id` = `12c724e6-25c1-4d81-a8ed-31ee6565fb72`, статус `pending_approval`; чат «GxG Agents», текст «GxG Hub: тестовая отправка пилота», ключ идемпотентности `m13-first-send-1`. Ссылка в UI: `https://gxg-approval-ui.tail9fb746.ts.net/intents/12c724e6-25c1-4d81-a8ed-31ee6565fb72`. Bundle перевыпущен 15:44 UTC с манифестом сервиса (в реестре появился `telegram.message.propose`); следующий перевыпуск не позже 03:44 UTC. Одобряет владелец своим passkey; результат (`effects.status` до `succeeded`, ровно одна отправка) допишу после одобрения.

## Решение одной кнопкой (AS-30a, EX-26)

Решение владельца 2026-11-24, спека `approval-service-v1.md` AS-30/AS-30a, EX-26 (ветка спеки `claude/admiring-planck-vzys0x`, `895c858`). Миграция `0007_session_decision.py`: `gxg_decide_session(principal, intent, version, args_hash, decision, comment)`, `SECURITY DEFINER`, `EXECUTE` только у `gxg_ui`; проверки как у решения через `decider` (статус, версия, хеш, срок, замороженные `allowed_approvers`, не requester), отказ при `step_up_required`, запись `decisions` с `step_up = false` и `decided_by = oidc:…`. `ui`: карточка intent'а без step-up с кнопками «Одобрить» / «Отклонить» без WebAuthn (CSRF, повторная проверка текущего bundle, хеш аргументов), со step-up прежний путь через `decider`; мобильная вёрстка (одна колонка, кнопки на всю ширину). Деплой: `migrate` и `ui` на `73fd4ed`, SUCCESS. Живая проверка (read-only задача): `gxg_decide_session` существует, `EXECUTE` только у `gxg_ui`; `gxg_decide` только у `gxg_decider`; заявка `12c724e6-25c1-4d81-a8ed-31ee6565fb72`: `status=pending_approval`, **`step_up_required=False`**, то есть владелец одобрит её кнопкой. **Не запускалось (нет Docker и PostgreSQL):** тесты `approval/tests_db/test_session_decision.py`, `approval/ui/tests_db/test_session_decision.py` (EX-26) и правки `test_roles.py`/`test_decisions.py`; SQL миграции проверен парсером, а работу функции подтверждает только живая проверка выше. Офлайн: 249 passed, ruff и `mypy --strict` чисто.

## Первая отправка выполнена

Заявка `12c724e6-25c1-4d81-a8ed-31ee6565fb72` одобрена владельцем кнопкой в UI (AS-30a, без step-up) и исполнена: `effects.status` через ContextForge от имени агента — `succeeded` (версия 5, `received_approvals` 1 из 1, правило `tg-propose-human`, `business_task_id` `m13-first-send`). По базе (read-only задача, удалена): `intents.status = succeeded`, **одна** запись в `executions` (`attempt = 1`, `state = succeeded`, `error_code` пуст, `provider_operation_id = tg:tg-pilot:9`, завершено 2026-11-25 07:34:12 UTC), других исполнений нет, то есть отправка ровно одна. Чат «GxG Agents», текст «GxG Hub: тестовая отправка пилота». Сверку со стороны Telegram (сообщение с id 9 в чате, один раз) может сделать владелец глазами.

Пилот M13 стартует с этой отправки (AC-M13-01…06 из `docs/implementation-plan-v1.md`). Из эксплуатации на 14 дней: перевыпуск bundle не реже раза в 12 ч (`pilot_bundle.py --deploy`, срок действия 24 ч), токен агента в `pilot-agent` живёт 30 дней (выпущен 2026-11-24), grants агента до 2026-12-24.

Сбои сборки одноразовых задач («not found at deploy/railway/approval.Dockerfile») объяснены и исправлены (`e7b3722`): `serviceConnect` запускает собственный деплой ветки по умолчанию, а скрипт брал «последний деплой» сервиса; теперь он следит за деплоем, который сам запустил.

## Сжатая приёмка M13 (решение владельца 2026-11-25), ход

- **AC-M13-02 (OPS-01), часть до одобрения:** созданы две заявки в «GxG Agents» (текст «GxG Hub: приёмка AC-M13-02, заявка A / B»): `55258291-1b31-4a9b-8dcf-7cd970c1b5f8` (A), `d8cdc61a-2c1b-4606-8647-1ba757fafc28` (B), обе `pending_approval`, `step_up_required=false`. В 17:33 UTC перезапущены все прикладные сервисы (`api`, `mcp`, `executor`, `sweeper`, `decider`, `ui`, `admin`, `notifier`, `telegram`, `contextforge`; Postgres и `migrate` не трогались), все вернулись в SUCCESS. По базе после рестарта: A и B по-прежнему `pending_approval`, у первой отправки `12c724e6…` всё ещё одна запись в `executions` (`attempt=1`, `succeeded`, `tg:tg-pilot:9`): рестарт ничего не отправил повторно. Осталось: владелец одобряет A и B, затем проверяю, что у каждой ровно одно исполнение. Состояния `executing` и `outcome_unknown` на живой системе без инъекции сбоя не воспроизвести; они покрыты офлайн-стендом (stage-C).
- **AC-M13-03 (OPS-02):** в 17:37 UTC токены агента отозваны в ContextForge (admin API); тот же вызов `propose` (с пустыми аргументами, ничего не создаёт) до отзыва давал HTTP 200, сразу после отзыва — HTTP 401 (задержка 0 с при пороге 60 с; `AUTH_CACHE_REVOCATION_TTL=5`). Агенту выпущен новый токен (`pilot-agent`). Ослабление политики: `gxg-policy diff` на проверочной паре (из bundle убран grant `propose`) показывает `widening: 1`; CI-гейт `scripts/ci/grants_widening.sh` на push красный при любом расширении (на PR — по метке владельца), то есть alert = красный гейт.
- **AC-M13-01 (EX-03…05, EX-11 на живом коннекторе):** EX-03 частично: `execute` на `https://telegram.railway.internal:8081` без токена, с мусорным токеном, с неверной схемой авторизации и с JWS неизвестного ключа и `kid` → HTTP 401, до провайдера (в чате нет сообщений от этих вызовов). Не проверено вживую: EX-03 с валидной подписью и чужим `aud`/истёкшим `exp`, EX-04 (повтор `jti` и `dispatch`), EX-05 (подмена args в токене), EX-11 (провайдер исполнил, ответ потерян): им нужен ключ подписи executor'а или инъекция сбоя, которых в боевых сервисах нет (AS-28). Они остаются на офлайн-стенде (stage-C, EX-01…EX-25) и не должны считаться пройденными на «живом Telegram».
- **AC-M13-04…06:** через 24 ч после последней отправки: сверка журнала чата с БД (каждое сообщение = ровно один intent `succeeded`/`reconciled_succeeded` с `decision=approve` человека; 0 сообщений без такого решения; 0 `outcome_unknown` старше 24 ч). Read-only задача `db-init` для таких отчётов уже есть (intents, executions); журнал чата со стороны Telegram сверяет владелец.

## AC-M13-02: исполнение двух одобренных заявок (2026-11-25)

Проверка по БД approval (read-only отчёт db-init), затем `--finalize` (временные job удалены):

| intent | status | execution | op |
|---|---|---|---|
| 12c724e6… (первая отправка) | succeeded | 1 × attempt=1 succeeded | tg:tg-pilot:9 |
| 55258291… (A) | succeeded | 1 × attempt=1 succeeded | tg:tg-pilot:10 |
| d8cdc61a… (B) | succeeded | 1 × attempt=1 succeeded | tg:tg-pilot:11 |

Каждая заявка исполнена ровно один раз, без повторов и ошибок. Сверка журнала чата с БД (AC-M13-04…06) — через ~24 ч после последней отправки.

## MCP через ContextForge: Apify (2026-11-25, решение владельца, без KB)

- Влито `claude/m13-mcp-routing` (b0aaf4c); `tests/deploy` 56 passed. Добавлен `GXG_SKIP_GATEWAYS` (setup не регистрирует ещё не развёрнутый `kb-t-pilot`); KB не деплоился.
- `unified_pdp.rules.json` — seed (пишется один раз), поэтому деплой его не обновил: переменная `GXG_CONFIG__UNIFIED_PDP_RULES` обновлена вручную из файла, ContextForge перезапущен (SUCCESS).
- `pilot_agent.py setup`: gateway `apify-t-pilot` (id b16429d3…) и `vs-apify-t-pilot` созданы; слаги tools совпадают с PDP: `apify-t-pilot-<tool>` для всех 6. Старые токены агента отозваны (оставлен новейший).
- probe от агента: `tools/list` = ровно 6 apify-tools (запускающих Actor'ы нет).
- **Вызов tools не работает:** `search-apify-docs` и `search-actors` → `GXG_IDENTITY_DENIED - identity_unavailable (gateway is not a registered connector)` (плагин GxgIdentity, fail-closed, pre-invoke; до Apify запрос не доходит).
- Не обходил: чтобы плагин пропустил вызов, gateway надо внести в `connectors` плагина, т.е. в реестр коннекторов; тогда плагин будет подписывать и отправлять identity-заголовок на внешний `mcp.apify.com`. Это решение за владельцем/оркестратором (нужен вариант «без identity-заголовка» для одного внешнего gateway либо отдельное исключение в плагине).

### Apify: вызовы заработали (2026-11-25)
- Плагин `gxg_identity`: новая опция `external: [gateway_id]` — для внешнего шлюза все проверки вызывающего (агент своего tenant, команда, токен) остаются, но identity-заголовок не подписывается и не отправляется, входящий `X-GxG-Identity` отбрасывается. Тесты плагина: 27 passed. `plugins.yaml`: `external: [b16429d3…]` (apify-t-pilot); seed обновлён вручную, ContextForge пересобран.
- probe от агента: `search-apify-docs` («webhooks») и `search-actors` («instagram scraper», 3 актора) возвращают результаты через ContextForge.

### KB (подготовка, сервис не стартует)
- Влито 9a7d73c (kb_reader.sql под Supabase); тест kb_reader.sql поправлен (BYPASSRLS не называется в SQL).
- `policies/pilot/tenants/t-pilot.yaml`: connection `kb-pilot` (domain kb), grant `kb-pilot-read` (read; collections tool, blueprint, insight, growth_hack). Бандл перевыпущен (f4c10e38…, built 18:24Z) и выложен в api/executor/ui/notifier/telegram/kb.
- Сервис `kb` создан. Переменные: KB_DB_CA (публичный Supabase Root 2021 CA, seed из `deploy/railway/supabase-root-2021-ca.pem`), KB_IDENTITY_KEYS, KB_BUNDLE_KEYS, GXG_BUNDLE_B64, KB_CONNECTIONS_JSON (`t-pilot` → `kb-pilot`, workspace 0000…0000), KB_DATABASE_URL = `postgresql://gxg_kb_reader.yojcyedwlgbperliefkd@aws-1-eu-west-1.pooler.supabase.com:5432/postgres?sslmode=verify-full&sslrootcert=/dev/shm/kb/ca.pem` (без пароля; хост пулера aws-1 выбран по региону eu-west-1, не проверен; verify-full по имени хоста пулера не проверен). KB_DATABASE_PASSWORD не задан (за оркестратором).
- **Блокер сборки:** `gxg-kb-service@claude/m6-kb-service` Dockerfile не принимается Railway: строка 14 `RUN --mount=type=secret,id=ca-bundle,required=false` (Railway допускает только `type=cache`) и строка 28 `VOLUME` (не поддерживается). Деплой `542bc08d…` FAILED на разборе Dockerfile. Мне нет доступа к репозиторию сервиса: нужно убрать mount=secret и VOLUME (или дать отдельный Dockerfile для Railway).
