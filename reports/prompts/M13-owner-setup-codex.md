# M13: настройка пилота через Chrome (промпт для Codex, computer use)

Ты работаешь в Chrome владельца через computer use. Все сайты открыты под его аккаунтами. Задача — подготовить пилот GxG Hub: дать Railway доступ к GitHub и разложить ключи по сервисам Railway.

**Правила:**
- Значение ключа или токена не печатай в чат, в отчёт и в файлы. Копируй его кнопкой Copy и сразу вставляй в нужное поле Railway.
- Ничего не удаляй и не меняй в других проектах Railway, в других репозиториях и в других OAuth-клиентах.
- Код, пароль или 2FA спрашивает владелец — он и вводит.
- Если шаг не получается, остановись и опиши, что видишь на экране.

Railway-проект: `gxg-hub-pilot`, окружение `production`. Сервисы уже созданы. Переменная задаётся так: проект → сервис → вкладка **Variables** → **New Variable** → имя и значение → **Add**. **Shared Variables не использовать.** После всех переменных нажми **Deploy** (или **Apply changes**), если Railway предложит.

## 1. Railway ↔ GitHub

1. Открой https://github.com/settings/installations, найди **Railway** → **Configure**.
2. В **Repository access** добавь `dmitry-v-ko/GxG-hub` и `dmitry-v-ko/gxg-telegram-service` (или включи «All repositories», если владелец согласен). **Save**.

## 2. Tailscale

1. https://login.tailscale.com/admin/acls/file. Убедись, что в `tagOwners` есть `tag:gateway`, `tag:approval-ui`, `tag:approval-admin`, `tag:agent`, у каждого `["autogroup:admin"]`. Если нет, допиши и сохрани. Больше в ACL ничего не меняй.
2. https://login.tailscale.com/admin/dns. Включи **MagicDNS** и **HTTPS Certificates**. Запиши **Tailnet name** вида `xxxx.ts.net`, это не секрет.
3. https://login.tailscale.com/admin/settings/keys → **Generate auth key**.
   - Reusable — вкл, Ephemeral — выкл, Pre-approved — вкл.
   - Tags: `tag:gateway`, `tag:approval-ui`, `tag:approval-admin`.
   - Expiration: 90 дней.
   - **Generate**, затем **Copy**.
4. Вставь этот ключ в Railway как `TS_AUTHKEY` у трёх сервисов: `contextforge`, `ui`, `admin`.
5. Funnel не включай.

## 3. Telegram API

1. В Railway открой проект **GxG Mastra** → сервис **gxg_agents_v2** → **Variables**. Найди `TG_API_ID` и `TG_API_HASH`, скопируй значение каждой (иконка копирования у переменной).
   Если их там нет: https://my.telegram.org → **API development tools**, вход под номером владельца, код вводит владелец.
2. В `gxg-hub-pilot` → сервис `telegram` задай `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`.

## 4. Бот уведомлений

1. Открой https://web.telegram.org, затем чат с **@BotFather**.
2. Отправь `/newbot`. Имя — `GxG Approvals`, username — свободный, на `_bot` (например `gxg_approvals_<4 цифры>_bot`).
3. Скопируй токен из ответа.
4. В `gxg-hub-pilot` → сервис `notifier` задай `GXG_SECRET__GXG_TELEGRAM_TOKEN`.
5. Открой чат с новым ботом и нажми **Start**.

## 5. Вход в Approval UI (Google OAuth)

1. https://console.cloud.google.com, выбери проект владельца или создай новый «gxg-hub».
2. **APIs & Services → OAuth consent screen**:
   - тип External;
   - App name `GxG Approval UI`;
   - email поддержки — владельца;
   - статус оставь **Testing**;
   - **Test users** — добавь Google-адрес владельца.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID → Web application**, имя `GxG Approval UI`. Authorized redirect URIs:
   - `https://gxg-approval-ui.<tailnet>.ts.net/auth/callback`
   - `https://gxg-approval-admin.<tailnet>.ts.net/auth/callback`

   `<tailnet>` — имя из п. 2.2. Нажми **Create**.
4. В `gxg-hub-pilot` → сервис `ui` задай переменные:
   - `GXG_OIDC_CLIENT_ID` — Client ID;
   - `GXG_SECRET__GXG_OIDC_CLIENT_SECRET` — Client secret;
   - `GXG_OIDC_ISSUER` = `https://accounts.google.com`;
   - `GXG_UI_PUBLIC_URL` = `https://gxg-approval-ui.<tailnet>.ts.net`.
5. Сервис `admin`:
   - `GXG_ADMIN_OPERATORS` — Google-адрес владельца;
   - `GXG_ADMIN_OIDC_AUDIENCE` — тот же Client ID, если Railway показывает эту переменную пустой.

## 6. ContextForge

Сервис `contextforge`:
- `PLATFORM_ADMIN_EMAIL` — email владельца;
- `APP_DOMAIN` = `https://gxg-gateway.<tailnet>.ts.net`.

## Не трогай

- `GXG_BUNDLE_KEYS`, `GXG_BUNDLE_B64`, `TELEGRAM_CONNECTIONS`, `TELEGRAM_BUNDLE_KEYS` задаст оркестратор, когда будут выбраны чаты пилота.
- Остальные ключи и пароли уже сгенерированы. Не меняй их и не удаляй.

## Отчёт (без значений секретов)

- готов ли доступ Railway к двум репозиториям;
- имя tailnet;
- username бота;
- список заданных переменных по сервисам, только имена;
- что не получилось и почему.
