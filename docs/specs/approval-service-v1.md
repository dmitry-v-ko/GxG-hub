# GxG Approval Service v1 — спецификация реализации

**Статус:** спецификация для реализации, код не написан. 2026-11-20.
Что и зачем — в [Эффекты и согласование](effects-and-approval-v1.md). Здесь — как построить: процессы, модель данных, переходы, API, аутентификация, хранение, эксплуатация.

## Стек

| Что | Выбор | Почему |
| --- | --- | --- |
| Язык | Python 3.12 | Общий `gxg-connector-kit` (канонизация, токены, grants) с коннекторами |
| HTTP | FastAPI | OpenAPI из коробки |
| БД | PostgreSQL 16, отдельная база | Транзакции и частичные уникальные индексы — основа атомарности |
| UI | Server-rendered (Jinja2 + htmx), без SPA | Меньше JS и поверхности для XSS/CSRF, cookie-сессии |
| Вход людей | Любой OIDC-провайдер (authlib) | Провайдер ещё не выбран; `oidc:<iss>:<sub>` — ID человека |
| Step-up | WebAuthn (passkey) | Сильнее TOTP, работает с телефона |
| Notify-бот | Telegram Bot API, **long polling** | Не нужен публичный webhook-endpoint |
| Подписи | Ed25519 (PyNaCl / `cryptography`), JWS | Одинаково для execution token и service JWT |
| Шифрование payload | Envelope encryption: data key на tenant, master key в secret store/KMS | Crypto-shredding при удалении |

## Процессы

Одна кодовая база, несколько процессов:

| Процесс | Порт / интерфейс | Доступен для |
| --- | --- | --- |
| `api` | 8090, внутренняя сеть | Коннекторы, Executor |
| `mcp` | 8091, внутренняя сеть | ContextForge (tools `effects.status`, `effects.cancel`) |
| `ui` | 8443 → `tailscale serve` HTTPS, только tailnet (`tag:approval-ui`) | Люди (`group:approvers`, SEC-26) |
| `admin` | 8092 → только tailnet (`tag:approval-admin`) | Оператор (`group:operators`) |
| `executor` | — (воркер) | Читает outbox, вызывает коннекторы |
| `sweeper` | — (воркер) | Истечение сроков, просроченные lease |
| `notifier` | — (воркер) | Бот, webhooks |

Все воркеры идемпотентны и могут работать в нескольких экземплярах: координация только через БД (`FOR UPDATE SKIP LOCKED`).

## Модель данных

DDL — ориентир; имена и типы обязательны, индексы и ограничения — тоже.

```sql
CREATE TABLE intents (
  intent_id            uuid PRIMARY KEY,
  tenant_id            text NOT NULL,
  requester_principal  text NOT NULL,
  business_task_id     text,
  tool_key             text NOT NULL,
  tool_version         text NOT NULL,
  connector_id         text NOT NULL,          -- aud execution token'а
  execute_tool         text NOT NULL,          -- executes_via из manifest
  connection_id        text NOT NULL,
  resource_id          text NOT NULL,
  effect_class         text NOT NULL CHECK (effect_class IN ('write_reversible','external_effect','high_risk')),
  canonical_args_hash  char(64) NOT NULL,
  args_ciphertext      bytea,                  -- NULL после очистки (RET-01)
  preview_ciphertext   bytea,
  idempotency_key      text NOT NULL,
  policy_version       text NOT NULL,
  rule_ids             text[] NOT NULL DEFAULT '{}',  -- все совпавшие правила, отсортированы
  approval_mode        text NOT NULL CHECK (approval_mode IN ('human','auto','not_required')),
  required_approvals   smallint NOT NULL CHECK (required_approvals BETWEEN 0 AND 2),
  step_up_required     boolean NOT NULL,
  allowed_approvers    text[],                 -- NULL = любой approver tenant'а
  status               text NOT NULL,          -- см. «Переходы»
  version              integer NOT NULL DEFAULT 1,
  attempts             smallint NOT NULL DEFAULT 0,  -- число попыток, дошедших до dispatch
  max_attempts         smallint NOT NULL DEFAULT 3,
  created_at           timestamptz NOT NULL,
  expires_at           timestamptz NOT NULL,   -- срок решения
  approved_at          timestamptz,
  execution_deadline   timestamptz,
  terminal_at          timestamptz,
  payload_purged_at    timestamptz,
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX ON intents (tenant_id, status, expires_at);
CREATE INDEX ON intents (tenant_id, requester_principal, created_at);

CREATE TABLE decisions (
  decision_id      uuid PRIMARY KEY,
  intent_id        uuid NOT NULL REFERENCES intents,
  tenant_id        text NOT NULL,
  decided_by       text NOT NULL,              -- oidc:… или policy:<rule>@<ver>
  decision         text NOT NULL CHECK (decision IN ('approve','reject')),
  intent_version   integer NOT NULL,
  args_hash        char(64) NOT NULL,
  step_up          boolean NOT NULL DEFAULT false,
  comment          text,
  decided_at       timestamptz NOT NULL,
  UNIQUE (intent_id, decided_by)              -- один голос на человека
);

CREATE TABLE executions (
  execution_id          uuid PRIMARY KEY,      -- = jti
  intent_id             uuid NOT NULL REFERENCES intents,
  attempt               smallint NOT NULL,     -- порядковый номер строки исполнения для intent'а
  state                 text NOT NULL CHECK (state IN ('reserved','dispatching','succeeded',
                          'failed_before_effect','outcome_unknown','reconciled_executed',
                          'reconciled_not_executed','released')),
  reserved_by           text NOT NULL,         -- экземпляр executor'а
  reserved_at           timestamptz NOT NULL,
  reserve_lease_until   timestamptz NOT NULL,
  dispatched_at         timestamptz,
  dispatch_lease_until  timestamptz,
  completed_at          timestamptz,
  provider_operation_id text,
  provider_receipt_ref  text,
  error_code            text,
  UNIQUE (intent_id, attempt)
);
-- не больше одного «живого» исполнения на intent
CREATE UNIQUE INDEX one_active_execution ON executions (intent_id)
  WHERE state IN ('reserved','dispatching','outcome_unknown');

CREATE TABLE outbox (
  id bigserial PRIMARY KEY, topic text NOT NULL, intent_id uuid, payload jsonb NOT NULL,
  created_at timestamptz NOT NULL, available_at timestamptz NOT NULL,
  attempts integer NOT NULL DEFAULT 0, delivered_at timestamptz
);

CREATE TABLE switches (
  scope text PRIMARY KEY,                     -- 'global' | 'tenant:<id>' | 'tool:<tenant>:<tool_key>'
  blocked boolean NOT NULL, reason text NOT NULL, changed_by text NOT NULL, changed_at timestamptz NOT NULL
);

CREATE TABLE approver_links (
  human_principal text NOT NULL, tenant_id text NOT NULL, telegram_user_id bigint NOT NULL,
  linked_at timestamptz NOT NULL, PRIMARY KEY (human_principal, tenant_id)
);

CREATE TABLE audit_events (                   -- только INSERT; UPDATE/DELETE запрещены правами роли БД
  id bigserial PRIMARY KEY, at timestamptz NOT NULL, actor text NOT NULL, action text NOT NULL,
  tenant_id text, intent_id uuid, from_status text, to_status text, details jsonb
);
```

- **AS-01.** Изменение `intents.status` и связанных строк (`decisions`, `executions`, `outbox`, `audit_events`) — в одной транзакции. Каждое изменение `intents` проверяет и увеличивает `version` (`UPDATE … WHERE intent_id = $1 AND version = $2`).
- **AS-02.** Ни одна роль БД процессов (AS-27) не имеет прав `UPDATE`/`DELETE` на `audit_events` и `DELETE` на `intents`, `decisions`, `executions`. Очистка payload идёт через `UPDATE … SET args_ciphertext = NULL`.

## Статусы intent'а и переходы

Статус intent'а — итог для потребителя. Детали попыток — в `executions`.

| # | Из | Событие | В | Кто | Условия | Побочные эффекты |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | — | propose | `pending_approval` / `approved` | Коннектор | Service JWT; requester есть в tenant'е; коннектор обслуживает tenant; tool найден в bundle по `(iss, tool_key, tool_version)` (AS-26), иначе 403; квоты (AP-05); нет kill switch; хеш совпадает; политика ≠ deny | Outbox: уведомление approver'ам или задача executor'у |
| T2 | `pending_approval` | approve, кворум набран | `approved` | Человек (UI) | Роль `approver` в tenant'е по bundle; в `allowed_approvers`; ≠ requester; step-up, если нужен; `version` и `args_hash` совпадают; `now < expires_at` | `execution_deadline = now + window`; outbox: executor, webhook |
| T3 | `pending_approval` | approve, кворум не набран | `pending_approval` | Человек | Как T2 | Запись решения; `version++` |
| T4 | `pending_approval` | reject | `rejected` | Человек | Как T2, кроме step-up | Webhook |
| T5 | `pending_approval` | срок | `expired` | Sweeper | `now ≥ expires_at` | Webhook |
| T6 | `pending_approval`, `approved` | cancel | `cancelled` | Requester (MCP) или approver | Нет живого исполнения | Webhook |
| T7 | `approved` | reserve | `executing` | Executor | `now < execution_deadline`; нет kill switch; `attempts < max_attempts`; вставка в `executions` (`attempt` = max + 1) прошла уникальный индекс | execution `reserved`, `reserve_lease_until = now + 60 с` |
| T8 | `approved` | срок исполнения | `expired` | Sweeper | `now ≥ execution_deadline` | Webhook |
| T9 | `executing` (exec `reserved`) | dispatch | `executing` (exec `dispatching`) | Коннектор | Service JWT коннектора = `aud` токена; токен валиден; `jti` = execution `reserved`; lease не истёк; нет kill switch | `attempts++`; `dispatch_lease_until = now + dispatch_timeout_s + 30 с` |
| T10 | `executing` (exec `reserved`) | lease истёк без dispatch | `approved` | Sweeper | `now ≥ reserve_lease_until` | exec → `released`; `attempts` не меняется (провайдер не вызывался) |
| T11 | `executing` (exec `dispatching`) | complete: succeeded | `succeeded` | Executor | Ответ коннектора по effect-result | Webhook |
| T12 | `executing` (exec `dispatching`) | complete: failed_before_effect | `approved` / `failed` | Executor | `approved`, если `attempts < max_attempts` и до deadline, иначе `failed` | Webhook при `failed` |
| T12a | `executing` (exec `reserved`) | complete: отказ `execute` до dispatch (401/409/422/423, AS-12) | `approved` / `failed` | Executor | Как T12; попытка **засчитывается** (`attempts++`), exec → `failed_before_effect` | Webhook при `failed` |
| T13 | `executing` (exec `dispatching`) | complete: outcome_unknown, обрыв, таймаут HTTP | `outcome_unknown` | Executor | — | Outbox: reconcile |
| T14 | `executing` (exec `dispatching`) | dispatch lease истёк | `outcome_unknown` | Sweeper | `now ≥ dispatch_lease_until` | Outbox: reconcile |
| T15 | `outcome_unknown` | reconcile: executed | `reconciled_succeeded` | Executor | — | Webhook |
| T16 | `outcome_unknown` | reconcile: not_executed | `reconciled_failed` | Executor | — | Webhook |
| T17 | `outcome_unknown` | reconcile: unknown | `outcome_unknown` | Executor | Повтор reconcile с backoff до 24 ч | Затем — в UI для ручного разбора, alert |
| T18 | `outcome_unknown` | ручное решение | `reconciled_succeeded` / `reconciled_failed` | `approver` или `tenant_admin` | Обязательное обоснование ≥ 10 символов | Webhook |

- **AS-03.** Других переходов нет. Терминальные статусы: `succeeded`, `failed`, `reconciled_succeeded`, `reconciled_failed`, `rejected`, `expired`, `cancelled`.
- **AS-04.** `reconciled_failed` терминален: автоматического повтора нет. Чтобы попробовать снова, бизнес-сервис создаёт новый intent с новым `idempotency_key` (Signals — новая версия кандидата).
- **AS-05.** Kill switch проверяется **внутри** транзакций T1, T7 и T9. Отдельный кеш в коннекторе не нужен: `dispatch` — единственная точка, после которой идёт вызов провайдера. Это заменяет 30-секундный кеш из SEC-20.

## Аутентификация сервисов

- **AS-06. Service JWT.** Каждый сервис (коннектор, executor, ContextForge-фасад) имеет свою пару Ed25519. Каждый внутренний вызов несёт `Authorization: Bearer <jwt>` с `typ: gxg-svc+jwt`, `iss` = ID сервиса, `aud` = ID получателя, `iat`, `exp ≤ iat + 60 с`, `jti`. Получатель проверяет подпись по реестру платформы: у сервиса 1–2 активных ключа (`public_keys`), принимается `kid` любого из них, подписывает сервис новейшим (ротация без простоя: новый ключ добавляется в bundle, затем старый удаляется). Доступ к endpoint'ам определяется видом сервиса (`kind`) по таблице ниже и списком его tenant'ов.

| `kind` | Разрешённые endpoint'ы |
| --- | --- |
| `connector` | `POST /v1/intents`, `POST /v1/executions/{id}/dispatch`, JWKS |
| `executor` | внутренние вызовы БД/outbox; `execute`/`reconcile` коннекторов |
| `approval_mcp` | `GET /v1/intents/{id}`, `POST /v1/intents/{id}/cancel` |
- **AS-06a.** Заголовок service JWT содержит `kid` = JWK thumbprint (RFC 7638, SHA-256, base64url) публичного ключа сервиса `iss` из реестра платформы. Получатель вычисляет thumbprint ключа из реестра и отклоняет токен, если `kid` не совпадает.
- **AS-07.** Транспорт — TLS во внутренней сети. mTLS — SHOULD, когда хостинг это поддерживает (не заменяет service JWT).
- **AS-08. Identity requester'а при propose.** Коннектор передаёт `requester.principal`, `requester.tenant_id` и, если механизм из [ADR-0001](../adr/0001-identity-propagation.md) даёт проверяемую assertion, саму `requester.assertion`. Approval Service:
  - MUST проверить, что principal существует в tenant'е по bundle, а коннектор допущен к этому tenant'у;
  - MUST независимо проверить `assertion`, если механизм её предоставляет;
  - иначе доверяет service JWT коннектора и пишет `identity_mode = connector_attested` в аудит.

## Реестр платформы

Файл `policies/platform/registry.yaml` (схема [platform-registry.schema.json](../../schemas/platform-registry.schema.json), пример [policies/platform/registry.example.yaml](../../policies/platform/registry.example.yaml)) входит в тот же подписанный bundle, что grants и политики:

- `services` — ID сервиса, тип (`connector`, `executor`, `approval_api`, `approval_mcp`), публичный ключ service JWT, обслуживаемые tenant'ы, для коннекторов — внутренний base URL;
- `webhooks` — подписчики событий по tenant'ам (AS-21).

- **AS-26.** `effect_class`, `executes_via` (→ `execute_tool`) и `dispatch_timeout_s` Approval Service берёт **только** из bundle (`platform.services[].tools`, GR-11a), а не из тела запроса коннектора. Неизвестный tool → 403 `permission_denied`.
- **AS-25.** Коннектор, не указанный в реестре для tenant'а, не может создать intent этого tenant'а (T1) и не получит execution token на его intents.

## Ключи

- **AS-09.** JWKS executor'а: `GET /.well-known/gxg-exec-jwks.json` на `api`. Коннекторы кешируют до 10 минут и принимают только `kid` из JWKS.
- **AS-10.** Ротация: новый ключ публикуется в JWKS не меньше чем за 15 минут до первого использования, старый остаётся там 24 часа после последнего использования. При компрометации ключ удаляется из JWKS сразу, все `reserved` исполнения освобождаются (`released`).
- **AS-11.** Допуск расхождения часов — 5 с для `iat`/`exp` всех JWT. Все хосты синхронизированы по NTP. Расхождение > 2 с — alert.

## Внутренний API

Полный машиночитаемый контракт: [approval-service.openapi.yaml](../../schemas/approval-service.openapi.yaml). Ошибки — [error.schema.json](../../schemas/error.schema.json).

| Метод и путь | Вызывает | Тело → ответ | Переходы |
| --- | --- | --- | --- |
| `POST /v1/intents` | Коннектор | [intent-create](../../schemas/intent-create.schema.json) → 201 (новый) / 200 (существующий) [intent-status](../../schemas/intent-status.schema.json) | T1 |
| `GET /v1/intents/{id}?tenant_id=&principal=` | MCP-фасад | → intent-status | — |
| `POST /v1/intents/{id}/cancel` | MCP-фасад от имени requester'а | `{on_behalf_of: {principal, tenant_id}, expected_version}` → intent-status | T6 |
| `POST /v1/executions/{id}/dispatch` | Коннектор | `{execution_token}` → `{dispatch_deadline}` | T9 |
| `GET /.well-known/gxg-exec-jwks.json` | Коннекторы | → JWKS | — |

Коды ответов: 400 `invalid_input`, 401 `unauthenticated`, 403 `permission_denied`, 404 `not_found` (в том числе чужой tenant), 409 `conflict` (версия, idempotency, состояние), 423 `effect_disabled`, 429 `rate_limited`.

### Контракт коннектора для Executor'а

| Метод и путь (на коннекторе) | Тело → ответ |
| --- | --- |
| `POST /internal/v1/execute/{execute_tool}` | Service JWT executor'а + [execute-request](../../schemas/execute-request.schema.json) → 200 [effect-result](../../schemas/effect-result.schema.json) |
| `POST /internal/v1/reconcile/{execute_tool}` | Service JWT executor'а + `{intent_id, execution_id, idempotency_key, provider_operation_id?}` → 200 [reconcile-result](../../schemas/reconcile-result.schema.json) |

- **AS-11a.** Для вызывающего вида `approval_mcp` `on_behalf_of` = `{principal, tenant_id}` **обязателен** на чтении и отмене (берётся из проверенной identity агента, SEC-03). Сервис фильтрует `WHERE tenant_id = $t AND requester_principal = $p`; чужой или несуществующий intent → 404, ответы побайтно одинаковы. UI работает в процессе сервиса и в этот API не ходит.
- **AS-12.** Ответы `execute` до `dispatch`: 401 (токен), 409 (`dispatch` отклонён), 422 (хеш args не совпал), 423 (kill switch). Это `failed_before_effect` без вызова провайдера, переход T12a: попытка засчитывается, поэтому повторяющийся отказ упирается в `max_attempts` и даёт `failed`, а не цикл до `expired`. Любая ошибка **после** успешного `dispatch`, включая исключение коннектора, — `outcome_unknown`.
- **AS-13.** HTTP-таймаут executor'а на `execute` = `dispatch_timeout_s` tool'а (по умолчанию 60 с). По таймауту — T13.
- **AS-14.** Executor не повторяет `execute` с тем же `execution_id`. Повтор после `failed_before_effect` — это новая попытка (T12 → T7).

## MCP-фасад

Сервер `mcp` публикуется в ContextForge как обычный коннектор с tools:

| Tool | Class | Вход | Выход |
| --- | --- | --- | --- |
| `effects.status` | `read` | `{intent_id}` | intent-status |
| `effects.cancel` | `write_reversible` | `{intent_id}` | intent-status |

Identity проверяется так же, как в любом коннекторе (SEC-03). Агент видит только свои intents (`requester_principal` = он). Чужие и несуществующие — `not_found`.

## Web UI

| Экран | Кто | Что |
| --- | --- | --- |
| Вход | Все | OIDC; сессия 8 ч, idle-таймаут 30 мин |
| Ожидают решения | `approver` | Свой tenant (или несколько, если человек approver в нескольких), сортировка по сроку |
| Карточка intent'а | `approver`, `viewer` | Preview (EFF-30…33), канонические args (раскрываемый блок), requester, `business_task_id`, правило политики, срок, история решений |
| Решение | `approver` | Approve / Reject, комментарий; step-up при необходимости; POST с CSRF-токеном, `version` и `args_hash` в форме |
| Неясный исход | `approver`, `tenant_admin` | Intents в `outcome_unknown` после исчерпания reconcile; ручное решение T18 |
| Привязка Telegram | `approver` | Одноразовый код (6 цифр, 10 мин), отправляется боту командой `/link <code>` |
| История | `approver`, `viewer` | Терминальные intents (без payload после очистки) |

- **AS-15.** Cookie: `HttpOnly`, `Secure`, `SameSite=Strict`, `__Host-` prefix. CSP: `default-src 'self'`, без inline-скриптов. Все значения из preview выводятся с экранированием.
- **AS-16.** Step-up: WebAuthn-assertion не старше 5 минут, привязанная к конкретному `intent_id` (challenge содержит `intent_id` и `args_hash`).
- **AS-17.** Approver, у которого нет зарегистрированного passkey, не может решать intents со `step_up_required`. В UI это показано явно.

## Notify-бот

- **AS-18.** Работает через long polling. Принимает только `/start` и `/link <code>`, остальные сообщения игнорирует.
- **AS-19.** Сообщение approver'у: название tenant'а, `preview.title`, `preview.target.label`, requester, срок, ссылка `https://<ui>/intents/<id>`. **Без** текста сообщения, сумм и других полей preview: Telegram — внешний сервис.
- **AS-20.** Inline-кнопок решения нет. Кнопка — только URL.

## Webhooks

- **AS-21.** Подписчики (например Signals) описаны в bundle: `tenant`, URL (из allowlist, SEC-15), `secret_ref`. Тело — [intent-event](../../schemas/intent-event.schema.json).
- **AS-22.** Подпись: заголовок `GxG-Signature: t=<unix>,v1=<hex(HMAC-SHA256(secret, t + "." + body))>`. Получатель отвергает `|now − t| > 300 с` и повторный `event_id`.
- **AS-23.** Доставка через outbox: экспоненциальный backoff от 5 с до 1 ч, до 24 ч. Потом — `dead`, alert. Webhook — подсказка: получатель всё равно может опросить `effects.status`.

## Хранение данных

- **RET-01.** `args_ciphertext` и `preview_ciphertext` очищаются через 30 дней после перехода в терминальный статус. Tenant может уменьшить срок до 1 дня или увеличить до 90 дней. `outcome_unknown` не очищается до разрешения.
- **RET-02.** Метаданные intents, decisions, executions и audit хранятся 1 год, затем удаляются заданием с записью в отдельный журнал.
- **RET-03.** Payload шифруется data key tenant'а. Удаление data key при выводе tenant'а делает его payload нечитаемым (crypto-shredding).
- **RET-04.** Бэкапы шифруются и хранятся не дольше 35 дней, чтобы очистка не обходилась восстановлением из старого бэкапа.

## Администрирование

| Что | Где | Кто |
| --- | --- | --- |
| Grants, approver'ы, политики, подписчики webhooks, реестр сервисов | Git → bundle | Оператор через PR (GR-09, GR-10) |
| Kill switch | `gxg-approval switch set|clear --scope … --reason …` → `admin` API | `platform_admin`; для `clear` нужен step-up |
| Ручной разбор `outcome_unknown` | UI | `approver`, `tenant_admin` |
| Ротация ключей | `gxg-approval keys rotate` | `platform_admin` |
| Привязка бота | UI | Сам approver |

- **AS-24.** Admin API доступен только из admin-сети и требует OIDC-токен оператора с ролью `platform_admin`. Каждое действие — в `audit_events` и alert в канал оператора.

## Наблюдаемость

Метрики Prometheus (метки без персональных данных):

| Метрика | Alert |
| --- | --- |
| `gxg_intents_created_total{tenant,tool,mode}` | — |
| `gxg_intents_pending{tenant}` | > 80 % `pending_per_tenant_max` |
| `gxg_approval_latency_seconds{tenant,tool}` | — |
| `gxg_executions_total{tenant,tool,outcome}` | Рост `outcome_unknown` |
| `gxg_intents_outcome_unknown{tenant}` | > 0 дольше 1 ч |
| `gxg_reserve_conflicts_total` | Резкий рост |
| `gxg_token_verify_failures_total{reason}` | > 0 (на стороне коннекторов) |
| `gxg_quota_rejections_total{tenant,requester}` | AP-07 |
| `gxg_switch_blocked{scope}` | Любое изменение |
| `gxg_bundle_age_seconds` | > 13 ч (пропущена плановая переподпись) |

## Развёртывание

- Минимальный вариант: по одному экземпляру каждого процесса и PostgreSQL с ежедневным бэкапом и PITR.
- Все процессы, кроме `ui`, — без внешнего доступа (SEC-13).
- `/healthz` и `/readyz` у каждого процесса. `readyz` проверяет БД, валидный bundle, ключ подписи (для executor'а).

## Изоляция процессов (решение владельца 2026-11-21)

Цель: компрометация процесса, открытого наружу (`ui`, `notifier`), не даёт права исполнить эффект или подделать решение человека.

- **AS-27. Своя роль БД на процесс, минимальные права.** Каждый процесс подключается к PostgreSQL своей ролью; общей роли приложения нет. Права — только из таблицы ниже, всё остальное запрещено (`REVOKE ALL` по умолчанию); владелец схемы и миграции — отдельная роль, не используемая процессами в работе. Переходы, которые процессу не принадлежат, ему физически недоступны. AS-02 действует для всех ролей.

  | Роль | `intents` | `decisions` | `executions` | `outbox` | `switches` | `audit_events` | `approver_links` |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | `gxg_api` | INSERT; SELECT; UPDATE только `status`, `version`, `terminal_at` для T1/T6 | — | — | INSERT | SELECT | INSERT | — |
  | `gxg_mcp` | SELECT (через `api`-функции чтения и отмены) | — | — | — | — | INSERT | — |
  | `gxg_ui` | SELECT; UPDATE только `status`, `version`, `approved_at`, `execution_deadline`, `terminal_at` для T2–T4, T18 | INSERT; SELECT | SELECT | INSERT | SELECT | INSERT | SELECT, INSERT, UPDATE |
  | `gxg_executor` | SELECT; UPDATE только `status`, `version`, `attempts`, `terminal_at` для T7–T17 | SELECT | INSERT; SELECT; UPDATE | SELECT; UPDATE `delivered_at`, `attempts` | SELECT | INSERT | — |
  | `gxg_sweeper` | SELECT; UPDATE `status`, `version`, `terminal_at` для T5, T8, T10, T14 | — | SELECT; UPDATE `state` | INSERT | SELECT | INSERT | — |
  | `gxg_notifier` | — | — | — | SELECT; UPDATE `delivered_at`, `attempts` | — | INSERT | SELECT |
  | `gxg_admin` | — | — | — | INSERT | SELECT, INSERT, UPDATE | INSERT | — |

  Чтение `args_ciphertext`/`preview_ciphertext` разрешено только ролям, которым нужен payload: `gxg_ui` (preview) и `gxg_executor` (args), через column-level privileges. Переходы, где важна согласованность, реализуются функциями `SECURITY DEFINER` с проверкой исходного статуса, а не прямыми UPDATE.
- **AS-28. Свои секреты на процесс.** Каждый секрет доступен ровно тем процессам, которым он нужен; остальные не получают его ни через переменные окружения, ни через смонтированные файлы.

  | Секрет | Процесс |
  | --- | --- |
  | Пароль/сертификат роли БД | только её процесс |
  | Ключ подписи execution token | `executor` |
  | Ключ service JWT | у каждого процесса свой (`api`, `mcp`, `executor`) |
  | Data key / доступ к KMS для расшифровки args | `executor` |
  | Data key для расшифровки preview | `ui` |
  | Шифрование payload при создании intent'а | `api` (только encrypt) |
  | Токен Telegram-бота | `notifier` |
  | Секреты подписи webhooks | `notifier` |
  | OIDC client secret, ключи сессий, WebAuthn RP | `ui` |
  | OIDC оператора для admin | `admin` |
- **AS-29. Отдельные deployable для внешних процессов.** `ui` и `notifier` собираются и разворачиваются отдельными образами (своё дерево зависимостей, свой цикл обновлений и сканирования уязвимостей) и запускаются с отдельными сетевыми политиками: `ui` — входящий HTTPS только из tailnet (SEC-26), исходящие только в БД и OIDC-провайдера; `notifier` — исходящие только в БД, Telegram Bot API и allowlist webhook-получателей, входящих нет. `api`, `mcp`, `executor`, `sweeper`, `admin` — внутренняя сеть, без выхода в интернет, кроме явно нужного (`executor` → коннекторы).
