# gxg-approval-ui

Approval Web UI ([approval-service-v1 «Web UI»](../../docs/specs/approval-service-v1.md#web-ui), EFF-15, AS-15…AS-17),
веха M9. Отдельный образ и lockfile (AS-29): от `gxg-approval` не зависит, общий только `gxg-connector-kit`.

## Экраны

| Путь | Кто | Что |
| --- | --- | --- |
| `/login` → IdP → `/auth/callback` → `/auth/complete` | все | OIDC code flow с PKCE и nonce (authlib). Человек — `oidc:<iss>:<sub>` из проверенного ID token. Промежуточная страница `/auth/callback` нужна из-за `SameSite=Strict`: переход с IdP межсайтовый, cookie в нём не приходит |
| `/` | `approver` | Ожидают решения по своим tenant'ам, по сроку; одинаковые (requester + preview) сгруппированы, каждая решается отдельно (AP-09) |
| `/intents/<id>` | `approver`, `viewer`, `tenant_admin` | Карточка: preview как текст с маркерами `⟦U+XXXX⟧` и предупреждением (EFF-31…33), канонические args, requester, `business_task_id`, правило, срок, история решений. Чужой tenant — 404 |
| `POST /intents/<id>/decision` | `approver` | CSRF, `version`, `args_hash`; для `step_up_required` — WebAuthn-assertion (challenge = `intent_id` + `args_hash`, ≤ 5 мин, одноразовый) |
| `/unknown`, `POST /intents/<id>/resolve` | `approver`, `tenant_admin` | T18 с обоснованием ≥ 10 символов |
| `/passkeys` | все | Регистрация passkey (AS-17) |
| `/telegram`, `POST /telegram/code` | `approver` | Код из 6 цифр на 10 минут для `/link <code>` в боте |
| `/history` | `approver`, `viewer` | Терминальные intents |
| `/healthz`, `/readyz` | — | `readyz`: БД и свежий bundle |

Все записи идут через функции `SECURITY DEFINER` роли `gxg_ui` (`gxg_decide`, `gxg_resolve_manual`, `gxg_link_code`)
и таблицу passkey. Повтор step-up-assertion из старой копии cookie нового решения не даёт: один голос на человека
обеспечивает БД (`UNIQUE (intent_id, decided_by)`); одноразовость на сервере — с `decider` (AS-30, M10). БД сама проверяет статус, `version`, `args_hash`, срок, requester ≠ approver и набор approver'ов,
замороженный при T1; ui дополнительно проверяет роль по текущему bundle и step-up.

## Ограничения

- `oidc-egress` — TCP-реле на один хост. IdP, у которых discovery, token и JWKS на разных хостах (например, Google),
  требуют по реле на хост; до выбора провайдера (backlog M9-F11).

## Конфигурация

Переменные — в `src/gxg_approval_ui/cli.py`. Секреты — только файлы `*_FILE` (AS-28): DSN роли `gxg_ui`, ключ
сессий, OIDC client secret, приватные preview-ключи tenant'ов, TLS-ключ. Без TLS процесс стартует только на loopback
(тесты); `GXG_UI_PUBLIC_URL` и `GXG_OIDC_ISSUER` обязаны быть `https` вне loopback.

Заголовки: CSP `default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`, без inline-скриптов
(единственный скрипт — `/static/webauthn.js`); cookie `__Host-gxg`, `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`.

## Runbook

| Симптом | Что делать |
| --- | --- |
| `/readyz` 503, `db: false` | Проверить DSN-секрет и сеть `ui_db` |
| `/readyz` 503, причина про bundle | Bundle старше 24 ч или не загрузился — проверить публикацию bundle (GR-12, GR-13) |
| 503 «Сервис временно недоступен» на карточке | Нет preview-ключа tenant'а в `GXG_PREVIEW_KEYS_FILE` |
| Approver не может одобрить `high_risk` | Нет passkey (карточка это показывает) — зарегистрировать на `/passkeys` |
| Подозрение на компрометацию ui | Сменить ключ сессий (все сессии недействительны), OIDC client secret, пароль `gxg_ui`; проверить `audit_events` по `decide`/`resolve_manual`/`passkey_registered`; kill switch (M10) |

## Тесты

- `uv run pytest approval/ui/tests` — без БД: маркеры невидимых символов, TLS-правило.
- `uv run pytest approval/ui/tests_db` — PostgreSQL в Docker (через Unix-сокет) и E2E: Playwright (Node, Chromium),
  процесс `gxg-approval-ui`, тестовый OIDC issuer `oidc-provider-mock` (только в тестах и профиле `test` compose).
- Мутации: `[tool.mutmut]` в `pyproject.toml`, `scripts/ci/mutmut.sh`.

## M10: решения через decider (AS-30)

ui больше не проверяет WebAuthn и не пишет решения. Как идёт решение:
1. Кнопка «Одобрить» или «Отклонить» запрашивает challenge: `POST /intents/<id>/challenge`.
2. ui пересылает запрос в decider (`GXG_DECIDER_URL`), тот выдаёт challenge с привязкой к intent, версии, хешу и решению.
3. Браузер подписывает challenge passkey.
4. ui пересылает сырой assertion в `decider`, который проверяет его и вызывает `gxg_decide`. У роли `gxg_ui` этой функции нет.

Passkey регистрируется только по одноразовому приглашению оператора (`gxg-approval passkeys invite`). Access log uvicorn пишется без query string, поэтому `?code=` OIDC callback в лог не попадает (SEC-17).
