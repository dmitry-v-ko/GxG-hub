# Gap-аудит gxg-telegram-service перед расширениями `ext.*` (MCC-11)

Дата: 2026-11-25. Режим: только чтение, код сервиса не менялся.

## Что проверялось

- Репозиторий: `dmitry-v-ko/gxg-telegram-service`. Локально его нет; клон лежит в scratchpad сессии аудита.
- **Ветка `main` пустая** (один коммит, `README.md` из двух строк). Весь код — в `claude/m13-railway` (HEAD `7e9d7f8`), в неё входит `claude/m11.1-telegram-service`. Аудит выполнен по `claude/m13-railway`. Пути ниже относятся к `src/gxg_telegram_service/`.
- Требования: MCC-03, MCC-08, MCC-11 (`mcp-connector-contract-v1.md`), раздел «Telegram» в `service-integration-v1.md`, MC-06…MC-10 в `conformance-matrix-v1.md`, ADR-0005, `connectors/telegram/README.md`.
- Пакета `gxg-capabilities.yaml`, мета-tools `ext.*` и класса реестра в сервисе нет: «не найдено» (`grep -ri "capabilit\|ext\.search\|ext\.call"` по `src/`, `gxg-manifest.yaml`).

## Сводка

| № | Пункт | Статус |
|---|---|---|
| 1 | Инвентарь методов и портов | готово |
| 2 | Кандидаты в стартовый реестр read | нужна правка |
| 3 | Chat scope до сессии и архива | готово (для ядра); для `ext.call` нужна правка при добавлении |
| 4 | Flood-wait и очередь вызовов | нужна правка |
| 5 | Логи без секретов, peer ID и текстов | готово, три замечания |
| 6 | Raw invoke и запрещённые операции | нужна правка (защитный слой) |
| 7 | Место для реестра и мета-tools | нужна правка (создать новое) |

Итого: готово — 3, нужна правка — 4, вне объёма — 0.

## 1. Инвентарь методов Pyrofork и портов — готово

Порт к Telegram — `Client` Protocol (`transport.py:27-34`): `invoke(raw-функция, …)`, `storage`, `connect`, `get_me`. Реализация — `pyrogram.Client` (`accounts.py:139-142`, `no_updates=True`, `sleep_threshold=0`) и фейк в `tests/fake_telegram.py`.

Порты сервиса (tools → порт):

| Tool | Класс | Источник | Файл:строка |
|---|---|---|---|
| `telegram.messages.search` | read | архив PostgreSQL (`websearch_to_tsquery`), Telegram не трогает | `tools.py:225`, `archive.py:97` |
| `telegram.thread.read` | read | архив | `tools.py:276`, `archive.py:116` |
| `telegram.messages.collect` | read (action `collect`) | архив, cursor по `archived_at`; при flood-wait Connection → `rate_limited` | `tools.py:294`, `archive.py:130` |
| `telegram.message.get` | read | **live**: raw `messages.GetHistory` на одно сообщение | `tools.py:315`, `live.py:55`, `transport.py:176-191` |
| `telegram.message.propose` | external_effect | запись в журнал и Approval Service; без `TELEGRAM_APPROVAL_URL` не публикуется | `effects.py` (`PROPOSE`), `server.py:503` |
| `telegram.chats.list` | — | **не найдено** (в манифесте нет). Список чатов есть только в admin-контуре: `admin.py:101` (`dialogs`), `admin_api.py:375` (`GET /chats`) |

Вызовы Telegram, которые реально выполняет код:

| Метод | Где | Класс |
|---|---|---|
| raw `messages.SendMessage` | `transport.py:126`, исполняется только `execute` (порт 8081) | эффект |
| raw `messages.GetHistory` | `transport.py:159` (reconcile), `:182` (`message.get`), `legacy/ingest.py:800` (archive-sync) | read |
| raw `messages.GetDialogs` | `legacy/ingest.py:396`, `:989` | read |
| raw `messages.GetDialogFilters` | `legacy/ingest.py:426`, `:569`, `:1089` | read |
| raw `chatlists.CheckChatlistInvite` | `legacy/ingest.py:661` (только CLI `folder-invite-check`) | read |
| raw `messages.Search` | `legacy/ingest.py:242` (только CLI `signal-search`, `sleep_threshold=60`) | read |
| `get_me` | `accounts.py:77` | read |
| `get_users` | `legacy/ingest.py:56`, `:74` (CLI `account-lookup`) | read |
| `get_dialogs` | `admin.py:104` | read |
| `send_code`, `sign_in`, `check_password` | `admin_api.py:294`, `:311`, `:332` — вход оператора, не операции над аккаунтом | служебные |

Устаревший CLI из `gxg_agents_v2` (`legacy/ingest.py:1228-1252`) остаётся в пакете, но в `[project.scripts]` не зарегистрирован и требует `TELEGRAM_CONNECTOR_INTERNAL=true`. Сервис импортирует из него три символа: `collect_archive_batch`, `_input_peer`, `_media_type` (`sync.py:23`, `transport.py:21`, `live.py:16`).

## 2. Кандидаты в стартовый реестр read — нужна правка

| Кандидат | Реализуем через имеющиеся порты? | Пояснение |
|---|---|---|
| История чата | да | `telegram.thread.read` (архив) и `message.get` (live) уже есть. Отдельная запись реестра нужна только для live-истории, которой нет |
| Сообщение по id | да | `live.py:55`; `message_fields` отдаёт вложения только метаданными (`live.py:27-48`) |
| Информация о чате | частично | в архиве есть `chat_ref`, `state`, `gap` (`archive.py:60-86`); название и тип — только у `get_dialogs` в admin-контуре. Порта «get_chat» **не найдено** |
| Информация о пользователе | нет | `get_users` есть только в устаревшем CLI. Порта **не найдено**. Пользователь не является чатом: нужен тип ресурса (вопрос владельцу 2) |
| Участники | нет | `get_chat_members` / `channels.GetParticipants` в коде **не найдено** |
| Закреплённые сообщения | нет | `messages.Search` с фильтром pinned или `GetHistory` с фильтром — **не найдено** |

Вывод: из шести кандидатов один (история) покрыт ядром. Для остальных нужны новые функции в `transport.py` по образцу `get_message` (`transport.py:176`): peer из `archive.chat_peer`, `retries=0`, `sleep_threshold=0`, фиксированный список полей ответа.

## 3. Chat scope до открытия сессии и чтения архива — готово (ядро)

Порядок для каждого маршрута: идентичность → схема → `grants.check` → порт (`tools.py:113-139`, `_chat` в `tools.py:215-223`).

| Маршрут | Проверка grant до порта | Проверка scope Connection | Сессия |
|---|---|---|---|
| `search` | `tools.py:233` для названных чатов; без названных — только `allowed()` (`:236`); элементы перепроверяются (`:251`) | в запросе только чаты архива (`archive.py:71`) | не открывается |
| `thread.read` | `tools.py:277` | `archive.py:118` → `chat_forbidden` → `not_found` | не открывается |
| `collect` | `tools.py:295` | `archive.py:132` | не открывается |
| `message.get` | `tools.py:316` | `live.py:57` (`chat_peer` из архива, нет — `chat_forbidden`) | проверяется статус после scope (`live.py:60`) |
| `propose` | в `effects.py` до обращения к Approval Service (не разбирался построчно; вне объёма расширений) | — | — |

Для `ext.call` проверки сейчас нет, потому что нет самого вызова. Что нужно: `ext.call` должен брать `chat_ref` из аргументов по описанию ресурса записи реестра и вызывать тот же `Connector._chat(...)` до любого порта. Сейчас `resource: {from_arg: chat_ref}` в манифесте (`gxg-manifest.yaml`) — только метаданные; проверку делает код в каждом обработчике.

Особенность: `search` без `chat_refs` и с маской-грантом (`allowed.all_of_type`) берёт `scope_chats` из архива (`tools.py:237-238`). Для записей реестра с несколькими чатами аналога не нужно: стартовый реестр лучше ограничить записями `scope: single`.

## 4. Flood-wait и очередь вызовов — нужна правка

Что есть:
- Flood-wait от Telegram **сохраняется только archive-sync**: `sync.py:58` пропускает проход, пока `store.deferred_s` не пуст (`store.py:130`), а `store.apply` пишет `retry_after` из пакета (`sync.py:110`). Читают это значение `archive.py:78-86` (`collect` → `rate_limited`) и `sync.py`.
- Авто-сон библиотеки выключен: `sleep_threshold=0` (`accounts.py:141`, `transport.py:137`, `:164`, `:186`).
- `retry_after_s` отдаётся единообразно через `service_error` (`tools.py:94-96`): `ceil(delay)`, не меньше 1 и не больше 86400. Для `message.get` задержка берётся из исключения (`live.py:65-66`). Для отправки — `Sent.retry_after_s` (`transport.py:141-142`) и квоты в БД (`store.py:255-274`, `effects.py:202-210`).

Чего нет:
- **Общей очереди или семафора на клиент**: поиск `asyncio.Semaphore|Lock|Queue` по `src/` ничего не находит. Один `Account.client` одновременно используют sync (`sync.py:83`), `message.get` (`live.py:64`), отправка (`transport.py:136`), admin `/chats` (`admin_api.py:375`) и проверка `get_me` (`accounts.py:77`).
- **Flood-wait из `message.get` не записывается**: `live.py:65-66` только отвечает вызывающему. Следующий `message.get` и следующий проход sync снова пойдут в Telegram, пока Telegram не повторит отказ. Обратное тоже верно: flood-wait, записанный sync, `message.get` не проверяет (`live.py` не читает `deferred_s`).
- Отправка не проверяет flood-wait Connection (`effects.py`: обращений к `deferred_s` нет).
- Единственная сериализация — `pg_advisory_xact_lock` на квоты отправки (`store.py:268`). Это не очередь вызовов к Telegram.

Следствие для `ext.call`: при нескольких live-записях без общей точки каждая будет сама решать, ждать ли. Нужен один шлюз на Connection: `deferred_s` до вызова, ограничение параллелизма, запись `retry_after` по любому flood-wait, единый вывод `retry_after_s`.

## 5. Логи — готово, три замечания

Что проверено:
- Аудит вызова — одна JSON-строка: `request_id`, tool, tenant, principal, `grant_id`, исход, класс ошибки, `authorization_header`, `duration_ms`, пропущена через `redact` (`tools.py:115-152`). Тексты запроса и peer ID не пишутся; в `error` кладётся имя класса исключения (`tools.py:133`, `:146`).
- Стек внутренних ошибок — только файл:строка:функция (`tools.py:147-148`), в stderr (`server.py:592-599`).
- `pyrogram` приглушён до `CRITICAL`, `mcp`/`fastmcp`/`httpx` — до `WARNING` (`server.py:600-603`).
- Остальные логи — события со счётчиками и классами (`sync.py:88`, `:117`; `accounts.py:43-48`, `admin_api.py:187-196`). Пример `sync_pass` содержит только счётчики.
- Peer ID в архиве — только ключ соединения таблиц (`archive.py:7-8`); `chat_peer` не возвращается (`archive.py:154`).

Замечания:
1. `server.py:290` — `bundle_rejected` пишет `str(exc)` от `BundleError`. Текст исключения кита не проверялся и может нести детали подписи; безопаснее писать класс. Проверить тест.
2. `events.py:92-100` — в `webhook_dead` попадают `event_id` и `tenant`; без текстов и peer. Допустимо, отметка для полноты.
3. `legacy/ingest.py:88-91`, `:335`, `:433`, `:463`, `:527`, `:622` печатают результаты (в том числе записи сообщений, `signal-search`) в stdout. Они недостижимы из процесса сервиса (отдельный CLI), но лежат в пакете; при выкидывании `legacy/` риск исчезнет.

Название чата (`title`) в admin-контуре отдаётся оператору (`admin.py:107-111`, `admin_api.py:378-382`); в логи не попадает (аудит админ-API пишет шаг, Connection, оператора, `admin_api.py:190-`).

## 6. Raw invoke и запрещённые операции — нужна правка (защитный слой)

Прямого пути «агент → произвольный метод» в коде **не найдено**: tools регистрируются только из `gxg-manifest.yaml` по словарю обработчиков (`tools.py:185-213`), параметры tools — JSON-схемы из `schemas/`. Но порт построен на сыром `invoke`, и защиты от добавления опасных вызовов в коде нет.

Места, где есть raw invoke или доступ к клиенту:

| Место | Что даёт |
|---|---|
| `transport.py:27-34` `Client.invoke(query, *args, **kwargs)` | любой raw-метод на сессии аккаунта |
| `transport.py:136`, `:159`, `:181` | единственные вызовы в пути tools (SendMessage, GetHistory) |
| `Account.client` (`accounts.py:33`) — публичное поле, у `Live` (`live.py:64`), `Effects`, `Sync`, `AdminApi` | клиент доступен всем модулям |
| `legacy/ingest.py:56`, `:74`, `:242`, `:396`, `:426`, `:569`, `:661`, `:800`, `:989`, `:1089` | read-вызовы устаревшего кода; CLI `main` (`:1228`) за переменной `TELEGRAM_CONNECTOR_INTERNAL` |

Запрещённые операции (2FA, завершение сессий, экспорт сессии, удаление аккаунта) в коде:
- `export_session_string`, `log_out`, `ResetAuthorization(s)`, `DeleteAccount`, `account.*Password*` — **не найдено** (поиск по `src/`).
- Рядом лежат разрешённые служебные операции: ввод пароля 2FA при входе оператора (`check_password`, `admin_api.py:332`) и запись файла сессии (`secure_session_file`, `admin_api.py:432`). Это вход в существующий аккаунт, не смена. Админ-порт 8082 закрыт ключами `TELEGRAM_ADMIN_CALLER_KEYS` (`server.py:31-33`) и недоступен агентам.
- В `ext.call` слабое место — не текущий код, а будущий: если реестр сошлётся на функцию, принимающую raw-объект или имя метода, запрет обходится. Что нужно: обёртки с жёстко заданным методом и аргументами, без передачи `query` извне; тест соответствия, что в реестре нет имён из запрещённого списка (MC-08); проверка при загрузке (MC-06).

## 7. Где разместить реестр и три мета-tool'а — нужна правка (создать новое)

Предложение, без изменений в коде сейчас:

| Что | Куда | Почему там |
|---|---|---|
| `gxg-capabilities.yaml` | в корень репозитория рядом с `gxg-manifest.yaml` (манифест читается в `tools.py:47-48`, `:199`) | MCC-11 п.1: «рядом с manifest» |
| Загрузка и проверка реестра (MC-06): класс, схема, `action`, ресурс, `enabled_default`, лимиты; запрет имён из списка запрещённых | новый модуль `capabilities.py` рядом с `tools.py`; вызывать из `Connector.tools()` (`tools.py:184`) до публикации | при невалидной записи — отказ стартовать, как `ConfigError` (`server.py:91`) |
| Три мета-tool'а `telegram.ext.search/describe/call` | новые обработчики в `Connector` и три записи в `gxg-manifest.yaml` с `input_schema` в `schemas/` | пайплайн `TelegramTool.run` (`tools.py:113-152`) уже даёт identity, схему, аудит, таймаут и ошибки; публикация идёт «манифест × словарь обработчиков» (`tools.py:185-213`) |
| Исполнение записей | новый модуль `ext.py`: имя записи → функция → порт в `transport.py`/`archive.py` | ядро `tools.py` не разрастается; live-функции рядом с `get_message` |
| Проверка ресурса записи | вызов `Connector._chat` (`tools.py:215`) с `chat_ref` из аргументов | тот же путь, что у ядра; `not_found` без обращения к порту |
| Аудит | расширить `entry |=` в `tools.py:138` полем с именем записи | MCC-11 п.4 / MC-10 |
| Единый шлюз flood-wait и параллелизма | новый класс на Connection между `Live`/`ext` и `account.client` (см. п. 4) | сейчас такого места нет |

Не нужны отдельные порты 8080/8081/8082: мета-tools идут через основной MCP-порт 8080. Порт 8081 (`execute`) не должен получать ничего из `ext`.

Что в самом пайплайне придётся изменить: таймаут берётся из `limits.timeout_s` манифеста (`tools.py:206`) для всего tool'а; для записей реестра лимиты свои, поэтому для `ext.call` нужно брать таймаут записи внутри обработчика. Также нужен признак «запись включена для tenant'а» (`enabled_default`, `effect_disabled`, MC-09): в `grants.check` (кит) такого вида не найдено, механизм не определён.

## Топ-5 разрывов

1. **Нет общего шлюза Telegram (п. 4).** Один клиент делят sync, `message.get`, отправка и admin без очереди; flood-wait пишет только sync. Live-записи `ext.call` без шлюза нарушат MC-10 (`retry_after_s`, лимиты).
2. **Нет портов для кандидатов реестра (п. 2).** Информация о чате и пользователе, участники и закреплённые сообщения не реализованы; готова только история. Каждая запись требует новой функции в `transport.py` и тестов с фейком.
3. **Нет способа включать запись по tenant'у (п. 7).** Для MC-09 (`effect_disabled`, грант без класса) в кит-резолвере такой модели не найдено.
4. **Порт построен на сыром `invoke` без защитного слоя (п. 6).** `Client.invoke` открыт всем модулям, `Account.client` публичен; MC-08 держится на том, что в коде пока нет опасных вызовов.
5. **Ветка `main` пустая, код только в `claude/m13-railway` (преамбула).** Расширения нужно делать от неё; решение о слиянии в `main` лежит вне объёма.

## Вопросы владельцу

1. Куда сливать `claude/m13-railway`, и от какой ветки вести работу над расширениями: `main` пуст.
2. Пользователь (`get_users`) — это отдельный тип ресурса или всегда читается в контексте разрешённого чата? От этого зависит формат grant'а для «информации о пользователе».
3. Где хранить включение записей реестра по tenant'ам (`enabled_default: false`): в grants bundle (изменение формата в Hub и ките) или в БД сервиса?
4. Live-чтение истории, участников и закреплённых сообщений — допустимо ли сразу, или ждём решения по live fallback из таблицы этапов (`service-integration-v1.md`, «live fallback — после решения по сессии»)?
5. `legacy/` (1258 строк `ingest.py`, три импортируемых символа) оставляем или выносим нужное и удаляем остальное до добавления `ext`?
6. `telegram.chats.list` в манифесте не опубликован; нужен ли он в ядре до `ext` (в service-integration он упомянут как «когда понадобится»)?
