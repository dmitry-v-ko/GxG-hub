# Runbook: развёртывание пилота M13 на Railway

Набор: `deploy/railway/` (сервисы, образы, точка входа), `scripts/deploy/railway_deploy.py`. Решения — [ADR-0003](../adr/0003-network-tailscale.md), SEC-13, SEC-28. Публичных доменов Railway и TCP Proxy нет, вход — только через tailnet, Funnel выключен. Все значения секретов остаются в Railway: скрипт печатает только имена переменных.

## Что делает владелец руками

| # | Что | Где |
| --- | --- | --- |
| 1 | Railway-токен (workspace token) в Network secrets сессии, хост `backboard.railway.com` | настройки среды сессии |
| 2 | Подключить GitHub к Railway и дать доступ к `dmitry-v-ko/GxG-hub` и `dmitry-v-ko/gxg-telegram-service` | Railway → Account → GitHub |
| 3 | Tailscale auth key: tagged (`tag:gateway`, `tag:approval-ui`), pre-authorized, ephemeral; записать как `TS_AUTHKEY` в сервисы `contextforge` и `ui` | Railway UI, переменная сервиса. Ни в Git, ни в чат |
| 4 | Не секретные переменные из `owner_env` (список печатает скрипт в конце): `APP_DOMAIN`, `GXG_UI_PUBLIC_URL` (`https://<узел>.<tailnet>.ts.net`), `GXG_OIDC_ISSUER`, `GXG_OIDC_CLIENT_ID`, `GXG_BUNDLE_KEYS`, `GXG_BUNDLE_B64`, переменные `telegram` | Railway UI |
| 5 | Секреты с `gen: owner`: `PLATFORM_ADMIN_EMAIL`, `GXG_SECRET__GXG_OIDC_CLIENT_SECRET`, `GXG_SECRET__GXG_TELEGRAM_TOKEN` (бот уведомлений, не пользовательская сессия), `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Railway UI |
| 6 | Войти в Telegram-сессию: `gxg-telegram-admin login` в сервисе `telegram` (Railway CLI `railway ssh`). Код из Telegram и 2FA вводит владелец. Сессия ложится на volume `/var/lib/telegram` | терминал владельца |
| 7 | Зарегистрировать WebAuthn-ключ approver'а через `decider`: оператор выдаёт приглашение (`gxg-approval passkeys invite`, [approval-ops §3](approval-ops.md)), approver открывает страницу «Passkey» в UI и вводит код | UI из tailnet |
| 8 | Выбрать чаты пилота (≤ 3) и выдать grants: `policies/tenants/<tenant>.yaml`, RID `telegram:<connection>/chat/<chat_ref>`; пересобрать и подписать bundle, записать в `GXG_BUNDLE_B64`. Bundle старше 24 ч отвергается (GR-13): переподписывать не реже чем раз в 12 ч | репозиторий + Railway UI |
| 9 | Применить миграции Signals (`SourceInboxItem`, `ActionCandidate`, `Outcome`, webhook-подписка) | репозиторий Signals |

## Порядок

1. `python scripts/deploy/railway_deploy.py --dry-run`: план без вызовов API, проверка AS-28.
2. `RAILWAY_TOKEN=… python scripts/deploy/railway_deploy.py`: проект `gxg-hub-pilot`, окружение, сервисы, переменные, volumes. Секреты создаются в процессе и сразу уходят в переменные сервиса-владельца. Повторный запуск ничего не меняет и не ротирует секреты.
3. Шаги 3–5 таблицы выше. Повторный запуск скрипта печатает, чего владельцу ещё не хватает.
4. Дождаться `approval-db`, запустить `db-init` (redeploy): роли и пароли по AS-27. Затем `python scripts/deploy/railway_deploy.py --finalize` удаляет `db-init` вместе с его переменными (в нём лежат пароли всех ролей).
5. Redeploy `migrate`, затем `api`, `decider`, `executor`, `sweeper`, `ui`, `notifier`, `mcp`, `telegram`, `contextforge`.
6. Bootstrap ContextForge с устройства оператора (порт `:8443`): team, агенты, токены, gateway `telegram-<tenant>` → `http://telegram.railway.internal:8080/mcp`, virtual server. После этого записать `tenants` и `connectors` в `GXG_CONFIG__PLUGINS_YAML`, правила агентов — в `GXG_CONFIG__UNIFIED_PDP_RULES`, redeploy `contextforge`. Новые tools по умолчанию запрещены.
7. Шаги 6–9 таблицы (Telegram, passkey, чаты и grants, Signals).

## Проверки после развёртывания

| Что | Как |
| --- | --- |
| AC-M14-11 | `python scripts/deploy/railway_deploy.py --verify`: у сервисов нет домена Railway, custom domain и TCP Proxy; внутренние URL — `*.railway.internal`; секретные переменные каждого сервиса равны заявленному набору (AS-28); `db-init` удалён; нет shared-переменных. Код возврата 0 |
| AC-M14-04, порт-скан | с хоста вне tailnet: `nmap -Pn -p- --open <узел>.<tailnet>.ts.net` и `curl -m 5 https://<узел>.<tailnet>.ts.net/health` дают 0 открытых портов и таймаут. Публичных адресов у проекта нет, `--verify` это подтверждает |
| Funnel | в контейнерах `contextforge` и `ui`: `tailscale --socket=/dev/shm/tailscale/tailscaled.sock funnel status` — пусто (`No serve config`). Контейнер не стартует, если Funnel включён |
| AC-M14-10 | с узла `tag:agent`: `GET https://gxg-gateway.<tailnet>.ts.net/admin` → 404, `:8443` не отвечает; с устройства `group:operators` админка на `:8443` открывается |
| AC-M14-09 | запрос из tailnet без токена и с поддельным `Tailscale-User-Login` → 401/403 |

Ошибка скрипта «Railway API …» без подробностей — это ответ API; токен и заголовки не печатаются.
