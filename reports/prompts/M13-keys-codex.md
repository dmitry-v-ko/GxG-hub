# M13: получить ключи для пилота (промпт для Codex)

Задача — получить внешние ключи для пилота GxG Hub и положить их в Railway. **Значения ключей нельзя выводить в чат, в лог и в git**: только в поля, указанные ниже. В отчёте перечисляются только имена переменных и где они заданы.

## 0. Railway-проект

Проект `gxg-hub-pilot` (окружение `production`) и его сервисы уже созданы. **Shared Variables не используй**: по AS-28 каждый секрет должен быть только у своего сервиса, проверка `--verify` shared-переменные запрещает. Ключи пп. 2–5 клади в Variables **конкретных сервисов** (сервис → Variables → New Variable) под указанными именами.

## 1. Railway API token → в Claude, не в Railway

- Где: railway.com → Account Settings → Tokens → Create token, scope — workspace с проектом `gxg-hub-pilot`. Имя: `gxg-hub-orchestrator`.
- Куда: claude.ai → Settings → Environment (Default) → Network secrets → Add. Хост: `backboard.railway.com`, заголовок `Authorization`, префикс `Bearer`.
- Если у тебя нет доступа к claude.ai, остановись на этом пункте и скажи владельцу, что этот токен он вставляет сам.

## 2. Tailscale — `TS_AUTHKEY`

1. login.tailscale.com → Access controls. В `tagOwners` должны быть `tag:gateway`, `tag:approval-ui`, `tag:approval-admin`, `tag:agent` с владельцем `autogroup:admin`. Если их нет, добавь.
2. DNS: включи MagicDNS и HTTPS Certificates. Запиши имя tailnet (вида `xxxx.ts.net`): это не секрет, оно нужно в п. 5.
3. Settings → Keys → Generate auth key. Параметры: Reusable — да, Ephemeral — нет, Pre-approved — да, Tags: `tag:gateway`, `tag:approval-ui`, `tag:approval-admin`, срок 90 дней.
4. Куда: переменная `TS_AUTHKEY` у сервисов `contextforge` и `ui`.
5. Funnel не включай.

## 3. Telegram API приложения — `TG_API_ID`, `TG_API_HASH`

- Где: эти значения уже заданы в Railway у существующего Telegram-коннектора в проекте `gxg_agents_v2`, сервис Telegram ingest, переменные `TG_API_ID` и `TG_API_HASH`. Возьми их оттуда. Если их там нет — my.telegram.org → API development tools (вход под аккаунтом владельца).
- Куда: сервис `telegram`, переменные `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`.
- Сессию Telegram не создавай: в неё владелец входит сам после развёртывания (код и 2FA).

## 4. Telegram-бот уведомлений — `GXG_TELEGRAM_BOT_TOKEN`

- Где: @BotFather → `/newbot`, имя «GxG Approvals», username — любой свободный, оканчивающийся на `_bot`. Это отдельный бот, не аккаунт коннектора.
- Куда: сервис `notifier`, переменная `GXG_SECRET__GXG_TELEGRAM_TOKEN`.
- Попроси владельца написать боту `/start`.

## 5. Вход людей в Approval UI (OIDC, Google) — `GXG_OIDC_CLIENT_ID`, `GXG_OIDC_CLIENT_SECRET`

- Где: console.cloud.google.com → APIs & Services → Credentials → Create OAuth client ID → Web application. Имя: «GxG Approval UI».
- Authorized redirect URI: `https://gxg-approval-ui.<tailnet>.ts.net/auth/callback`, где `<tailnet>` — имя из п. 2.2.
- OAuth consent screen: тип External, статус Testing, в Test users только Google-аккаунт владельца.
- Куда: сервис `ui`, переменные `GXG_OIDC_CLIENT_ID`, `GXG_SECRET__GXG_OIDC_CLIENT_SECRET` и `GXG_OIDC_ISSUER=https://accounts.google.com`.

## 6. Email администратора ContextForge

- Сервис `contextforge`, переменная `PLATFORM_ADMIN_EMAIL`: email владельца для входа в админку.

## Не нужно

Ключи подписи, HMAC webhook'ов, пароли баз, ключи сессий UI и шифрования payload и preview скрипт развёртывания генерирует сам. Supabase для пилота не нужен.

## Отчёт

Коротко, без значений:
- какие переменные заданы и у каких сервисов;
- имя tailnet;
- username бота;
- добавлен ли Railway-токен в claude.ai или его вставляет владелец.
