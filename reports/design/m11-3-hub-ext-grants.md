# M11.3, часть Hub: права `telegram.ext:read`, включение записей реестра, virtual servers, аудит

**Статус:** дизайн, код и политики не написаны. Ничего не применено к спекам: правки перечислены в разделе 1.5 как «было → стало». Ветка `claude/m11-3-hub-design-bdead6`, основа `40e95a0` (MCC-11 уже в спеках). Пометка «не проверено» значит: в файлах репозитория подтверждения нет. Ссылки `файл:строка` даны на этом commit.

Источники: [mcp-connector-contract-v1.md](../../docs/specs/mcp-connector-contract-v1.md) (MCC-11, строки 102–117), [service-integration-v1.md](../../docs/specs/service-integration-v1.md):40–42, [resource-grants-v1.md](../../docs/specs/resource-grants-v1.md), [security-v1.md](../../docs/specs/security-v1.md), ADR-0001/0002/0005, [resource-grants.schema.json](../../schemas/resource-grants.schema.json), [grants.py](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py), [tools/gxg-policy](../../tools/gxg-policy/src/gxg_policy), [stand/](../../stand), [gateway/contextforge/gxg_identity](../../gateway/contextforge/gxg_identity), [reports/conformance/M5.json](../conformance/M5.json). План: `docs/implementation-plan-v1.md`, разделы M11.2 (строка 607) и M11.3 (строка 619) в `/Users/dk1/go/GxG-hub-mcc11`. На ветке нет `docs/implementation-plan-v1.md` версии с M11.2/M11.3: читал ту копию.

## Итог

1. Текущая модель grants **не выражает** `telegram.ext:read`: `actions` — закрытый enum `read|collect|write|propose` ([resource-grants.schema.json:78-81](../../schemas/resource-grants.schema.json)), а `GrantResolver` сравнивает действие с множеством по равенству ([grants.py:308](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py)). Нужна правка схемы, спеки и резолвера (M11.2, уже запланирована, ждёт O-1).
2. Всё остальное в grants **уже работает без изменений**: RID чата, маски `chat/*`, `expires_at`, `grant_id` в аудите, `diff` расширения прав, подписанный bundle.
3. Включение записи для tenant'а — новый блок `extensions` в файле tenant'а. Он едет в bundle без изменения bundle-схемы, потому что та ссылается на схему grants ([grants-bundle.schema.json:42](../../schemas/grants-bundle.schema.json)).
4. ContextForge v1.1.1 видит только три tool'а `telegram.ext.*` и различает их по имени. Внутрь `ext.call` он не смотрит: проверено только сопоставление «email агента × имя tool». Права и включение по записи проверяет коннектор. **Форк не нужен.**
5. Имя записи в аудите пишет коннектор (одна строка на вызов, поле через `fields`, [mcp.py:371](../../packages/gxg-connector-kit/src/gxg_connector_kit/mcp.py)). Имя записи привязано подписью к вызову (`ahs`). Аудит ContextForge с именем записи не подтверждён: MC-10 строим на аудите коннектора.

## 1. Как выразить `telegram.ext:read` в текущей модели grants

### 1.1 Что уже умеет

| Возможность | Где подтверждено |
| --- | --- |
| Чаты как RID `telegram:<conn>/chat/<ref>`, точные или маска `chat/*` | GR-04, [resource-grants-v1.md:87](../../docs/specs/resource-grants-v1.md); `_RID`, `_MASK`, [grants.py:37-38](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py); пилот [t-pilot.yaml](../../policies/pilot/tenants/t-pilot.yaml) |
| Только разрешения, срок `expires_at`, `grant_id` в аудите | GR-06, GR-07; [grants.py:306-309, 370](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py) |
| Чужой tenant и чужой connection → отказ | GR-02; [grants.py:319](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py) |
| Нет права на названный ресурс → `not_found` (GR-17), поиск сначала по `allowed()` (GR-16) | [resource-grants-v1.md:135-136](../../docs/specs/resource-grants-v1.md); `allowed()` [grants.py:326-334](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py) |
| `gxg-policy diff` считает тройки (subject, action, resource) и помечает расширение прав; строки действий он не разбирает | [diff.py:37-54](../../tools/gxg-policy/src/gxg_policy/diff.py) |
| Bundle несёт файл tenant'а целиком | GR-11a; [grants-bundle.schema.json:42](../../schemas/grants-bundle.schema.json) |

### 1.2 Чего не умеет

- **Нет действия «группа расширений».** Enum действий закрыт, `Action` в ките — `Literal` из четырёх значений ([grants.py:34](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py)). Грант `read` на чат **не** должен давать `ext:read`: иначе все записи реестра открываются любому, кто читает чат (MCC-11 п.5 запрещает «грант на всё»).
- **Нет иерархии действий.** `ext:read` должен покрывать `ext:read:members`, обратное неверно. Резолвер сравнивает по равенству.
- **Нет «включения записи для tenant'а».** Схема файла tenant'а закрыта (`additionalProperties: false`, [resource-grants.schema.json:7](../../schemas/resource-grants.schema.json)), места для списка записей нет.
- **Манифест** тоже закрывает `action` теми же четырьмя значениями ([gxg-manifest.schema.json:61-67](../../schemas/gxg-manifest.schema.json)). Имя `telegram.ext.call` ему подходит (три сегмента, шаблон на строке 44), класс `read` тоже.
- **Ресурс записи не на чате** (например, информация о пользователе): тип RID и правило определения не заданы. Не проверено, нужен ли такой тип в стартовом наборе (O-2); см. вопрос Q2.

### 1.3 Предлагаемая форма

Право — новое действие в `actions`, домен берётся из RID ресурса (поэтому в действии нет `telegram.`):

```text
ext:read                 группа «все записи класса read»
ext:read:<подгруппа>     например ext:read:members, ext:read:history, ext:read:info
```

`telegram.ext:read` из MCC-11 остаётся человеческим обозначением в тексте спеки и в отчётах; в YAML пишется `actions: [ext:read]` на RID `telegram:…`.

Правила:

- Шаблон действия: `^ext:read(:[a-z][a-z0-9_]*)?$`. Голое `ext`, `ext:` и классы кроме `read` схема отклоняет (в v1 расширения только `read`). Это закрывает AC-M11.2-03 «грант на `ext` без класса отклоняется».
- Сопоставление в `GrantResolver`: грант `ext:read` покрывает запрошенное `ext:read` и `ext:read:<любая>`; грант `ext:read:members` покрывает только `ext:read:members`. Грант `read`, `collect`, `write`, `propose` на `ext:*` не распространяется и наоборот.
- Запись реестра несёт `action: ext:read:<подгруппа>` и тип ресурса (в MCC-11 п.2 это поля записи). Коннектор зовёт `check(tenant, principal, "ext:read:members", rid)`. `check` и `allowed` остаются теми же методами; меняются только тип `Action` и условие на [grants.py:308](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py).
- Маска `telegram:tg-main/chat/*` с `ext:read` допустима по GR-04, но это самый широкий вариант. Предлагаю lint-предупреждение, а не запрет (раздел 5).
- Права по-прежнему allow-only. «Выключить одну запись» грантами нельзя: для этого есть включение (раздел 2).

Пример гранта:

```yaml
grants:
  - id: tg-hermes-ext-info
    subjects: [agent:t-alpha:hermes]
    actions: [ext:read:info, ext:read:history]
    resources:
      - telegram:tg-main/chat/c-sales-team
    expires_at: "2027-03-01T00:00:00Z"
    reason: "Пилот: справочное чтение чата партнёров"
```

### 1.4 Как это проверяет коннектор (порядок для `ext.call`)

Порядок выведен из MCC-11 п.3–5 и GR-17; пункты 3–5 требуют подтверждения владельца (Q6).

| Шаг | Проверка | Отказ |
| --- | --- | --- |
| 1 | Identity по JWS, `tool` и `ahs` совпадают с вызовом (MCC-02, SEC-03) | `unauthenticated` |
| 2 | Имя записи есть в реестре коннектора | `not_found` |
| 3 | Класс записи `read` | `permission_denied` с указанием на `propose`-tool |
| 4 | Запись включена для tenant'а в bundle (раздел 2) | `effect_disabled` |
| 5 | Ресурс определён по данным backend'а (GR-01), `check` на `ext:read[:подгруппа]` | `not_found` (GR-17), чужой tenant — `not_found` (GR-02) |
| 6 | Лимиты записи, провайдер, ответ по схеме записи | `invalid_input`, `rate_limited` и т.д. |

### 1.5 Предлагаемые правки спек (не применены)

| # | Файл | Было | Стало |
| --- | --- | --- | --- |
| 1 | resource-grants-v1.md, таблица «Действия» (строки 35-40) | четыре действия: `read`, `collect`, `write`, `propose` | добавлена строка: `ext:read[:<подгруппа>]` — вызов записей реестра класса `read` через `<domain>.ext.call`; не входит в `read`; `ext:read` покрывает все подгруппы, подгруппа — только себя |
| 2 | там же, строка 42 «Действие tool'а задаётся в manifest» | действие всегда из manifest tool'а | «…; для `ext.call` действие задаёт запись реестра (`gxg-capabilities.yaml`), manifest содержит `ext:read`» |
| 3 | resource-grants-v1.md, «Формат файла tenant'а» | нет блока включения | добавлен блок `extensions` и новое правило GR-19 (ниже) |
| 4 | resource-grants-v1.md, GR-04 | маски `…/<type>/*` допустимы для любых действий | без изменения текста; добавлено примечание: для `ext:*` маска даёт lint-предупреждение `ext-mask-wide` |
| 5 | resource-grants-v1.md, новое GR-19 | отсутствует | «Включение записи реестра: `extensions[].entries` перечисляет записи, которые коннектор вправе исполнять для tenant'а. Запись исполняется, только если она включена **и** у principal'а есть `ext:read[:подгруппа]` на ресурс. Включение не выдаёт прав. Выданное право не включает запись» |
| 6 | resource-grants.schema.json, `actions.items` (строки 78-81) | `{"enum": ["read","collect","write","propose"]}` | `{"anyOf": [{"enum": [...те же...]}, {"pattern": "^ext:read(:[a-z][a-z0-9_]*)?$"}]}` |
| 7 | resource-grants.schema.json, верхний уровень | свойства: `tenant`, `schema_version`, `connections`, `principals`, `groups`, `grants` | добавлено `extensions` (форма в разделе 2) |
| 8 | grants.py `Action` и `_candidates` | `Action = Literal[...4]`, `action in g.actions` | `Action = str`, проверка разобранным предикатом «покрывает» (иерархия `ext:read` ⊇ `ext:read:x`) |
| 9 | resource-grants-v1.md, GR-11a | `tenants.{id}.grants` — файл grants | то же, плюс упоминание: `extensions` едет внутри; `platform.<connector>.extensions` — список `{name, version, effect_class, action}` из реестра (см. Q5) |
| 10 | gxg-manifest.schema.json `tools[].action` (строки 61-67) | enum из четырёх | enum плюс шаблон `^ext:read$`, применимый только к `<domain>.ext.call/describe/search` |
| 11 | mcp-connector-contract-v1.md, словарь, строка 37 | `effect_disabled`: «Эффект выключен: kill switch, tool не включён» | «…; запись реестра не включена для tenant'а (MCC-11 п.7г)» |
| 12 | mcp-connector-contract-v1.md, MCC-11 п.5 (строка 114) | «`telegram.ext:read`, `telegram.ext:read:members`» | «в grants — действие `ext:read[:подгруппа]` на RID коннекта; запись включается блоком `extensions` (GR-19)» |
| 13 | service-integration-v1.md, строка 42 | «Права (`telegram.ext:read`, allowlist чатов, включение записи для tenant'а) выдаёт Hub через политики» | добавлено: «файл `policies/tenants/<tenant>.yaml`: `grants` (`ext:read…`, RID чатов) и `extensions`» |
| 14 | security-v1.md, SEC-20 | «отключение эффектов по tenant и по tool» | дополнить: запись реестра отключается новым bundle (GR-13, до 5 мин); экстренно — снятием tool'а `telegram.ext.call` в ContextForge целиком (Q4) |
| 15 | ADR-0005, раздел «Доступ» | четыре пункта (видимость, чаты, запись, Mastra) | пятый: чтение расширений — `ext:read` + включение записи; запись через `ext.call` не исполняется |
| 16 | conformance-matrix-v1.md, MC-09 | «грант на `ext` без указания класса» | без изменения; добавить кейс «`ext:write_reversible` в grants → отклоняется схемой» |

## 2. Включение записи реестра для tenant'а

### 2.1 Где

В файле tenant'а, рядом с `grants`, потому что:

- файл уже под ревью и `gxg-policy diff`/GR-10 (расширение прав требует второго ревьюера);
- bundle (GR-11a) уже несёт файл целиком, коннектор берёт свой tenant-срез (GR-12);
- отдельного хранилища «включений» не появляется.

`enabled_default: false` живёт в реестре коннектора (MCC-11 п.2). Включение — это строка в `extensions`. Отдельного поля «выключить» нет: нет строки — запись выключена.

### 2.2 Форма

```yaml
extensions:
  - id: tg-ext-info-v1
    connection: tg-main              # connection этого tenant'а с domain: telegram
    entries:                         # имя@мажорная версия записи реестра
      - telegram.chat.get_info@1
      - telegram.chat.list_members@1
      - telegram.chat.list_pinned@1
    expires_at: "2027-03-01T00:00:00Z"   # необязательно
    reason: "Пилот: справочное чтение, O-2 одобрен 2026-12-01"
```

Схема блока (предлагаемая): `id` по шаблону `$defs/id`; `connection` — id из `connections`; `entries` — непустой уникальный массив строк `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2}@[0-9]+$`; `expires_at`, `reason` как у grants. `additionalProperties: false`.

Версия закреплена по мажору. Новая мажорная версия записи (другая схема или класс) **не включена**, пока PR не поменяет `@N`. Это сохраняет принцип MCC-11 п.5: «новая запись включается отдельным решением».

Имена записей в примере условные: стартовый набор — решение владельца O-2. Ни реестра, ни имён в репозитории нет (не проверено).

### 2.3 Как включённая запись доходит до коннектора

1. PR меняет `policies/tenants/<tenant>.yaml`. CI: validate, lint, diff (GR-09). Новая запись в `extensions` — расширение прав, нужен второй ревьюер (GR-10).
2. После мержа CI собирает и подписывает bundle (GR-11), коннектор подхватывает его за ≤ 5 мин (GR-13).
3. Коннектор при проверке (раздел 1.4, шаг 4) ищет `connection` вызова в `extensions[]` своего tenant-среза и имя записи в `entries`. Не нашёл — `effect_disabled`.
4. AC-M11.3-03: после merge нового bundle запись работает, соседний tenant не затронут. Срез tenant'а делает `load_bundle(..., tenant=…)` ([grants.py:132](../../packages/gxg-connector-kit/src/gxg_connector_kit/grants.py)); изоляцию проверяет тест «чужой tenant» в AC-M11.2-03.

### 2.4 Пример: tenant целиком

```yaml
tenant: t-alpha
schema_version: 1
connections:
  - {id: tg-main, domain: telegram, credential_ref: "secret://t-alpha/telegram/main"}
principals:
  agents:
    - id: agent:t-alpha:hermes
    - id: agent:t-alpha:analyst
groups:
  researchers: [agent:t-alpha:hermes, agent:t-alpha:analyst]
grants:
  - id: tg-researchers-ext-read
    subjects: [group:researchers]
    actions: [ext:read:info, ext:read:history]
    resources: [telegram:tg-main/chat/c-sales-team]
    expires_at: "2027-03-01T00:00:00Z"
    reason: "Справочное чтение расширениями"
  - id: tg-hermes-ext-members
    subjects: [agent:t-alpha:hermes]
    actions: [ext:read:members]
    resources: [telegram:tg-main/chat/c-sales-team]
    reason: "Состав участников нужен только Hermes"
extensions:
  - id: tg-ext-info-v1
    connection: tg-main
    entries: [telegram.chat.get_info@1, telegram.messages.get_history@1]
    reason: "Включены только info и history; members не включена: вызов даст effect_disabled"
```

Здесь у Hermes `ext:read:members` выдано, но запись не включена: ожидаемый результат `effect_disabled` (AC-M11.3-03).

### 2.5 Альтернативы и почему не они

- **Поле `enabled_tenants` в самом реестре.** Реестр — в репозитории `gxg-telegram-service` (ADR-0005), включение там идёт мимо политик Hub и мимо GR-10.
- **Включение через grants** (специальное действие `ext:enable`). Смешивает «кто может» и «что включено». Один RID не определяет запись. Отклонено.
- **Включение на уровне principal'а.** Слишком мелко для v1; MCC-11 говорит «для tenant'а». Вопрос Q3.

## 3. ContextForge v1.1.1: virtual servers и граница возможностей

### 3.1 Что публикуем

В ContextForge регистрируются **три tool'а**: `telegram.ext.search`, `telegram.ext.describe`, `telegram.ext.call`, плюс ядро M11.1. Записи реестра в ContextForge **не регистрируются**: это аргументы `ext.call` (`name`, `arguments`). Практический вывод: новая запись не требует ни регистрации в шлюзе, ни правки правил ContextForge, ни его перезапуска. Меняется только bundle.

Имя tool'а в ContextForge строится как `<gateway>-<tool>` с точками → дефисами: на стенде `telegram-t-alpha-telegram-messages-search` ([unified_pdp.rules.json:16-18](../../stand/unified_pdp.rules.json)). Для расширений будет `telegram-<tenant>-telegram-ext-{search,describe,call}`; точное имя после регистрации — не проверено.

### 3.2 Virtual servers по ролям

Как сейчас устроено на стенде: gateway `telegram-<tenant>` (visibility `team`) и virtual server `vs-tg-<tenant>` с `associated_tools` = все tools этого gateway ([stand/bootstrap.py:143-181](../../stand/bootstrap.py)). Virtual server не граница доступа: ограничивает видимость в `tools/list`, а вызов разрешает `unified_pdp` (deny by default) ([ADR-0002:20-28](../../docs/adr/0002-gateway-choice.md)).

Предложение:

| Virtual server | Состав `associated_tools` | Кто |
| --- | --- | --- |
| `vs-tg-<tenant>` (как сейчас) | ядро M11.1 (read) | агенты без расширений |
| `vs-tg-ext-<tenant>-<роль>` | ядро + три `telegram.ext.*` | агенты роли, у которой в файле tenant'а есть `ext:read…` |

«Роль» = группа агентов из `groups` файла tenant'а (например `researchers`). Привязка роли к virtual server — в конфиге bootstrap/генератора правил, не в ContextForge RBAC. Сам механизм «роль → virtual server» в v1.1.1 не проверен: на стенде virtual server один на team, ролей два (`reader`, `nobody`) различает только `unified_pdp` по email ([unified_pdp.rules.json:10-47](../../stand/unified_pdp.rules.json)).

Правила `unified_pdp`: для агента с `ext:read…` в bundle — allow на три имени `ext-*`; остальным — ничего (deny по умолчанию, [policies/README.md:19](../../policies/README.md)). Генерация правил из реестра платформы для production в плане «после этапа B» и пока не написана; для M11.3 нужна её часть «агент с ext-грантом → три allow». Генератор должен читать **тот же** bundle, чтобы не было дрейфа (GR-18, SHOULD).

### 3.3 Что ContextForge даёт (подтверждено)

| Возможность | Источник |
| --- | --- |
| Видимость tools по team; `tools/list` на `/rpc` и virtual server — только своя team | [M5.json:653](../conformance/M5.json) (`test_sc01_each_agent_lists_only_its_team_tools`) |
| Deny by default, сопоставление «точный email × точное имя tool», deny `*execute*` раньше allow | [ADR-0002:20-28](../../docs/adr/0002-gateway-choice.md); [unified_pdp.rules.json:2-9](../../stand/unified_pdp.rules.json) |
| Identity-плагин `tool_pre_invoke`: подпись JWS с `tool` (оригинальное имя) и `ahs` (sha256 канонических аргументов), на всех путях, включая virtual server и алиасы | [plugin.py:34-62](../../gateway/contextforge/gxg_identity/plugin.py); [core.py:96-101](../../gateway/contextforge/gxg_identity/core.py); [ADR-0001:111, 140](../../docs/adr/0001-identity-propagation.md) |
| Добавление tool'ов без форка: плагин и правила загружаются конфигом, без плагина ContextForge не стартует | [plugin.py:1-8](../../gateway/contextforge/gxg_identity/plugin.py); [stand/README.md](../../stand/README.md), строка про `PLUGINS_FAIL_ON_PLUGIN_ERROR` |
| Закреплённый образ v1.1.1 по digest | [M5.json:6](../conformance/M5.json) |

### 3.4 Чего не даёт (и что из этого следует)

| Ограничение | Следствие для расширений |
| --- | --- |
| Правила `unified_pdp` в репозитории содержат условия только по `subject.email` и действиям `tools.invoke.<имя>` ([unified_pdp.rules.json](../../stand/unified_pdp.rules.json)). Условия по аргументам вызова **не проверены**. | Нельзя на уровне ContextForge разрешить `ext.call` только для записи X. Разрешение по записи — в коннекторе. Если окажется, что PDP умеет условия по аргументам, всё равно не полагаемся: источник истины один — bundle |
| ContextForge сам валидирует `arguments` по `inputSchema` и при ошибке отвечает `-32603`, а не `invalid_input` ([M5.json:685](../conformance/M5.json)) | Входная схема `ext.call` должна быть общей: `{name: string, arguments?: object}`. Схему конкретной записи проверяет коннектор (иначе MCC-05 нарушается) |
| Один `TOOL_TIMEOUT` на все tool'ы (на стенде 25 с, [compose.yaml:66](../../compose.yaml)); свой его ответ — без кода ([M5.json:685](../conformance/M5.json)) | Таймаут `ext.call` в ContextForge ≥ максимума по записям + запас; таймауты записей — внутри коннектора |
| Лимит запросов MCP у ContextForge по умолчанию 100/мин, при превышении блокировка 15 мин ([M5.json](../conformance/M5.json), «findings_during_cycle»; на стенде повышен) | Все записи делят лимит одного tool'а `ext.call`. Шумная запись блокирует остальные. Лимиты записей — в коннекторе; поведение лимита ContextForge для `ext.call` — проверить на стенде |
| Отзыв токена агента — до `AUTH_CACHE_REVOCATION_TTL` (стенд 5 с, по умолчанию 30 с) ([M5.json](../conformance/M5.json), «findings_during_cycle»; [compose.yaml:71](../../compose.yaml)) | Для расширений менять нечего; экстренное отключение — раздел 6 |
| REST-tool с `visibility` рядом с `team_id` становится `public` ([ADR-0002:28](../../docs/adr/0002-gateway-choice.md)) | Три `ext.*` регистрируются тем же кодом bootstrap с `visibility` внутри `tool` |
| Глобальный `/mcp` отказывает team-scoped токену ([ADR-0001:140](../../docs/adr/0001-identity-propagation.md)) | Агенты ходят на `/rpc` или virtual server; для расширений ничего не меняется |

### 3.5 Нужен ли форк

**Нет.** Для M11.3 в ContextForge нужны только: регистрация gateway (как у `telegram-<tenant>`), virtual server, правила `unified_pdp` (конфиг) и существующий плагин `gxg_identity` без изменений. Условия, при которых вывод «нет» перестанет быть верным (каждое надо проверить на стенде в M11.3, пока всё не проверено):

1. `gateway`-регистрация подхватывает три мета-tools и схему `ext.call` без правки ContextForge (вероятно, как у существующих tool'ов; не проверено).
2. MC-05 (обновление ContextForge с прогоном SC/EX) проходит с `ext.*`.
3. Лимит запросов и таймаут ведут себя, как описано выше.

Если понадобится имя записи в аудите ContextForge (см. раздел 4), это правка **нашего** плагина `gxg_identity` (он уже в репозитории), а не форк.

## 4. Аудит: где фиксируется имя записи

### 4.1 Коннектор — запись о факте (MC-10)

Коннектор пишет одну JSON-строку на вызов ([mcp.py:353-385](../../packages/gxg-connector-kit/src/gxg_connector_kit/mcp.py)): `request_id`, `tool`, `tenant`, `principal`, `grant_id`, `outcome`, `duration_ms` плюс `fields`, которые возвращает handler (строка 371). Для `ext.call` handler добавляет в `fields`:

| Поле | Значение |
| --- | --- |
| `ext_entry` | имя записи из реестра (`telegram.chat.get_info`) |
| `ext_entry_version` | версия записи |
| `ext_group` | подгруппа права (`ext:read:info`) |

Правила:

- `tool` остаётся `telegram.ext.call`, `ext_entry` — второе поле. MC-10 проверяет именно имя записи.
- **Имя ключа не из `SENSITIVE_KEYS`** (`text`, `body`, `query`, `args`, `token`, `authorization`, [redact.py:9](../../packages/gxg-connector-kit/src/gxg_connector_kit/redact.py)): иначе поле вырезается. Поэтому не `query`/`args`.
- **Неизвестное имя** пишется как `ext_entry: "<unknown>"`, а не как введено агентом: исключает подделку строк аудита и попадание текста в лог (SEC-18). Отказы шагов 2–5 раздела 1.4 тоже пишут `ext_entry`, если имя известно реестру.
- Имя записи достоверно: оно лежит в `arguments`, а `ahs` в подписанном JWS связывает аргументы с вызовом ([core.py:101](../../gateway/contextforge/gxg_identity/core.py)); коннектор сверяет `ahs` до выполнения ([mcp.py:355-357](../../packages/gxg-connector-kit/src/gxg_connector_kit/mcp.py)).
- Аргументы записи (ссылки на чаты, тексты запросов) в аудит не пишутся. Содержит ли `ref` чата «peer ID» в виде, запрещённом AC-M11.3-05, зависит от формата `chat_ref` сервиса: в пилоте это непрозрачный `tgc1_…` ([t-pilot.yaml](../../policies/pilot/tenants/t-pilot.yaml)); для нового сервиса не проверено.

Проверка в стенде: помощник `stand.audit("telegram")` читает эти строки ([standlib.py:230](../../stand/tests/standlib.py), [test_telegram.py:113-114, 608-614](../../stand/tests/test_telegram.py)). MC-10 и AC-M11.3-05 пишутся тем же способом: фильтр по `ext_entry`, проверка отсутствия `text` и ссылок. Мутационный кейс AC-M11.2-04 «убрать имя записи из аудита» ловится этим же тестом.

### 4.2 ContextForge — не источник имени записи

- ContextForge видит вызов как `telegram.ext.call` с аргументами. Ведёт ли он собственный аудит аргументов и в каком виде — **не проверено**: M4/M5 используют только аудит коннектора (M5.json: «по аудиту `demo`»).
- Если владелец хочет имя записи и на стороне шлюза, то минимальное решение — расширить `metadata` результата нашего плагина: сейчас там `{"gxg_identity": sub}` ([plugin.py:62](../../gateway/contextforge/gxg_identity/plugin.py)); добавить `ext_entry` (только имя, не аргументы). Попадают ли метаданные плагина в журнал ContextForge v1.1.1 — не проверено; это отдельная проверка, а не предпосылка M11.3. Вопрос Q8.

### 4.3 Корреляция

`request_id` из JWS пишется в строку коннектора (`entry |= {"request_id": ident.request_id, …}`, [mcp.py:358](../../packages/gxg-connector-kit/src/gxg_connector_kit/mcp.py)), `bundle_version` — в `GrantResolver.version` (GR-14). Строка аудита `ext.call` связывается с запросом в ContextForge по `request_id` (claim `request_id` берётся из `gc.request_id`, [plugin.py:49](../../gateway/contextforge/gxg_identity/plugin.py)).

## 5. Правила `gxg-policy lint` для новых полей

Коды в стиле существующих (`dangling-connection`, `rid-domain`, `grant-expiring`, [lint.py:101-147](../../tools/gxg-policy/src/gxg_policy/lint.py)). Уровень: E — ошибка, W — предупреждение.

Схемные (validate, до lint):

1. `ext:` без класса и голое `ext` в `actions` — E (шаблон схемы).
2. Класс не `read` (`ext:write_reversible`, `ext:external_effect`, `ext:high_risk`) — E.
3. `extensions[].entries` без `@мажор` или с неверным именем — E.

Lint по файлу tenant'а:

4. `ext-action-domain` — E: грант с `ext:*` на RID, чей connection не имеет домена с расширениями (по `platform.<connector>.extensions` из bundle или манифеста; зависит от Q5).
5. `ext-grant-mixed` — W: `ext:*` и `read`/`collect`/`write`/`propose` в одном grant'е (хуже читается diff).
6. `ext-mask-wide` — W: `ext:*` на маске `…/<type>/*` без `expires_at`.
7. `ext-subgroup-unknown` — E (если реестр доступен) или W (если нет): подгруппа не из реестра.
8. `ext-enable-dangling-connection` — E: `extensions[].connection` не объявлен.
9. `ext-enable-domain` — E: у connection нет расширений в реестре.
10. `ext-enable-duplicate` — E: имя дважды в одном connection или `id` повторяется (расширение существующего `duplicate-id`, [lint.py:58](../../tools/gxg-policy/src/gxg_policy/lint.py)).
11. `ext-enable-unknown-entry` — E: имя или мажор не найдены в реестре.
12. `ext-enable-not-read` — E: запись не класса `read`.
13. `ext-enable-forbidden-name` — E: имя из запрещённого списка (2FA, сессии, экспорт, удаление аккаунта, `invoke`; список в M11.2).
14. `ext-enable-dead` — W: запись включена, а ни у одного principal'а нет подходящего `ext:read…` на каком-либо ресурсе connection'а.
15. `ext-grant-no-entries` — W: выдан `ext:read…`, но в `extensions` для этого connection нет ни одной записи этой подгруппы (вызовы дадут `effect_disabled`; допустимо, но стоит заметить).
16. `ext-expired` / `ext-expiring` — W: срок `extensions[].expires_at` как у `grant-expired`/`grant-expiring` (14 дней).
17. `diff`: новая запись в `extensions` и новое действие `ext:*` — расширение прав (`widening`); удаление — сужение. `diff` для `ext:*` работает на уровне троек уже сейчас ([diff.py:37-54](../../tools/gxg-policy/src/gxg_policy/diff.py)); для `extensions` нужен новый вид записи `kind: extension`. Вариант «ext:read:members выдан шире, чем ext:read:info» diff не различает, потому что сравнивает строки: приемлемо (любое новое действие — widening).

Манифесты и реестры:

18. `ext-manifest-effect` — E: `telegram.ext.{search,describe,call}` в манифесте с `effect_class` не `read`.
19. `ext-manifest-executes` — E: у `ext.*` в манифесте есть `executes_via` (нельзя исполнять эффекты).
20. `ext-registry-enabled-default` — E: запись не-`read` с `enabled_default: true` (AC-M11.2-01; линтит загрузчик реестра M11.2, правило здесь для CI Hub, если реестр доступен — Q5).

## 6. Риски и открытые вопросы владельцу

### Риски

1. **Реестр живёт в другом репозитории** (ADR-0005). Hub-lint не видит записей без копии реестра или его digest в bundle: правила 4, 7, 9, 11–13, 15, 20 зависят от этого (Q5). В репозитории Hub коннектор `connectors/telegram` с манифестом пока есть; после M11.1 он удаляется (ADR-0005, «Последствия»).
2. **Экстренное отключение одной записи** не быстрее bundle (до 5 мин, GR-13). Быстрый путь — снять `telegram.ext.call` у virtual server и в правилах ContextForge, то есть отключить сразу все записи tenant'а (ограничено токенами: ≤ 5 с по SEC-20). Сейчас в спеках быстрого пути «одна запись» нет (Q4).
3. **Лимит и таймаут ContextForge общие** на `ext.call` (раздел 3.4): одна шумная запись исчерпывает лимит остальным.
4. **Дрейф** между bundle и правилами `unified_pdp`, пока правила пишутся вручную; GR-18 только SHOULD. Для M11.3 правила стенда правятся вручную (раздел 3.2).
5. **Информационные утечки через отказы:** `effect_disabled` раскрывает, что запись существует и выключена. Порядок проверок в 1.4 — предложение (Q6).
6. **Широкие маски** `chat/*` вместе с `ext:read` читают любой чат аккаунта (раздел 5, правило 6).
7. **Не проверено на стенде:** имя tool'а в ContextForge, поведение лимитов для `ext.call`, условия `unified_pdp` по аргументам, журнал метаданных плагина, привязка роли к virtual server.
8. **Плагин и API ContextForge:** ADR-0001:143 требует, чтобы MC-05 ловил изменение API `cpex.framework`. Расширения не добавляют зависимостей на новые API, если имя записи в метаданные плагина не добавлять (раздел 4.2).

### Решения владельца (получены от оркестратора, учтены)

- **O-1 разрешён в минимальной форме:** группы `<domain>.ext:<класс>[:<подгруппа>]`; грант на ext без класса отклоняется; включение записи по tenant'у — отдельный явный список в политике tenant'а, по умолчанию выключено; расширение прав = `widening` в `gxg-policy diff`; `effect_disabled` без изменения смысла, запись реестра приравнена к tool'у. Реализует M11.2 (сессия m112-schema-tests), не этот документ. Сверка с разделом 1: форма из раздела 1.3 (`ext:read[:подгруппа]` в `actions`, домен берётся из RID) выражает ту же группу `telegram.ext:read`; если реализация M11.2 запишет домен в самом действии, раздел 1.3 и правки 1, 6, 8 меняются только в шаблоне строки. Q1 этим по сути закрыт, прошу сверить форму записи с M11.2. Правка 11 («`effect_disabled`: уточнить словарь») по решению не нужна: смысл не меняется.
- **O-2 (предложение, сверить с аудитом tg-gap-audit):** шесть записей `read`, только в пределах разрешённого `chat_ref`: информация о чате; история по диапазону; закреплённые сообщения; список участников с лимитом; информация о пользователе из этого чата; одно сообщение по ID. Всё, что требует peer ID вне разрешённых чатов, не включается. Это снимает Q2: все шесть записей имеют ресурс `chat`, нового типа RID не нужно. Подгруппы из примеров (`info`, `history`, `members`, `pinned`) остаются условными; имя подгруппы для «одного сообщения» и «пользователя из чата» не выбрано.
- **O-3:** в `demo` `ext.*` не добавляем. Тесты MC-06…MC-10 на M11.2 идут на тестовом коннекторе, на Telegram — в M11.3.
- **O-4:** M11.2/M11.3 в отдельной ветке, ничего не мержить и не пушить. M11.3 не начинать до закрытия пилота M13; в пилот расширения не входят. Практически: правила `unified_pdp` и virtual server `vs-tg-ext-*` из раздела 3 в пилотный tenant `t-pilot` не добавляются.

### Решения по вопросам Q1–Q8 (оркестратор по поручению владельца)

| Q | Решение | Что меняется в тексте выше |
| --- | --- | --- |
| Q1 | Группа значением в `actions` (`ext:read[:подгруппа]`), отдельного поля нет | Совпадает с разделом 1.3 |
| Q2 | Только ресурс `chat` (`scope: single`); записи про пользователя вне чата откладываются | Раздел 1.2, пункт про ресурс не на чате, снят; «пользователь из этого чата» остаётся с ресурсом `chat` |
| Q3 | Включение на tenant, блок `extensions` | Совпадает с разделом 2; привязки к группе агентов нет |
| Q4 | Задержка до 5 мин (GR-13) приемлема; быстрого отзыва одной записи нет; быстрый путь — снять `telegram.ext.call` у virtual server | Риск 2 остаётся как описание ограничения; правка 14 (SEC-20) сводится к этой фразе |
| Q5 | Lint без реестра: правила, которым нужен реестр, дают предупреждение; digest реестра в bundle отложен | Правила 4, 7, 9, 11–13, 15, 20 раздела 5 — уровень W; правка 9 (`platform.<connector>.extensions`) не делается |
| Q6 | Порядок отказов как в 1.4; `ext.search` отдаёт только включённые записи, входящие в право вызывающего | Раздел 1.4 принят целиком, включая шаги 3–5 |
| Q7 | Версия записи и подгруппа в аудите | Раздел 4.1 принят (`ext_entry_version`, `ext_group`) |
| Q8 | Имя записи в аудите ContextForge не нужно; MC-10 по аудиту коннектора | Раздел 4.2 остаётся справкой; правка плагина `gxg_identity` не делается |

### Что осталось непроверенным на стенде

- имя tool'а `telegram-<tenant>-telegram-ext-*` после регистрации в ContextForge v1.1.1;
- принимает ли ContextForge общую схему `{name, arguments?}` для `ext.call` без `-32603` на валидных вызовах и как ведёт себя лимит 100/мин для одного tool'а;
- что `unified_pdp` не умеет условия по аргументам (в репозитории они не использованы, но обратное тоже не проверено);
- привязка роли (группы агентов) к virtual server `vs-tg-ext-<tenant>-<роль>`: на стенде virtual server один на team;
- как `tools/list` virtual server ведёт себя при двух серверах одной team у одного агента;
- что `chat_ref` нового сервиса не содержит peer ID в виде, запрещённом AC-M11.3-05;
- прохождение MC-05 (обновление ContextForge) с `ext.*`.

Все эти пункты проверяются только в M11.3, который по O-4 не начинается до закрытия пилота M13.
