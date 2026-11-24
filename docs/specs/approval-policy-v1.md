# GxG Hub — политика согласования v1

**Статус:** нормативный формат, не реализован. 2026-11-20.

Определяет, что Approval Service делает с новым intent'ом: **запретить**, **отправить человеку** или **одобрить автоматически**. Также задаёт сроки и лимиты. Базовые умолчания по классам эффектов — в [Эффекты и согласование](effects-and-approval-v1.md#классы-эффектов-и-путь-исполнения). Политика может их только ужесточить, кроме явных правил авто-одобрения для `external_effect`.

## Где хранится

- Файл на tenant: `policies/approval/<tenant_id>.yaml`, схема [approval-policy.schema.json](../../schemas/approval-policy.schema.json), пример [policies/approval/example.yaml](../../policies/approval/example.yaml).
- Политика живёт в Git рядом с grants, проходит тот же CI и попадает в тот же подписанный bundle ([GR-09…GR-14](resource-grants-v1.md#проверки-в-ci)).
- `policy_version` в intent = `bundle_version`; все совпавшие правила записываются в `rule_ids` (отсортированный список).

## Формат

```yaml
tenant: t-alpha
schema_version: 1

defaults:
  approval_ttl: 24h          # pending_approval → expired
  execution_window: 1h       # approved → expired, если не исполнено
  max_attempts: 3            # попыток дойти до dispatch (EFF-23), необязательно; 1…3, по умолчанию 3

quotas:
  propose_per_requester_per_hour: 30
  pending_per_requester_max: 20
  pending_per_tenant_max: 100

rules:
  - id: tg-partner-auto
    description: "Короткие ответы в чат партнёров без согласования"
    match:
      tools: [telegram.message.propose]
      resources: [telegram:tg-main/chat/c-sales-team]
      requesters: [agent:t-alpha:signals-actions]
    decision: auto_approve
    limits: { max_per_hour: 10, max_per_day: 50 }

  - id: crm-two-person
    match:
      tools: [crm.deal.propose_update]
    decision: require_approval
    approvals: 2
    step_up: true
    approvers: [group:finance-approvers]
    approval_ttl: 4h

  - id: no-broadcast
    match:
      tools: [telegram.broadcast.propose]
    decision: deny
```

### Поля правила

| Поле | Смысл |
| --- | --- |
| `match.tools` | Точные имена `propose`-tools |
| `match.effect_classes` | Классы эффектов |
| `match.resources` | RID или маска `…/<type>/*` (правила как в GR-04) |
| `match.requesters` | Агенты или группы из файла grants того же tenant'а |
| `decision` | `deny`, `require_approval`, `auto_approve` |
| `approvals` | 1 или 2 (кворум) |
| `step_up` | Нужна повторная аутентификация approver'а перед решением |
| `approvers` | Сужение круга approver'ов: люди (`oidc:…`) или группы людей из файла grants. Без поля — любой `approver` tenant'а |
| `approval_ttl`, `execution_window` | Переопределение сроков, только в сторону уменьшения |
| `max_attempts` | Сколько попыток исполнения (`dispatch`, T7/T12/T12a) допустимо, прежде чем intent станет `failed`. Целое 1…3, по умолчанию 3, только в сторону уменьшения. Задаётся и в `defaults`, и в правиле; из умолчания и всех совпавших правил берётся **наименьшее** значение. Фиксируется в intent'е при T1 (AP-04) |
| `limits` | Только для `auto_approve`, обязателен и непуст (хотя бы `max_per_hour` или `max_per_day`): сколько авто-одобрений в час и в сутки по этому правилу |

Правило срабатывает, если intent удовлетворяет **всем** указанным условиям `match` (AND); внутри списка достаточно совпадения с одним элементом (OR).

## Вычисление решения

- **AP-01.** Вычисление детерминированное, порядок правил в файле значения не имеет:
  1. Базовое решение по классу: `write_reversible` → `not_required`, `external_effect` → `require_approval` (1 approver), `high_risk` → `require_approval` (1 approver, `step_up: true`).
  2. Собираются все совпавшие правила.
  3. Если есть хоть один `deny`, intent отклоняется при создании: `propose` возвращает `permission_denied`, intent не сохраняется (в аудите — `rule_id`).
  4. Если есть `require_approval`, они объединяются по самому строгому значению: `approvals` = max, `step_up` = OR, `approvers` = пересечение (пустое пересечение → ошибка lint, во время работы → `deny`), сроки = min.
  5. `auto_approve` действует, только если **нет** совпавших `require_approval`, класс = `external_effect` и лимиты правила не исчерпаны. Если совпало несколько `auto_approve`, учитывается каждый лимит.
  6. Если лимит `auto_approve` исчерпан, intent уходит человеку (`require_approval`, 1 approver), а не отклоняется.
- **AP-02.** Для `high_risk` правило `auto_approve` недопустимо: CI отклоняет такой файл, а во время работы оно игнорируется с alert'ом.
- **AP-03.** Лимиты `auto_approve` считаются по intents, одобренным этим правилом, в скользящем окне, атомарно с созданием intent'а (SELECT … FOR UPDATE по счётчику правила или эквивалент).
- **AP-04.** Решение политики записывается в intent: `approval.mode` (`human` / `auto` / `not_required`), `required_approvals`, `step_up`, `rule_ids`, `policy_version`. Позднее изменение политики **не** меняет уже созданные intents. Kill switch и отзыв grants действуют на них при исполнении (EFF-18).

## Квоты на `propose`

Защищают approver'ов от потока заявок и Hub от сломанного или обманутого агента.

- **AP-05.** Превышение `propose_per_requester_per_hour`, `pending_per_requester_max` или `pending_per_tenant_max` → `propose` возвращает `rate_limited` с `retry_after_s`. Intent не создаётся, approver'у ничего не приходит.
- **AP-06.** Умолчания, если `quotas` не заданы: 30 в час на requester'а, 20 ожидающих на requester'а, 100 ожидающих на tenant.
- **AP-07.** Срабатывание квоты пишется в аудит и в метрику. Повторные срабатывания у одного агента (> 3 за час) вызывают alert оператору: это признак зацикливания или prompt injection.

## Защита approver'а от усталости

- **AP-08.** Не больше одного уведомления в боте на intent. Если у approver'а больше 5 новых заявок за 10 минут, бот присылает одну сводку со ссылкой на список.
- **AP-09.** Список в UI отсортирован по сроку истечения. Заявки одного requester'а с одинаковым preview визуально группируются, но каждая решается отдельно (EFF-15: без массового одобрения).

## Lint в CI

Дополнительно к GR-09:
- ссылки в `requesters` и `approvers` существуют в файле grants того же tenant'а;
- в `approvers` только люди с ролью `approver`;
- нет `auto_approve` для tools класса `high_risk` (класс берётся из manifest коннектора);
- `approval_ttl` и `execution_window` не больше умолчаний; `max_attempts` — целое от 1 до 3;
- отчёт diff: появившиеся `auto_approve`, увеличенные лимиты и удалённые `require_approval`/`deny` считаются **расширением** и требуют второго ревьюера (GR-10).
