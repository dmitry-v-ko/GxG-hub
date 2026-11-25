# M11.2: группы прав `<domain>.ext:<класс>[:<подгруппа>]` — предложение (ждёт владельца, O-1)

Статус: **предложение, спеки и схемы не изменены.** Блокирует AC-M11.2-03 (`blocked_on_owner`).

## Проблема
Грант в `resource-grants-v1.md` сейчас = `subjects` + `actions` + `resources` (RID). Группы `telegram.ext:read` из MCC-11 п.5 в формате не выражаются. Нужно решить: это новое поле гранта или значение `actions`.

## Предлагаемое решение
Новое необязательное поле гранта `ext_groups` (список). `actions` остаётся как есть: `ext.call` имеет `action: read`, поэтому грант на ресурс (`read` + RID чата) по-прежнему обязателен, а `ext_groups` лишь сужает, какие записи реестра доступны.

Допустимый вид: `^[a-z][a-z0-9_]*\.ext:(read|write_reversible|external_effect|high_risk)(:[a-z][a-z0-9_]*)?$`. Голое `telegram.ext` или `telegram.ext:*` не проходит шаблон (MC-09: «грант на ext без класса отклоняется»).

### Diff `docs/specs/resource-grants-v1.md` (после таблицы действий)
```diff
+## Группы расширений
+
+Tools расширения (`<domain>.ext.*`, MCC-11) разрешаются группой записей реестра.
+
+- **GR-18.** Поле гранта `ext_groups: [<domain>.ext:<класс>[:<подгруппа>]]`. Класс — effect_class записи, подгруппа — поле `group` записи реестра (необязательное).
+- **GR-19.** Грант на `ext` без класса отклоняется при `gxg-policy validate`, загрузке bundle и в `GrantResolver`.
+- **GR-20.** Группа `<domain>.ext:read` разрешает только записи `effect_class: read`. Для остальных классов `ext.call` возвращает `permission_denied` независимо от группы (MCC-11 п.3).
+- **GR-21.** Группа разрешает запись только если запись включена для tenant'а (`enabled_default` или решение tenant'а). Иначе `effect_disabled`.
+- **GR-22.** `ext_groups` без `read`-гранта на ресурс ничего не разрешает.
```

### Diff `schemas/resource-grants.schema.json` (в `grants.items.properties`)
```diff
+          "ext_groups": {
+            "type": "array", "uniqueItems": true, "minItems": 1,
+            "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]*\\.ext:(read|write_reversible|external_effect|high_risk)(:[a-z][a-z0-9_]*)?$"}
+          },
```
Схема `gxg-capabilities.schema.json` получит необязательное поле `group` (подгруппа) — это уже в нашей зоне, добавим вместе с принятием.

## Что потребуется после подтверждения
- `GrantResolver`: новый метод `ext_allowed(tenant, principal, group, rid)` и учёт `ext_groups` в `check` для `ext.call`; поле входит в bundle (`grants-bundle` схема).
- `gxg-policy lint/validate`: проверка шаблона, GR-19, GR-22.
- Тесты ≥ 12 кейсов AC-M11.2-03: без класса, класс не-`read`, подгруппа, чужая подгруппа, чужой tenant, запись выключена для tenant'а, истёкший грант, маска RID, ext_groups без ресурсного гранта и т. д.

## Вопросы владельцу
1. Поле `ext_groups` отдельно от `actions` (как выше) или значение в `actions`?
2. Подгруппа = поле `group` записи реестра?
3. Включение записи для tenant'а: где живёт (поле гранта, отдельный список `ext_enabled` в файле tenant'а)?
