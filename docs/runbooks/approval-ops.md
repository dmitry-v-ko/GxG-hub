# Runbook: Approval Service, эксплуатация (M10)

Процессы, сети и секреты — `approval/deploy/compose.yaml`. Метрики — `api:8090/metrics`, `executor:9101/metrics`, `demo:8081/metrics`. Правила alert'ов — `approval/deploy/prometheus/rules.yaml`. Получатель Alertmanager — канал оператора.

Команды оператора идут через admin API (`admin-ingress`, только loopback хоста, наружу — `tailscale serve` для `tag:approval-admin`). Для них нужны:
- `GXG_ADMIN_URL` — адрес admin API;
- `GXG_OPERATOR_TOKEN_FILE` — файл с ID token оператора из IdP. Оператор должен быть в `GXG_ADMIN_OPERATORS` процесса `admin`.

## 1. Kill switch (EX-13, AS-05)

**Когда:** подозрение на неверные эффекты, компрометация коннектора или агента, инцидент провайдера.

```bash
gxg-approval switch set --scope global --reason "INC-123: provider sends duplicates"
gxg-approval switch set --scope tenant:t-alpha --reason "..."
gxg-approval switch set --scope tool:t-alpha:demo.message.propose --reason "..."
```

**Что происходит.** Switch проверяется внутри транзакций T1, T7 и T9. Поэтому следующий `dispatch` возвращает 423 без кешей, а провайдер не вызывается. Исполнения, которые уже `dispatching`, доходят до своего исхода. Действие пишется в `audit_events` (`admin_switch_set`), алерты — `GxgSwitchChanged` и `GxgAdminAction`.

**Снять блокировку.** Нужен свежий вход в IdP: `auth_time` не старше 5 минут, иначе 403.

```bash
gxg-approval switch clear --scope global --reason "INC-123 resolved"
```

**Проверить состояние:** `GET /v1/switches` или метрика `gxg_switch_blocked{scope}`.

## 2. Ротация ключа execution token (AS-09, AS-10)

1. На хосте executor'а выполнить `gxg-approval keys rotate`. Новый ключ сразу попадает в JWKS, а executor начинает подписывать им через 15 минут.
2. Через 24 часа после последнего использования старого ключа выполнить `gxg-approval keys remove --kid <старый>`.
3. **Если ключ скомпрометирован:**
   - сразу выполнить `keys remove --kid <kid>`;
   - затем в executor выполнить `gxg-approval keys release`: все `reserved` исполнения перейдут в `released`.
4. Каждая команда пишет одну строку в `audit_events` (SEC-09): `key_rotated`, `key_removed`, `keys_released`, актор `operator:<пользователь ОС>`, в `details` — `kid` и SHA-256 публичного JWKS до и после (`jwks_sha256_before`/`jwks_sha256_after`; у `keys_released` — число освобождённых). Команде нужен `GXG_DB_DSN_FILE` роли executor'а (на хосте для `rotate` и `remove` тоже). Строка пишется до изменения файлов, в одной транзакции: если БД недоступна, команда падает, ключи и JWKS не меняются. Ротацию всё равно отметить в журнале инцидентов.

## 3. Приглашение на passkey (AS-30)

Каждое решение (approve и reject) подписывается passkey approver'а. Passkey регистрируется только по одноразовому приглашению оператора, полученному вне UI.

```bash
gxg-approval passkeys invite --human 'oidc:https://idp.example.com:<sub>'
```

1. Передать код из ответа (`invite`) человеку по отдельному каналу. Код действует 24 часа и только один раз.
2. Человек открывает страницу «Passkey» в UI, вводит код и регистрирует ключ.

**Потерян или скомпрометирован passkey.** Ролью владельца (DBA) выполнить `UPDATE webauthn_credentials SET enrolled_by = NULL WHERE credential_id = …`, затем выдать новое приглашение. Строку **не удалять**: по её публичному ключу проверяются прошлые решения (AS-30, assertion в `decisions`). Decider не принимает ключ без `enrolled_by`, поэтому отозванный ключ решать больше не может. Passkey, записанные до M10 (`enrolled_by IS NULL`), тоже не принимаются: такому approver'у нужно новое приглашение.

## 4. `outcome_unknown` дольше часа (алерт `GxgOutcomeUnknownOverAnHour`)

1. Найти intent: UI → «Неясный исход» или метрика `gxg_intents_outcome_unknown{tenant}`.
2. Executor повторяет reconcile с backoff до 24 часов. Если провайдер ответил `unknown`, нужно сверить с провайдером вручную: журнал доставки и `provider_operation_id` в `executions`.
3. Ручное решение (T18) принимает `approver` или `tenant_admin`. Обоснование — не меньше 10 символов, оно пишется в аудит.
4. Повторную отправку не делать: после `reconciled_failed` бизнес-сервис создаёт новый intent (AS-04).

## 5. Webhook подписчика `dead` (алерт `GxgWebhookDead`)

**Что произошло.** Событие не удалось доставить за 24 часа: повторы шли с backoff от 5 секунд до 1 часа. После этого строка помечается `dead`, и notifier переходит к следующему событию.

1. Проверить подписчика: сеть `notifier_webhooks`, сертификат, ответ 2xx, секрет `secret://…` в `<GXG_WEBHOOK_SECRETS_DIR>/…`. Если секрета нет, notifier ничего не отправляет и пишет `last_status = 'no secret'`.
2. Подписчик восстанавливает пропущенное сам, опросом `effects.status`: webhook — только подсказка (AS-23).
3. Если нужна повторная доставка, роль владельца выполняет `UPDATE webhook_deliveries SET dead_at = NULL, next_at = now(), created_at = now() WHERE id = …`. Сделать это нужно в течение 7 дней, потом строка удаляется очисткой.

## 6. Очистка payload (RET-01)

`sweeper` раз в час вызывает `gxg_purge(GXG_RETENTION_DAYS)`, по умолчанию 30 дней, допустимо 1…90. Что делает очистка:
- у intents, которые терминальны дольше срока, обнуляет `args_ciphertext` и `preview_ciphertext`. Метаданные и аудит остаются, в аудит пишется `payload_purged`. Intents в `outcome_unknown` не очищаются;
- удаляет строки outbox, доставленные больше суток назад (`notify` хранит title и requester);
- удаляет завершённые доставки webhook старше 7 дней;
- удаляет старые challenge, приглашения и коды привязки Telegram.

**Проверка:** `SELECT count(*) FROM intents WHERE payload_purged_at IS NOT NULL`.

## 7. Bundle старше 13 часов (алерт `GxgBundleStale`)

1. Проверить плановую переподпись (CI/cron оператора) и `readyz` у `api`, `ui`, `executor`.
2. Если bundle старше 24 часов, процессы отказывают (GR-13): переподписать и выкатить bundle сразу.
