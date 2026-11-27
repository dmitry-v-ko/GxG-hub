# M11.3 / M11.4, часть Hub: политики, virtual servers, аудит, MC-11…MC-13

Ветка `claude/m114-hub-policy-c1d0fa` от `claude/mcc11-ext-on-m14`. Ничего не запушено. Спеки (кроме строк MC-11…MC-13 матрицы) не менялись: предложения правок в разделе 5.

## 1. Что сделано

| Что | Где |
| --- | --- |
| Tenant `t-ext-example`: `read`, `ext:read`, подгруппы `ext:read:members/users/reactions`, `collect`, allowlist чатов, `extensions.enabled: []` + девять записей в комментарии, `ext:read:pii` отдельным закомментированным grant'ом | `gateway/contextforge/examples/tenants/example-ext.yaml` |
| Тестовый tenant `t-ext-test`: девять записей включены, `ext:read:pii` только у collector | `gateway/contextforge/examples/tenants/example-ext-test.yaml` |
| lint: `ext-mask-wide` (W), `ext-pii-no-expiry` (W) | `tools/gxg-policy/src/gxg_policy/lint.py` |
| golden: `bad/24…27` (pii в неверном регистре, `ext:pii` без класса, формат имени из дизайна, повтор записи), `diff/11` (новый `ext:read:pii` = widening), `diff/12` (девять записей = 9 widening типа `extension`) | `tools/gxg-policy/tests/` |
| Набор инструментов и правила `unified_pdp` по агентам, считаются из файла tenant'а | `gateway/contextforge/ext_roles.py`, примеры в `gateway/contextforge/examples/` |
| Тесты видимости: роли → tools, drift-тест, `ext.search` без pii для `ext:read`, `t-pilot` без ext | `gateway/contextforge/tests/test_ext_virtual_servers.py` |
| redact: ключи `phone`, `photo*`, `file_name`, `bytes` и номера в строках | `packages/gxg-connector-kit/src/gxg_connector_kit/redact.py` |
| MC-11…MC-13: строки матрицы, `check_mc11..13`, fake-коннектор, 5 мутантов, stage F (`not_run`), счётчик MC = 13 | `tests/contract/`, `scripts/ci/stage_f.py`, `tests/ci/test_stage_f.py` |

## 2. Virtual servers (решение в этой работе)

Имя сервера считается по набору tools агента, а не по «роли»: `vs-tg-<tenant>` (ядро), `-ext`, `-people`, `-ext-people`. Привязку роли к virtual server в ContextForge v1.1.1 никто не проверял, поэтому она не нужна: сервер выводится из тех же grants. Расхождение с дизайном (`vs-tg-ext-<tenant>-<роль>`): если владелец хочет имя по группе агентов, меняется только `server_name`.

- `ext.search/describe/call` видит агент с любым `ext:*` на telegram-ресурсе; `participants.collect` и `messages.collect` — агент с `collect`.
- pii-запись на уровне ContextForge не отличить (это аргумент `ext.call`). «Не видна роли без права» обеспечивает коннектор: `ext.search` показывает запись, только если она включена и `ext_check` покрывает её группу. Тест на `BundleStore` ядра: `ext:read` видит 7 из 9, collector с явным `ext:read:pii` — 9, чужой чат — 0.
- Правила `unified_pdp` — точный email × точное имя tool, без `execute`/`reconcile`/`propose` (тест). Пилотный tenant и `stand/unified_pdp.rules.json` не тронуты (O-4, тест).

## 3. Аудит

- Имя записи пишет коннектор (`ext_entry`, `ext_entry_version`, `ext_group`), ContextForge его не видит; плагин `gxg_identity` не менялся (Q8). Для `participants.collect` — `tool`, число элементов, `pii_fields` (число, не значения).
- Ключи `phone`, `phone_number`, `photo`, `photos`, `photo_ref(s)`, `file_name`, `filename`, `bytes`, `photo_bytes` вырезаются из любой записи аудита и логов вместе с вложенными; E.164-номер в строке (текст ошибки с эхом ввода) заменяется на `[phone]`. Счётчики (`photo_count`) остаются. Это страховка поверх коннектора, не замена: сервис обязан не класть значения в `fields`.
- Проверка MC-11 ищет телефон, ссылку на фото, имя файла и байты в `repr(audit)`; мутант «телефон в аудите» ловится.

## 4. Не проверено на стенде (и почему)

Стенд из `claude/m4-live` не в этой базе, `stand/secrets/` нет, образа сервиса с `ext.*` и `participants.collect` нет, а коннектор Telegram в этом репозитории (`connectors/telegram`) их не содержит. Поэтому **не проверено на стенде**:

1. Регистрация трёх meta-tools и `participants.collect` в ContextForge v1.1.1 и их фактические имена (`telegram-<tenant>-telegram-ext-call` — по правилу стенда, не по факту).
2. Принимает ли шлюз общую схему `ext.call` `{name, arguments?}` без `-32603`.
3. MC-05 (обновление ContextForge, прогон SC/EX с `ext.*`).
4. Лимит 100/мин и таймаут `TOOL_TIMEOUT` на `ext.call` (общие на все записи).
5. Условия `unified_pdp` по аргументам (не использованы).
6. Два virtual server одной team у одного агента в `tools/list`.
7. MC-06…MC-13 на сервисе: в отчёте stage F остаются `not_run`.
8. Live Telegram, Postgres: не затрагивались.

## 5. Предложения правок спек (не применены)

1. Матрица и roadmap: `roadmap-v1.md:16` говорит «MC-06…MC-10»; стало бы «MC-06…MC-13».
2. `mcp-connector-contract-v1.md`: сервис отдаёт `invalid_input` с `details.reason = limit_exceeded | cursor_too_large`, а AC-M11.4-07 пишет `limit_exceeded`. Либо добавить код в словарь MCC-05, либо записать эту форму в AC.
3. GR-25/AC-M11.4-07: явно сказать, что без `pii` запись `users.profile_photo_get` даёт `permission_denied`, а группы по GR-23 — `resource_forbidden`; в `participants.collect` — `field_errors`, а не отказ (формулировка «`permission_denied` для этих полей» в service-integration это и означает).
4. `photo_count == 0` не отличает «нет фото» от «скрыто приватностью»: фильтр `no_avatar` даёт ложные срабатывания. Решение владельца.
5. Имена записей: в дизайне `telegram.chat.get_info@1`, в реестре и схеме tenant'а — `telegram:chats.get_info` без `@мажор`. Дизайн (разделы 2.2–2.4) устарел.
6. `check-connectors` сервиса: `telegram.ext.call: input limit must have a maximum <= limits.max_results` — правило Hub или манифест сервиса, решить.

## 6. Блокирует

- Прогон MC-06…MC-13 на сервисе и стенде: нужны стенд M4 (`claude/m4-live`), секреты и образ сервиса; по O-4 M11.3 не начинается до закрытия M13.
- Ветка сервиса вендорит kit: после слияния этой ветки перевендорить (`redact.py` изменился).
