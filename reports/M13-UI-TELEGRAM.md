# M13-UI-TELEGRAM: подключение Telegram-аккаунта пилота из Approval UI

Ветка `claude/m13-ui-telegram` (от `claude/m13-deploy`). `railway_deploy.py` не запускался, PR не создан.

## Что сделано

1. **Раздел `/operator/telegram`** (`approval/ui`). Оператор — OIDC-пользователь, чей подтверждённый email есть в
   `GXG_UI_OPERATORS`. Остальным 404 (и `GET`, и все `POST`); без сессии — 403 «Войдите заново».
   - `/auth/complete` кладёт email в сессию только при `email_verified: true`; scope OIDC теперь `openid email`.
   - Шаги (`GET` показывает то, что говорит `status` Telegram-сервиса): телефон → код → пароль 2FA (если
     `password_required`) → «Готово» → список чатов с чекбоксами (≤ 3) → «Сохранить выбор для пилота» (`PUT scope`).
     Под списком — выбранные `chat_ref`. Кнопка «Начать заново» = `login/cancel`.
   - Каждый `POST` — с CSRF, затем redirect 303 (PRG): введённое значение нигде не отображается повторно. Пароль —
     `type=password autocomplete=off`. Телефон, код, пароль уходят в API один раз.
   - Проверки до API: формат телефона/кода, пароль непустой ≤ 256, ≤ 3 чатов, каждый `chat_ref` есть в `GET chats`.
   - Ошибка API показывается текстом (`message`/`detail` от сервиса или понятная фраза по статусу); недоступность —
     «Telegram-сервис не отвечает…» (502). Из текста ошибки вырезается всё, что оператор только что ввёл.
   - Клиент: `tgadmin.py`. Service JWT `iss=approval-ui`, `aud=telegram` (по умолчанию), TTL 60 с, подпись
     `GXG_UI_SVC_KEY`; заголовок `X-GxG-Operator: oidc:<iss>:<sub>`.
2. **Журналы.** uvicorn access log уже без query (`NoQuery`, тест есть), тела не пишутся; логгеры httpx/httpx2/httpcore
   на WARNING. Мастер использует только `POST`-тела. Тест прогоняет весь мастер с `caplog` на DEBUG: телефон, код и пароль
   в записях журнала не встречаются. Свои `log.*` в ui не добавлялись.
3. **Деплой.** `services.yaml`: у `ui` секрет `GXG_SECRET__GXG_UI_SVC_KEY` (`ed25519_seed`, AS-28 `ui_svc_key`),
   `GXG_TELEGRAM_ADMIN_URL=http://${{telegram.RAILWAY_PRIVATE_DOMAIN}}:8082` (в `internal_urls`), `GXG_UI_OPERATORS` —
   `owner_env`; у `telegram` — `TELEGRAM_ADMIN_CALLER_KEYS` (config, генератор `caller_keys:ui.<секрет>:approval-ui`).
   `railway_deploy.py`: AS-28 `ui_svc_key` только у `ui`; новый генератор `caller_keys`; `--verify` проверяет, что
   значение у `telegram` равно публичной половине живого секрета `ui`, и что URL приватный.
4. **Главная `/`** для оператора без заявок: ссылки «Подключение Telegram» и «Passkey»; в навигации — ссылка для оператора.
5. Побочно: разбил слишком длинную строку docstring в `deploy/railway/telegram_db_init.py` (E501 останавливал `check-all`).

## Новые переменные

| Переменная | Где | Секрет |
| --- | --- | --- |
| `GXG_UI_OPERATORS` | ui, owner_env: emails через запятую | нет |
| `GXG_TELEGRAM_ADMIN_URL` | ui, `http://telegram.railway.internal:8082` | нет |
| `GXG_SECRET__GXG_UI_SVC_KEY` → `GXG_UI_SVC_KEY_FILE` | ui, генерирует `railway_deploy.py` | да (AS-28 `ui_svc_key`) |
| `GXG_TELEGRAM_ADMIN_AUDIENCE` | ui, необязательна, по умолчанию `telegram` | нет |
| `TELEGRAM_ADMIN_CALLER_KEYS` | telegram, `approval-ui=ed25519:<pub>` | нет |

Без `GXG_TELEGRAM_ADMIN_URL` страница отвечает 503 «не настроено»; URL без ключа — отказ старта.

## Тесты

- `approval/ui/tests/test_telegram_wizard.py` (fake Telegram-API проверяет service JWT как настоящий; без БД): весь
  мастер (с 2FA и без), cancel, не-оператор (нет email / чужой email) → 404 на GET и все POST, без сессии → 403, CSRF на
  каждом шаге, секреты не в HTML и не в логах, ошибки API (текст, эхо кода вырезается, 401, 500, API недоступен),
  локальные проверки, ≤ 3 чатов и неизвестный чат, неизвестный шаг, «не настроено», главная оператора, claims JWT.
- `approval/ui/tests/test_cli.py`: проводка `GXG_UI_OPERATORS`, URL, ключа, audience.
- `tests/deploy`: ключ `approval-ui` только у `ui`, публичная половина у `telegram`, `--verify` зелёный/красный, AS-28.

Результат локально: `uv run pytest approval/ui/tests tests/deploy` — 119 passed; ruff, ruff format, mypy --strict — чисто.

## Не проверено / допущения

- **Docker нет в этой среде**: `approval/ui/tests_db`, E2E (Playwright), `approval/tests_db` не запускались. В
  `tests_db/conftest.py` `Browser.login` не умеет класть email в сессию — E2E мастера не написан.
- Ветка `/auth/complete` с `email`/`email_verified` не покрыта юнит-тестом (нужен IdP-mock); провайдер должен отдавать
  `email` и `email_verified` **в ID token** (Google отдаёт). Если нет — оператор не определится; запасной вызов userinfo не сделан.
- Формат `TELEGRAM_ADMIN_CALLER_KEYS` (`approval-ui=ed25519:<b64url>`, как `TELEGRAM_IDENTITY_KEYS`) и `aud=telegram`
  — моё допущение по контракту; сверить с другим тредом, при расхождении поменять строку в `services.yaml` / `GXG_TELEGRAM_ADMIN_AUDIENCE`.
- Пустой выбор чатов разрешён (`PUT scope` с `[]` очищает).
- Порт 8082 в `services.yaml` у `telegram` не объявлен (серверная часть — другой тред).
