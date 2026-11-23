# M10 — вопросы владельцу (`pending_owner_decision`)

Работа по этим вопросам не блокировалась: сделан консервативный вариант по умолчанию. Если владелец решит иначе, изменение пойдёт как `new_scope` следующего цикла.

| # | Вопрос | Сделано по умолчанию | Где |
| --- | --- | --- | --- |
| 1 | AS-30 задаёт challenge = SHA-256(intent_id ‖ args_hash ‖ version ‖ decision). Он детерминирован, поэтому свежесть ≤ 5 мин (AS-16) и однократность по нему не проверить | challenge = SHA-256(…) ‖ 16 случайных байт. Challenge — строка `decider_challenges`, действует 5 мин, используется один раз. Хеш-часть совпадает с формулой спеки | `0005_ops.py` (`gxg_decision_challenge`, `gxg_decide`) |
| 2 | Как регистрировать passkey «вне ui» (AS-30): одноразовая ссылка оператора или повторный вход в IdP с `max_age` | Одноразовое приглашение оператора: `gxg-approval passkeys invite`, 24 ч, хеш в `passkey_invites`. Passkey без `enrolled_by` (записанные M9) не принимаются, и approver'ам нужна перерегистрация | `decider.py`, `admin.py`, runbook §3 |
| 3 | Нужен ли service JWT для вызова ui → decider (в AS-06 у ui нет `kind`) | Без service JWT: decider доступен только из сети `ui_decider`. Полномочия даёт assertion, а не вызывающий процесс: decider сам связывает человека с credential и challenge | `compose.yaml`, `decider.py` |
| 4 | Где задаётся роль `platform_admin` (AS-24). В bundle такой роли нет | Список `GXG_ADMIN_OPERATORS` в конфигурации процесса `admin`. Step-up для `switch clear` — ID token с `auth_time` не старше 5 мин | `admin.py` |
| 5 | Какие права у `gxg_decider` и `webhook_deliveries` — в таблице AS-27 их нет | `gxg_decider`: SELECT `webauthn_credentials` (без `created_at`) и `decider_challenges`, EXECUTE четырёх функций. `gxg_notifier`: SELECT, INSERT и UPDATE статусных колонок `webhook_deliveries`. У `gxg_ui` отозваны INSERT и UPDATE на `webauthn_credentials` и EXECUTE `gxg_decide` | `0005_ops.py`, `test_roles.py` |
| 6 | Срок хранения payload по tenant'у (RET-01: 1…90 дней). Поля в схеме политики нет | Один срок на весь сервис: `GXG_RETENTION_DAYS`, по умолчанию 30 | `sweeper.py`, `gxg_purge` |
| 7 | `max_attempts` в схеме политики (вопрос из M8) | Необязательное поле в `defaults` и в правиле, 1…3, по умолчанию 3. Из нескольких значений берётся меньшее, фиксируется при T1. Увеличить сверх 3 нельзя: как и сроки, поле только уменьшает | `approval-policy.schema.json` (+ копия в kit), `policy.py`, `approval-policy-v1.md` |
| 8 | Порядок доставки webhooks: в AS-23 это не задано | Head-of-line по подписчику: следующее событие ждёт доставки или `dead` предыдущего. Отсюда порядок `version` после простоя (AC-M10-02) | `notifier/webhooks.py` |
| 9 | T18 (ручной разбор `outcome_unknown`) подписывается passkey? AS-30 называет только approve и reject | Нет, T18 остаётся в ui, как в M9 | — |
| 10 | Отдельный процесс для `/metrics` | Метрики из БД отдаёт `api` (`gxg_metrics()`, только агрегаты). У `executor` свой порт 9101 (`gxg_reserve_conflicts_total`), у коннектора demo — `/metrics` на внутреннем порту (`gxg_token_verify_failures_total`) | `metrics.py`, `cli.py`, `effects.py` |

## Остаточные риски AS-30: нужно принять или выбрать защиту (критик M10 ц1)

| # | Риск | Почему остаётся | Возможная защита |
| --- | --- | --- | --- |
| R1 | Скомпрометированный `ui` подменяет challenge. Человек нажимает «Отклонить» на intent A, а его passkey подписывает approve для intent B | WebAuthn не показывает человеку содержимое challenge. Подпись привязана к B, к версии и к решению, но человек видит экран A | Показ решения вне `ui`, например push в доверенный канал или страница decider. Либо transaction confirmation (нет в браузерах). По умолчанию принято как известный риск |
| R2 | Код приглашения проходит через `ui`: скомпрометированный `ui` может зарегистрировать свой ключ, пока приглашение не использовано | Приглашение вводится на странице `ui` | Регистрация на отдельном origin decider либо подтверждение входом в IdP с `max_age` (вариант 2 AS-30). По умолчанию — срок 24 ч и одно использование, аудит `passkey_enrolled` с оператором |
