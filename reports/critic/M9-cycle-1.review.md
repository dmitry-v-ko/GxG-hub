# M9 — ponytail-review критика, цикл 1

Дифф: `3c2f0b0..c402415` (ветка `claude/m9-ui-bot`; `6453cf0` после него меняет только `reports/conformance/M9.json`).
Навык: `.claude/skills/ponytail-review/SKILL.md`. Нагрузка из репозитория (approval-service-v1 «Развёртывание»,
self-review исполнителя): по одному экземпляру `ui` и `notifier`, несколько approver'ов на tenant, десятки–сотни
intents в день. Под неё оценивал масштаб.

## What this change does

Добавляет два внешних процесса Approval Service отдельными образами со своими lockfile:
- `ui` (`approval/ui`): вход через OIDC (authlib), сессия в подписанной cookie, список/карточка/история, решение с
  CSRF, `version` и `args_hash`, step-up WebAuthn (py_webauthn), ручной T18, выдача кода привязки Telegram;
- `notifier` (`approval/notifier`): outbox `notify` → Telegram (только URL-кнопка), long polling только `/start` и
  `/link`, сводка AP-08.
БД: `gxg_decide` проверяет набор approver'ов, замороженный при T1; таблицы `link_codes`, `webauthn_credentials`;
`gxg_ui` теряет прямую запись в `outbox` и `approver_links`. Compose-сети по AS-29 и тест на настоящих контейнерах.

## Что проверено руками

- Тесты: `uv run pytest approval/tests approval/ui/tests approval/tests_db approval/ui/tests_db
  approval/notifier/tests_db` — 521 passed; E2E отдельно (`test_e2e.py`, Playwright/Chromium) — 2 passed;
  `uv run pytest approval/deploy/tests` (сборка образов с прокси-CA) — 6 passed.
- mutmut заново в отдельном worktree на `c402415`: notifier 253 мутанта — 252 убиты, 1 выжил
  (`_notify__mutmut_9`).
- SQL-мутации: все строки M9 из `scripts/ci/mutate_m7_sql.py` (20) убиты; из 5 своих мутантов выжили два: «привязать только первый tenant» (находка 12) и «ui сохраняет USAGE на `outbox_id_seq`» (эквивалентен: INSERT в outbox отозван).
- mutmut ui повторён: 572 мутанта — 569 убиты, 3 выжили (те же, что в списке эквивалентных).
- Образ `gxg-approval-notifier:m9-test` запущен с токеном `999:CRITIC-DEMO-SECRET` против локального HTTP-сервера:
  токен в логе (находка 1).
- PostgreSQL: вызов функции с параметром `integer` и значением `2**31` из psycopg → `42883 function … (bigint) does
  not exist` (находка 9).

## Must fix

1. **Токен Telegram-бота пишется в лог каждые 25 секунд** (`approval/notifier/src/gxg_approval_notifier/cli.py:20`,
   `bot.py:66`)
   - **What this is:** бот обращается к Bot API по URL `https://api.telegram.org/bot<токен>/<метод>`. Процесс
     включает логирование уровня INFO для всех логгеров (`logging.basicConfig(level=logging.INFO)`).
   - **Problem:** httpx на уровне INFO пишет каждую строку запроса с полным URL. В логе контейнера каждый
     `getUpdates` и `sendMessage` выглядит так:
     `INFO httpx HTTP Request: POST http://127.0.0.1:18099/bot999:CRITIC-DEMO-SECRET/getUpdates "HTTP/1.0 501 …"`
     (проверено на собранном образе). Код сам старательно убирает URL из `BotError`, но библиотека пишет его
     рядом. Это нарушение SEC-17 («секреты никогда не попадают в логи») и AS-28 (токен — только у `notifier`):
     лог читает любой, кто читает логи контейнеров.
   - **Fix:** в `cli.main` после `basicConfig` — `logging.getLogger("httpx").setLevel(logging.WARNING)` (и
     `httpcore`). Тест: запустить `Bot.call` с конфигурацией логов из `cli` под `caplog` уровня INFO и проверить,
     что токена нет ни в одной записи.
   - **If we skip it:** токен бота утекает в систему логов; с ним можно читать и слать сообщения от имени бота
     approver'ам (фишинговые ссылки «одобрить»).

## Should fix

2. **Два «эквивалентных» мутанта не эквивалентны** (`scripts/ci/mutmut-equivalent.txt:97,100`; `app.py:143`,
   `bot.py:134`)
   - **What this is:** `rstrip("/")` срезает конечный слэш у issuer и у адреса UI. Мутант меняет его на
     `rstrip("XX/XX")`, то есть срезает и все конечные `X`.
   - **Problem:** причина в списке — «URL никогда не кончается на X». Это допущение, а не эквивалентность: issuer
     Keycloak `https://sso.example.com/realms/GXGX` с мутантом даёт discovery по `…/realms/G/.well-known/…`, вход
     ломается, а тесты зелёные. DoD-4: выжившие неэквивалентные мутанты недопустимы.
   - **Fix:** `removesuffix("/")` вместо `rstrip("/")` в обоих местах — тогда мутант (`removesuffix("XX/XX")`)
     ломает уже существующие тесты со слэшем на конце (`test_issuer_with_a_trailing_slash`, ui_url со слэшем), либо
     один тест с URL на `X`. Обе строки убрать из списка.
   - **If we skip it:** список эквивалентных мутантов начинает прятать реальные пробелы в тестах.

3. **Запуск `ui` без TLS вне loopback ничем не проверен; `cli.py` не в mutmut** (`approval/ui/src/gxg_approval_ui/cli.py:55-82`,
   `approval/ui/pyproject.toml:42`, `scripts/ci/mutmut.sh:9`)
   - **What this is:** `main()` отказывается стартовать без TLS-сертификата на не-loopback адресе и требует `https` у
     публичного URL и issuer (AS-29 «входящий только HTTPS»).
   - **Problem:** `mutmut.sh` пишет, что мутируются «app, stepup, render, cli», а `only_mutate` ui `cli.py` не
     содержит. Тестов на `main()` нет: если удалить ветку `elif host not in LOOPBACK: raise SystemExit(…)` или
     вызовы `require_tls`, все тесты остаются зелёными, и `ui` молча слушает обычный HTTP на `0.0.0.0:8443`.
   - **Fix:** тест `main()` с подменённым `uvicorn.run` и env: без сертификата на `0.0.0.0` → `SystemExit`;
     `http://` public URL/issuer → `SystemExit`; с сертификатом → `uvicorn.run(..., ssl_certfile=…)`. Добавить
     `cli.py` в `only_mutate` (или исправить комментарий).
   - **If we skip it:** проверка, которая держит HTTPS-only, может пропасть при рефакторинге незаметно.

4. **Один битый preview-ключ кладёт весь список** (`approval/ui/src/gxg_approval_ui/app.py:203-240`, `envelope`/`summary`)
   - **What this is:** каждая строка списка расшифровывает preview, чтобы показать заголовок.
   - **Problem:** нет ключа tenant'а или ключ не открывает конверт (ротация: `api` уже шифрует новым ключом, а у
     `ui` старый) → `Refused(503)` на весь `/`, `/history`, `/unknown`. Approver двух tenant'ов не видит заявок и
     исправного tenant'а. EFF-14: список в UI — источник истины, когда бот молчит.
   - **Fix:** в `summary()` ловить `Refused` и показывать «превью недоступно» вместо заголовка; карточка по-прежнему
     503. Тест: два tenant'а, у одного нет ключа — список второго виден.
   - **If we skip it:** одна ошибка конфигурации останавливает согласование во всех tenant'ах человека.

5. **«Одноразовость» step-up держится на копии cookie, тест принимает 409** (`stepup.py:1-5,157`,
   `tests_db/test_decisions.py:222-241`)
   - **What this is:** challenge step-up лежит в подписанной cookie-сессии и «выдёргивается» при первой проверке.
   - **Problem:** сессия хранится у клиента: старая копия cookie по-прежнему содержит challenge. У синхронизируемых
     passkey (iCloud/Google) `sign_count` всегда 0, и py_webauthn повтор не ловит. Тест повтора принимает `403` или
     `409`; `409` даёт правило «один голос на человека» в БД, а не проверка step-up, так что тест прошёл бы и без
     pop. Реального вреда нет: challenge привязан к intent и хешу, второго голоса БД не даст.
   - **Fix:** исправить формулировку в docstring/README («один голос на человека держит БД; повтор assertion в
     пределах 5 минут не даёт нового решения»), в тесте — аутентификатор со счётчиком 0 и ожидание `409` с
     проверкой, что решение одно. Серверное хранение использованных challenge — вместе с `decider` (AS-30, M10).
   - **If we skip it:** документ обещает свойство, которого нет; следующий, кто снимет уникальный индекс голоса,
     получит повтор.

6. **Загрузчик bundle скопирован** (`approval/ui/src/gxg_approval_ui/cli.py:32-52` ≈ `approval/src/gxg_approval/config.py:51-71`)
   - **What this is:** чтение `grants-bundle.json` + `.sig` и перезагрузка раз в минуту.
   - **Problem:** две копии, которые должны меняться вместе (GR-13: интервал, обработка ошибок). `ui` не может
     зависеть от `gxg_approval` (AS-29), но обе уже зависят от `gxg_connector_kit`.
   - **Fix:** одна функция в `gxg_connector_kit.grants` (например, `BundleStore.watch(dir)`), оба `cli` её зовут.
   - **If we skip it:** правка интервала или логики в одном процессе забудется во втором.

7. **Нет `shortcut:` в коде для сессии без отзыва** (`app.py:153`, `app.py:292-297`)
   - **What this is:** выход только очищает cookie в браузере.
   - **Problem:** план и self-review называют это `shortcut`, но в коде пометки нет; читатель `logout` думает, что
     сессия завершена, а скопированная cookie живёт до 30 минут простоя / 8 часов.
   - **Fix:** комментарий `shortcut: no server-side revocation, add a table when logout-everywhere is needed` у
     `SessionMiddleware` и `logout`.
   - **If we skip it:** ограничение известно только из отчётов.

## Nice to have

8. **Регистрация passkey без повторной аутентификации и без роли** (`app.py:437-457`, `stepup.py:89-117`)
   - **What this is:** `/passkeys` добавляет ключ любому вошедшему человеку.
   - **Problem:** украденная cookie сессии позволяет добавить свой passkey и пройти step-up на `high_risk`. Любой
     пользователь IdP без роли в bundle может добавлять ключи в `webauthn_credentials` без ограничения числа.
     Владелец перенёс регистрацию через `decider` с повторным входом в M10 (AS-30), поэтому для M9 это открытый путь,
     а не нарушение спеки.
   - **Fix (дёшево, до M10):** `/passkeys*` — только человеку с ролью `approver`; второй ключ — только после step-up
     существующим ключом.
   - **If we skip it:** step-up до M10 защищает от ошибки человека, но не от кражи сессии.

9. **Огромная `version` → 500** (`app.py:388-391`, `app.py:420-423`)
   - **Problem:** `version=2147483648` в форме: psycopg передаёт `bigint`, PostgreSQL не находит
     `gxg_decide(…, bigint, …)` (`42883`), `call()` пробрасывает ошибку → 500 вместо 400/409. Ничего не записано.
   - **Fix:** `0 < version < 2**31` в форме, иначе `NO_VERSION`.

10. **Подпись получателя в Telegram без маркеров невидимых символов** (`bot.py:101-110`)
    - **Problem:** `target_label` — данные backend'а (например, название чата). `U+202E` в нём переворачивает текст
      уведомления. Утечки нет (AS-19 разрешает поле), но подсказка может ввести в заблуждение; в UI тот же текст
      уже показан с `⟦U+202E⟧`.
    - **Fix:** та же замена Cc/Cf/Zl/Zp на маркеры (10 строк stdlib) для `title`, `target_label`, `requester`.

11. **Egress-реле к IdP — ровно один хост** (`approval/deploy/compose.yaml`, `oidc-egress`)
    - **Problem:** у части IdP discovery, token и JWKS на разных хостах (Google: `accounts.google.com`,
      `oauth2.googleapis.com`, `www.googleapis.com`). С одним реле вход не работает.
    - **Fix:** строка в README/compose «IdP с endpoint'ами на одном хосте; иначе по реле на хост» до выбора
      провайдера.

12. **Привязка Telegram для нескольких tenant'ов не проверена тестом** (`0002_ui_notifier.py`, `gxg_link_claim`,
    `FOREACH t IN ARRAY c.tenant_ids`)
    - **Problem:** мутант критика `c.tenant_ids[1:1]` (привязать только первый tenant) выжил на
      `approval/tests_db` + `ui/tests_db` + `notifier/tests_db`: все тесты привязки — с одним tenant'ом. Approver
      двух tenant'ов получал бы уведомления только по одному.
    - **Fix:** тест «код на два tenant'а → две строки `approver_links`, уведомления обоих доходят»; мутацию — в
      `mutate_m7_sql.py`.

## Что не находка (проверено)

- XSS: Jinja2 `autoescape=True`, нет `|safe`/`Markup`, единственный скрипт — `/static/webauthn.js`; E2E видит в
  `#preview` только теги шаблона.
- CSRF: все POST (`/logout`, решение, T18, `/passkeys` через заголовок, `/telegram/code`) сверяют токен из сессии
  `hmac.compare_digest`; `SameSite=Strict`.
- Reject без step-up — по T4 спеки («как T2, кроме step-up»).
- Кворум: `_gxg_intent` берёт `FOR UPDATE`, голос уникален `(intent_id, decided_by)`.
- OIDC: state, PKCE, nonce, `iss`/`aud`/`exp`/подпись — authlib, есть тесты; `/login` чистит сессию.

Verdict: fix 1 first, then 2–7, 9 and 12 (перенос в рубрику: 1 — critical; 2 — нарушение DoD-4; 3, 4 — major; остальные — minor).
Lean: -22 lines possible.
Not checked: реле `egress` против настоящих IdP/Telegram (запрещено в задании); GitHub CI (минут нет); mutmut пакета `approval` (изменения T1 в `intents.py`) и полный `check-all` — к моменту отчёта `check-all` владельца на `c402415` ещё шёл (stand, mutmut).
