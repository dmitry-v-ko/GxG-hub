# Предложение: одобрение без подписи каждой заявки (варианты 1 и 3)

**Статус:** черновик правки спек на утверждение владельцем, 2026-11-24. Код начинаем после утверждения.

Решение владельца: варианты 1 (автоодобрение по политике) и 3 (разрешение на серию). Всё, что не подходит ни под правило, ни под разрешение, по-прежнему подписывается passkey (AS-30).

## Что уже есть

Вариант 1 частично есть в спеке и коде:
- `decision: auto_approve` с лимитами `max_per_hour` / `max_per_day` (AP-01…AP-04, тест EX-18);
- запись `decided_by = policy:<rule>@<ver>`.

Не хватает трёх вещей:
- ограничений на текст;
- обязательного суточного лимита;
- правки AS-30, которая сейчас требует подпись на каждое решение.

Вариант 3 новый.

---

## Правка 1. `docs/specs/approval-policy-v1.md`

### 1a. Новое условие `match.args` — в таблицу «Поля правила»

| Поле | Смысл |
| --- | --- |
| `match.args` | Ограничения на канонические args intent'а. Ключ — имя поля верхнего уровня (например, `text`). Значение — набор условий, нужны все: `max_length` (символы Unicode), `pattern` (регулярное выражение RE2, должно совпасть **целиком**, без обратных ссылок), `enum` (список допустимых значений). Поля нет в args или условие не выполнено → правило не совпало. |

Пример для пилота:

```yaml
  - id: tg-agents-auto
    description: "Короткие сообщения агента пилота в «GxG Agents» без подписи"
    match:
      tools: [telegram.message.propose]
      resources: [telegram:tg-pilot/chat/<chat_ref «GxG Agents»>]
      requesters: [agent:t-pilot:pilot]
      args:
        text: { max_length: 500 }
    decision: auto_approve
    limits: { max_per_day: 20 }
```

### 1b. В строке `limits` таблицы: суточный лимит обязателен

Было: «обязателен и непуст (хотя бы `max_per_hour` или `max_per_day`)».

Стало: «обязателен; `max_per_day` обязателен всегда, `max_per_hour` — по желанию».

### 1c. В `match.resources` для `auto_approve` маски запрещены

Новый пункт:

- **AP-02a.** В правиле `auto_approve` `match.resources` обязателен. В нём только точные RID, маска `…/*` не допускается. `match.requesters` тоже обязателен. Lint отклоняет нарушение. Так авто-одобрение всегда перечисляет чаты явно, и новый чат не попадёт под правило незаметно.

### 1d. Новый раздел после «Вычисление решения»: «Разрешение на серию действий»

- **AP-10.** Правило `require_approval` может разрешить **разрешение на серию** (standing approval). Для этого в правило добавляются поля:
  - `standing: { max_ttl: 7d, max_uses: 200, max_per_day: 50 }`.

  Без поля `standing` разрешение на intents этого правила не действует. Политика в Git задаёт потолки, а человек внутри них выбирает конкретные значения.
- **AP-11.** Разрешение действует только при трёх условиях:
  - класс `external_effect`;
  - итог AP-01 — `require_approval` с `approvals = 1` и `step_up = false`;
  - создатель разрешения входит в `allowed_approvers` intent'а.

  Для `high_risk`, кворума 2 и `step_up` разрешение недопустимо: lint отклоняет `standing` в таких правилах, а во время работы оно игнорируется с alert'ом.
- **AP-12.** Если совпало правило `auto_approve` с неисчерпанным лимитом, intent одобряет оно, и разрешение не расходуется. Порядок: `deny`, затем `auto_approve` (если нет `require_approval`), затем разрешение, затем человек.
- Lint и diff: появление `standing` или увеличение его потолков — это **расширение**, нужен второй ревьюер (GR-10).

---

## Правка 2. `docs/specs/approval-service-v1.md`

### 2a. AS-30 — новая первая фраза

Было: «Каждое решение (approve и reject, не только `high_risk`) сопровождается WebAuthn-assertion…»

Стало:

> **AS-30. Решение человека подписано, проверяет его не `ui`.** Каждое решение **человека** (approve и reject) сопровождается WebAuthn-assertion… *(дальше текст без изменений)*. Без решения человека intent одобряется только двумя способами:
> - правилом политики `auto_approve` (AP-01, AP-02a), `decided_by = policy:<rule>@<ver>`;
> - действующим разрешением на серию (AS-31), `decided_by = standing:<standing_id>`.
>
> Само разрешение создаётся только подписанным решением человека через `decider`. Других способов одобрить intent нет.

### 2b. Новый пункт AS-31

- **AS-31. Разрешение на серию** (решение владельца 2026-11-24).
  1. **Создание.** Approver в UI указывает поля разрешения:
     - агента (один `requester`);
     - tools;
     - ресурсы (точные RID, без масок);
     - ограничения `args` в формате `match.args`;
     - срок;
     - `max_uses`;
     - необязательный `max_per_day`.

     Значения не выше потолков `standing` правила (AP-10). UI показывает, что именно подписывается. Challenge = SHA-256(канонический JSON разрешения ‖ nonce). Подпись проверяет `decider`, как в AS-30. Запись создаёт `gxg_grant_standing` (`SECURITY DEFINER`, `EXECUTE` только у `gxg_decider`). Assertion хранится в записи разрешения.
  2. **Применение.** При T1, если итог AP-01 — `require_approval` и выполнено AP-11, Approval Service ищет действующее разрешение. Оно должно совпадать по requester, tool, RID и `args`, быть не истёкшим и не отозванным, с неисчерпанными лимитами. Поиск, проверка и списание одного использования идут **в одной транзакции** с созданием intent'а (`SELECT … FOR UPDATE` по записи разрешения). Intent сразу переходит в `approved`, `approval_mode = 'standing'`, в `decisions` — `decided_by = standing:<id>`. Уведомление в бот на такой intent не отправляется.
  3. **Проверка при каждом использовании.** Разрешение проверяется по **текущей** политике. Если в новом bundle у правила нет `standing` или потолки ниже значений разрешения, разрешение не применяется, и intent уходит человеку. Grants и kill switch действуют как обычно (EFF-18).
  4. **Конец действия.** Когда истёк срок или исчерпан `max_uses`, следующие intents идут обычным путём, к человеку. Исчерпанный `max_per_day` — то же до конца суток.
  5. **Отзыв.** Отозвать разрешение могут:
     - создатель или любой approver tenant'а — кнопкой в UI, без passkey (отзыв только сужает права);
     - оператор — в `admin`.

     Отзыв атомарен. Intents, одобренные этим разрешением и ещё не зарезервированные executor'ом, переходят в `cancelled`.
  6. **Журнал.** Каждый intent хранит ссылку на разрешение. В UI у разрешения есть страница: кто подписал и когда, поля, сколько использовано, список intents. У intent'а указано, каким правилом или разрешением он одобрен.

### 2c. Модель данных

- `intents.approval_mode`: добавить значение `'standing'`.
- Новая таблица:

```sql
CREATE TABLE standing_approvals (
  standing_id      uuid PRIMARY KEY,
  tenant_id        text NOT NULL,
  requester        text NOT NULL,
  tools            text[] NOT NULL,
  resources        text[] NOT NULL,              -- точные RID
  args_constraints jsonb NOT NULL DEFAULT '{}',  -- формат match.args
  max_uses         integer NOT NULL CHECK (max_uses > 0),
  max_per_day      integer CHECK (max_per_day > 0),
  used             integer NOT NULL DEFAULT 0,
  valid_from       timestamptz NOT NULL,
  expires_at       timestamptz NOT NULL,
  created_by       text NOT NULL,                -- oidc:…
  rule_id          text NOT NULL,                -- правило с `standing`, под которым создано
  policy_version   text NOT NULL,
  assertion        jsonb NOT NULL,               -- authenticatorData, clientDataJSON, signature, credential_id
  revoked_at       timestamptz,
  revoked_by       text
);
```

- `intents.standing_id uuid REFERENCES standing_approvals` — `NULL`, если intent одобрен не по разрешению.
- Счётчик за сутки — по `intents` с этим `standing_id` за скользящие 24 ч, в той же блокировке.

### 2d. Строка «Вход» в таблице доступа

Вместо «каждое решение подписывается passkey, AS-30» — «каждое решение человека и каждое разрешение на серию подписываются passkey, AS-30, AS-31».

---

## Правка 3. `docs/specs/effects-and-approval-v1.md`

- **EFF-15**, пункт «массового одобрения… нет».

  Стало: «массового одобрения уже созданных заявок нет. Одобрение без человека — только правилом `auto_approve` или разрешением на серию (AS-31), и только для `external_effect`».
- В таблице классов, строка `external_effect`, — добавить: «или правило `auto_approve`, или разрешение на серию (AS-31)».

Строка `approval-policy-v1.md` AP-09 ссылается на EFF-15 и остаётся верной.

---

## Правка 4. `docs/specs/conformance-matrix-v1.md`

| ID | Этап | Сценарий | Ожидаемо | Требования |
| --- | --- | --- | --- | --- |
| EX-26 | C | `auto_approve` с `match.args`: текст в пределах `max_length` и `pattern`; длиннее; не по шаблону; поля нет; исчерпан `max_per_day` | В пределах → `approved` без человека, `decided_by = policy:…`; иначе → к человеку | AP-01, AP-02a, AP-03 |
| EX-27 | C | Lint: `auto_approve` без `max_per_day`, с маской в `resources`, без `requesters`; `standing` в правиле с `step_up`, кворумом 2 или для `high_risk` | Lint отклоняет каждый случай | AP-02a, AP-10, AP-11 |
| EX-28 | C | Разрешение: создание без assertion, с assertion на другой JSON, выше потолков, не approver'ом | Каждое отклонено, записи нет | AS-30, AS-31 |
| EX-29 | C | Разрешение действует. Варианты: intent в его рамках; чужой чат; чужой агент; args вне ограничений; после срока; после `max_uses`; после отзыва; после bundle без `standing`; 2 параллельных propose при последнем использовании | В рамках → `approved` без человека, `decided_by = standing:…`. Остальное → к человеку. Параллельно — ровно одно одобрение по разрешению | AS-31 |
| EX-30 | C | Отзыв, когда одобренный по разрешению intent ещё не зарезервирован executor'ом | Intent `cancelled`, вызова провайдера нет | AS-31 п.5, EFF-16 |
| EX-31 | C | `ui` без `decider` пытается создать разрешение или одобрить intent | Отказ, `gxg_ui` не имеет `EXECUTE` | AS-30, AS-31 |

## Вопросы владельцу

1. Потолки разрешения для пилота: срок до 7 дней, до 200 отправок, до 50 в сутки?
2. Отзыв без passkey (отзыв только сужает права) — согласны?
3. Ограничения правила 1 для «GxG Agents»: до 500 символов, до 20 в сутки?
