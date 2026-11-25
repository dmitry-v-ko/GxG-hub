# Gap-аудит gxg-telegram-service перед M11.3 (расширения ext.*)

**Дата:** 2026-11-25. **Тип:** разведка, только чтение. Код сервиса не менялся, против живого Telegram ничего не запускалось, `.env` и секреты не открывались.

## 0. Что именно проверено

Ссылки вида `файл:строка` относятся к репозиторию `dmitry-v-ko/gxg-telegram-service`, коммит `7e9d7f8` ветки `claude/m13-railway`. Пути указаны от корня сервиса.

**Важное наблюдение о состоянии репозитория.** В ветке по умолчанию `main` лежит только `README.md` из двух строк (коммит `e98a61e`). Весь код живёт в ветках `claude/m11.1-telegram-service` (84 коммита) и `claude/m13-railway` (91 коммит, включает первую). Ни одна из них не влита в `main`. Аудит сделан по `claude/m13-railway`, потому что это самая свежая линия, на которой собран Railway-деплой.

**Прочитано в Hub:** ADR-0005, MCC-02, MCC-03, MCC-08, MCC-11 (`mcp-connector-contract-v1.md`), раздел «Telegram» в `service-integration-v1.md`, `resource-grants-v1.md` (GR-01…GR-18), SEC-17 и SEC-18. В этом worktree план `implementation-plan-v1.md` ещё без M11.2 и M11.3, поэтому их критерии (AC-M11.2-01…05, AC-M11.3-01…07) читались в соседнем checkout `claude/mcc11-extensions`. Ограничение: Hub в этом аудите прочитан, но не менялся.

## 1. Что сервис публикует сегодня

### 1.1. Tools и порты

| Порт | Что на нём | Кто ходит | Где в коде |
| --- | --- | --- | --- |
| 8080 | MCP (Streamable HTTP), `/healthz`, `/readyz` | ContextForge | `server.py:553`, `create_app` `server.py:358-386` |
| 8081 | внутренние `execute`/`reconcile`, только при заданном `TELEGRAM_APPROVAL_URL`, опционально TLS | Executor | `server.py:557-571` |
| 8082 | админ-API: вход в аккаунт, scope чатов; слушается только при `TELEGRAM_ADMIN_CALLER_KEYS` | Approval UI | `server.py:573-574`, `admin_api.py` |

Опубликованные MCP tools (`gxg-manifest.yaml`):

| Tool | Класс | Откуда читает | Строки manifest |
| --- | --- | --- | --- |
| `telegram.messages.search` | read | архив Postgres | 8-17 |
| `telegram.thread.read` | read | архив Postgres | 18-27 |
| `telegram.messages.collect` | read, действие `collect` | архив Postgres | 28-37 |
| `telegram.message.get` | read | **живой Telegram** (единственный live-read) | 38-47 |
| `telegram.message.propose` | external_effect | intent в Approval Service; отправка через порт 8081 | 48-62 |

`telegram.chats.list` как tool нет: список чатов доступен только оператору через `admin.py:101-111` (`dialogs`) и админ-API.

Публикация tool'а управляется словарём `handlers` в `tools.py:186-193`: tool попадает в MCP, только если его обработчик подключён (`tools.py:211-212`). `message.get` появляется при наличии сессий (`server.py:437`), `propose` — при наличии Approval URL.

### 1.2. Manifest

Manifest читается с диска при каждом вызове `Connector.tools()` (`tools.py:199`) и сопоставляется с обработчиками по имени. Схемы входа и выхода подгружаются функцией `load_schema` (`tools.py:66-71`) из `schemas/`. Все tools ядра описаны там же в `gxg-manifest.yaml`, дополнительного загрузчика конфигурации нет.

### 1.3. Identity (X-GxG-Identity)

Проверка идёт в `TelegramTool.run` (`tools.py:113-121`): из HTTP-заголовков берётся `X-GxG-Identity`, верификатор кита `ContextForgeVerifier` (`server.py:409-410`, `vendor/.../cf_identity.py`) проверяет подпись Ed25519, срок, `aud=telegram`, одноразовый `jti` и привязку к вызову: claim `tool` обязан совпасть с именем вызванного tool'а (`cf_identity.py:73`), claim `ahs` — с хешем аргументов (`cf_identity.py:75`). Заголовок `Authorization` от клиента фиксируется в аудите, но полномочий не даёт (`tools.py:119`).

Без ключей `TELEGRAM_IDENTITY_KEYS` включается `NoVerifier`, который отказывает всем (`server.py:248-252`, fail closed). Тестовый issuer допускается только при `GXG_PROFILE=test` и взаимоисключим с боевыми ключами (`server.py:190-193`).

Следствие для `ext.call`: подпись identity привязана к имени `telegram.ext.call` и хешу всех аргументов, включая имя записи реестра и её параметры. Подменить запись реестра при неизменной подписи нельзя.

### 1.4. Grants

`GrantResolver` из кита (`BundleStore`), подписанный bundle загружается при старте и при изменении файлов (`BundleFiles`, `server.py:268-290`). Без валидного bundle вызов даёт `temporarily_unavailable` до обработчика (`tools.py:126`, `tools.py:195-197`).

Проверка права на чат в `Connector._chat` (`tools.py:215-223`): RID `telegram:<hub-connection>/chat/<chat_ref>`, действие `read` или `collect`, нет права → `not_found` без обращения к сервису. Поиск сначала сужается до `allowed()` (`tools.py:236-241`), затем каждый элемент проверяется повторно (`tools.py:250-253`). Это реализует GR-16 и GR-17.

В киту действие ограничено списком `Literal["read", "collect", "write", "propose"]` (`vendor/.../grants.py:33`). Групп вида `telegram.ext:read` в `GrantResolver` нет. Это работа M11.2.

Tenant привязан к connection через `TELEGRAM_CONNECTIONS` (`server.py:234-245`): один Telegram-аккаунт обслуживает один tenant. Нет connection у tenant'а → `not_found` (`tools.py:217-219`).

### 1.5. Аудит

Одна JSON-строка на вызов в логгер `gxg_telegram_service.audit`: `request_id`, `tool`, `tenant`, `principal`, `grant_id`, `outcome`, `duration_ms`, при ошибках `service_error`, `reason`, класс исключения (`tools.py:115, 121, 138, 141-152`). Перед записью запись проходит `redact()` (`tools.py:152`). Текст, запрос и аргументы в запись не кладутся вовсе.

## 2. Сессия Pyrofork, flood-wait и очередь вызовов

### 2.1. Где что живёт

| Что | Где | Замечание |
| --- | --- | --- |
| Сессия Pyrofork | `accounts.py:122-144`: один клиент на Connection, файл сессии в `TELEGRAM_SESSION_WORKDIR`, `no_updates=True`, `sleep_threshold=0` | Клиент автоматически не ждёт flood-wait |
| Владение сессией | `PrimarySessionLock` на всё время процесса (`accounts.py:51-60`, `legacy/config.py:17`) | Второй процесс получает статус `busy` |
| Статус сессии | `Account.status` (`accounts.py:25, 41-49, 86-92`), `/readyz` 503 при не-`ok` (`accounts.py:111-115`) | `expired` пишет alert |
| Archive-sync | `sync.py:54-118`, цикл в `Runtime.background` (`server.py:328-356`) | Один проход на Connection, до 3 запросов истории на чат |
| Flood-wait | колонка `telegram_archive.connection_state.retry_after` (`db/20261105010000_telegram_archive.sql:64-66, 128-130`), читается `Store.deferred_s` (`store.py:130-141`) и `ArchiveService._state` (`archive.py:78-86`) | Пишется **только** функцией применения пачки sync'а |
| Квоты отправки | `Store.admit`/`reserve` под `pg_advisory_xact_lock` на Connection (`store.py:208-285`) | Считает только отправки, чтение не учитывает |
| Очередь вызовов | **отсутствует** | См. ниже |

### 2.2. Единого места для вызовов в Telegram нет

Вызовы в Telegram сегодня идут из трёх мест, каждое со своим вызовом `client.invoke`:

1. archive-sync: `legacy/ingest.py:800-803` (`_history_page`), `legacy/ingest.py:989-992` (диалоги), `legacy/ingest.py:1089`.
2. live-чтение одного сообщения: `live.py:55-69` → `transport.get_message` (`transport.py:176-191`).
3. отправка и reconcile: `transport.send` (`transport.py:107-149`), `transport.find_sent` (`transport.py:152-173`).

Общие свойства этих путей: все используют `retries=0` и `sleep_threshold=0` (`transport.py:137, 163-164, 186-187`), то есть flood-wait не ждётся, а возвращается вызывающему. Это правильно для политики «никогда не повторять рано». Но дальше разрыв:

- **Нет общего семафора или очереди на Connection.** `Runtime.background` крутит sync последовательно, а MCP-вызов `message.get` приходит в тот же event loop параллельно с проходом sync и с отправкой. Ничто не ограничивает число одновременных запросов на один аккаунт.
- **Flood-wait от live-пути не записывается.** `Live.message` при `account.errors.flood` просто превращает ошибку в `ServiceError("flood_wait")` (`live.py:65-66`). Запись в `connection_state` делает только sync (`sync.py:108-112`, `store.apply`). То же для send: `Sent("not_sent", retry_after_s=…)` (`transport.py:141-142`) уходит в Approval Service, но не в общее состояние. Значит, после flood-wait на live-чтении sync продолжит стучаться в Telegram и поймает второй flood-wait.
- **Live-путь не проверяет уже известную отсрочку.** `Live.message` не вызывает `Store.deferred_s` или аналог: если sync уже получил `FLOOD_WAIT_X`, live-вызов всё равно уйдёт в Telegram.
- **Нет пейсинга.** Нет ограничения частоты запросов на аккаунт, кроме квот на отправку.

Итого: «единое место, через которое пройдут и ядро, и ext-вызовы» **есть только на уровне клиента**: общий объект `Account.client` (`accounts.py:32, 144`) и общая точка проверки статуса `account.status != "ok"` (`live.py:60-62`). Как вызовы идут, нигде не регулируется. Для ядра с одним live-чтением это терпимо, для ext с десятком live-read это недопустимо: один агент может исчерпать лимиты аккаунта и остановить archive-sync, которым питается Signals.

### 2.3. Что мешает добавить новые методы чтения

| Препятствие | Где |
| --- | --- |
| Порт `Client` в `transport.py:27-34` знает только `invoke`, `connect`, `get_me`, `storage`; новые raw-функции через него вызываются, но каждая требует отдельной обёртки по образцу `get_message` | `transport.py:176-191` |
| Хелпер `_input_peer` берёт peer **только из кэша сессии** (`storage.get_peer_by_id`), без сетевого поиска; нет в кэше → `ArchivePeerUnavailable` | `legacy/ingest.py:787-792` |
| Fake Telegram для тестов не умеет новые методы; расширять придётся `tests/fake_telegram.py` | `tests/fake_telegram.py` |
| `TelegramTool.run` проверяет только одну `inputSchema` (`self.parameters`) и не валидирует выход и размер ответа | `tools.py:122-125, 139` |
| В `Connector.tools()` набор обработчиков фиксирован (`tools.py:186-193`), `ext.*` нужно добавлять туда и в manifest | `tools.py:184-213` |

## 3. Право на чат, chat_ref и peer ID

### 3.1. Порядок проверок

1. identity (`tools.py:120`).
2. входная схема (`tools.py:122-125`).
3. bundle доступен (`tools.py:126`).
4. grants на RID чата (`Connector._chat`, `tools.py:215-223`). Нет права → `not_found`. До этого момента ни сессия, ни архив не открывались.
5. scope Connection: чат должен лежать в `telegram_archive.chats` этого Connection. Для чтения архива это `ArchiveService._state` (`archive.py:70-71`), для live — `archive.chat_peer` (`archive.py:154-165`, вызывается в `live.py:57`). Чата нет → `ServiceError("chat_forbidden")` → `not_found` (`tools.py:97-98`).
6. только после этого берётся сессия и вызывается Telegram (`live.py:60-64`).

Следствие: grants только сужают scope Connection и никогда не расширяют (комментарий `tools.py:6-7`). Scope задаёт оператор через админ-API, и триггер `purge_out_of_scope` удаляет чат из архива в той же транзакции.

### 3.2. chat_ref и peer ID

`chat_ref` — не случайный идентификатор, а **обратимая** подпись peer'а: `tgc1_` + base64url(tag ‖ AES-256-CTR(peer)), где tag — HMAC от `connection_id` и peer'а (`chatrefs.py:1-54`). Секрет — `TELEGRAM_CHAT_REF_SECRET_FILE`, общий с `gxg_agents_v2`. Из этого следует:

- `ChatRefs.resolve(connection, chat_ref)` (`chatrefs.py:43-54`) возвращает peer ID по одному только `chat_ref` и **не проверяет** scope. Им пользуется sync (`sync.py:63`).
- Для tool'ов путь другой: peer берётся из строки архива (`archive.chat_peer`), то есть сначала проверяется scope. Для ext это правильный путь, и использовать `resolve` напрямую в ext-вызовах нельзя.
- Peer ID наружу не возвращается ни в одном tool (`archive.py:9`, `archive.py:154`, `chatrefs.py:4`).
- `chat_ref` другого Connection не разрешается (проверка tag, `chatrefs.py:52-53`).

### 3.3. Какие методы чтения реально вызываются при chat scope

Таблица описывает возможности MTProto/Pyrofork по их документированному поведению. Ни один метод не запускался, сигнатуры не проверены на установленной версии `pyrofork==2.3.69` (`pyproject.toml:7`), поэтому перед M11.3 нужна проверка по исходникам библиотеки.

| Метод чтения | Нужный peer | При chat scope | Замечание |
| --- | --- | --- | --- |
| История чата (`messages.GetHistory`) | peer чата из scope | да | Уже используется (`transport.py:176-191`) |
| Сообщения по ID (`messages.GetMessages`, `channels.GetMessages`) | peer чата из scope | да | Для супергрупп и каналов нужен channel peer |
| Поиск внутри чата (`messages.Search` с `peer`) | peer чата из scope | да | Живой поиск, отличается от поиска по архиву |
| Закреплённые (`messages.Search` с фильтром pinned; `Chat.pinned_message`) | peer чата из scope | да | |
| Информация о чате (`messages.GetFullChat`, `channels.GetFullChannel`) | peer чата из scope | да | Ответ содержит ссылки-приглашения и ID связанных чатов; поля надо отсеивать |
| Число участников / онлайн | peer чата из scope | да | Только число |
| Участники (`channels.GetParticipants`, `messages.GetFullChat`) | peer чата из scope | да, но результат — **пользователи** с peer ID | Нужен `user_ref` или маскирование (см. вопрос владельцу O-3) |
| Ответы в треде (`messages.GetReplies`) | peer чата из scope | да | |
| Информация о пользователе (`users.GetFullUser`, `users.GetUsers`) | peer пользователя | **нет** для произвольного ID | Пользователь вне списка чатов; допустим только как элемент списка участников разрешённого чата |
| Диалоги и список чатов (`messages.GetDialogs`) | нет peer | **нет** | Раскрывает чаты вне scope; только оператор (`admin.py:101`) |
| Глобальный поиск (`messages.SearchGlobal`) | нет peer | **нет** | Идёт по всем чатам аккаунта |
| Общие чаты с пользователем (`messages.GetCommonChats`) | peer пользователя | **нет** | Раскрывает чужие чаты |
| Журнал событий и админ-данные (`channels.GetAdminLog`, ссылки-приглашения) | peer чата | технически да, но по смыслу нет | Нужны права администратора, выдают лишние ПД |

Практическое правило для реестра: запись допустима, если у неё ровно один аргумент-ресурс `chat_ref` из scope, а все остальные аргументы — числа и строки внутри этого чата. Запись с произвольным `user_id` или без `chat_ref` в реестр не попадает.

## 4. Логи и аудит

### 4.1. Что попадает

| Канал | Содержимое | Источник |
| --- | --- | --- |
| Аудит вызова | `request_id`, `tool`, `tenant`, `principal`, `grant_id`, `outcome`, `duration_ms`, класс ошибки | `tools.py:115-152` |
| Внутренняя ошибка | класс исключения и кадры стека `файл:строка in функция`, без текста исключения | `tools.py:145-149`; в stderr (`server.py:592-603`) |
| События сессии | `connection`, `status` | `accounts.py:41-49` |
| Sync | счётчики без ID и текстов | `sync.py:117` |
| Webhook | только идентификаторы | `sync.py:136-145` |
| Логи библиотек | `mcp`, `fastmcp`, `httpx` → WARNING; `pyrogram` → CRITICAL, так как он пишет peer и телефон на INFO | `server.py:600-603` |

В ответ клиенту текст сообщений попадает, но через `escape` и пометку `untrusted: true` (`tools.py:78-82, 285`). В аудит текст не попадает.

### 4.2. Остаточные риски для ext

- `redact()` вырезает ключи `text`, `body`, `query`, `args`, `token`, `authorization` на любой глубине (`vendor/.../redact.py:9-17`). Это защита по списку ключей, а не по смыслу. Если новый обработчик ext положит в `fields` что-то вроде `title` чата, имя участника или `user_id`, редакция его не остановит. Для `fields` нужен allowlist ключей.
- Исключения из `invoke` могут нести peer ID в тексте. Сейчас логируется только класс (`tools.py:146`, `live.py:69`, `archive.py:57`). Новые обработчики обязаны держаться этого же правила.
- `ServiceError.detail` — тоже только имя класса (`service.py:20-23`).

### 4.3. Место для имени записи реестра

Есть, и очень удобное. Обработчик возвращает пару `(content, fields)`, и `fields` сливается в `entry` перед записью (`tools.py:138`: `entry |= fields | {"outcome": "ok"}`). Достаточно вернуть `{"registry_entry": "<имя>"}` и `{"registry_version": "<версия>"}`. Но:

- на ветках ошибок (`not_found`, `effect_disabled`, `permission_denied`) `fields` не возвращается, потому что исключение вылетает до строки 138. Имя записи для отказов надо класть в `entry` раньше, сразу после разбора аргументов (в `run`, после проверки схемы на `tools.py:125`), иначе отказ не попадёт в аудит с именем записи (MCC-11 п.4, AC-M11.3-05);
- имя ключа не должно совпадать с `SENSITIVE_KEYS`: не `args`, не `query`.

## 5. Загрузка конфигурации и версия кита

### 5.1. Загрузчик реестра

Отдельного загрузчика конфигурации или реестра нет. Есть три связанных места:

- `Settings.from_env` (`server.py:150-231`): конфигурация целиком из переменных окружения, валидируется при старте, при ошибке сервис не стартует (`ConfigError`, `server.py:91`, `main` `server.py:615-622`).
- manifest и схемы: `yaml.safe_load(MANIFEST.read_text())` внутри `Connector.tools()` (`tools.py:199`) и повторно в `_effects` (`server.py:518`).
- в кит M1.1 есть `load_manifest` с линтом (`vendor/.../mcp.py:205`), но сервис им не пользуется.

`gxg-capabilities.yaml` естественно ложится рядом с `gxg-manifest.yaml`. В образ он попадёт добавлением одной строки `COPY` в `Dockerfile` (рядом со строкой 18: `COPY gxg-manifest.yaml ./`). Логично грузить его один раз в `build()` (`server.py:402`) и валить старт при невалидном реестре, как сделано с остальной конфигурацией. Загрузчик должен быть из кита после M11.2, а не копией в сервисе.

### 5.2. Версия gxg-connector-kit

ADR-0005 говорит про git-зависимость с закреплённым commit. Фактически:

- `pyproject.toml:52`: `gxg-connector-kit = { path = "vendor/gxg-connector-kit" }`, комментарий в строках 49-51: кит скопирован из GxG-hub `7e7ce56`, «byte-equal», потому что сборка Railway не видит приватный GxG-hub.
- `uv.lock` ссылается на `directory = "vendor/gxg-connector-kit"`.
- `gxg-approval` и `gxg-connector-check` закреплены git rev (`pyproject.toml:53-54`).

Следствие: bump кита — это повторное вендоринг каталога (≈ 25 файлов с тестами) и отдельная проверка byte-equal. Для M11.3 это обязательный шаг после M11.2, потому что именно в кит M11.2 кладёт загрузчик реестра и группы грантов `telegram.ext:read`.

## 6. Предложение стартового набора записей реестра (решение за владельцем)

Все записи класса `read`, действие `read`, ресурс `chat` (один `chat_ref` из scope), `enabled_default: false`. Имена рабочие, не нормативные.

| № | Запись | Подгруппа прав | Метод Pyrofork / MTProto | Тип ресурса | Вне chat scope? | Замечание |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `chat.info.get` | `telegram.ext:read:chat` | `GetFullChat` / `GetFullChannel` | chat | не нужно | Отсеивать ссылки-приглашения |
| 2 | `chat.members.count` | `telegram.ext:read:chat` | `get_chat_members_count` | chat | не нужно | Только число |
| 3 | `chat.members.list` | `telegram.ext:read:members` | `GetParticipants` | chat | не нужно, но результат содержит пользователей | Нужно решение O-3: `user_ref` или без ID |
| 4 | `chat.pinned.list` | `telegram.ext:read:messages` | поиск с фильтром pinned | chat | не нужно | Результат помечается `untrusted` |
| 5 | `messages.history.live` | `telegram.ext:read:messages` | `GetHistory` | chat | не нужно | Для чатов с `not_synced` и `sync_gap`; дублирует ядро, поэтому спорная |
| 6 | `messages.get_many` | `telegram.ext:read:messages` | `GetMessages` | chat | не нужно | До 50 ID; обобщает `message.get` |
| 7 | `messages.search.live` | `telegram.ext:read:messages` | `messages.Search` с peer | chat | не нужно | Расширяет архивный поиск до живого |
| 8 | `messages.replies.list` | `telegram.ext:read:messages` | `GetReplies` | chat | не нужно | Тред под сообщением |
| 9 | `message.reactions.list` | `telegram.ext:read:messages` | `GetMessageReactionsList` | chat | не нужно | Содержит пользователей, как №3 |
| 10 | `user.info.get` | `telegram.ext:read:members` | `users.GetFullUser` | **user** | **нужно вне scope**: peer не из списка чатов | Не включать до решения O-3; вместо неё данные пользователя только внутри №3 |

Рекомендованный старт: №1, №2, №4, №6, №8 (нет пользовательских ID в ответе) плюс №3 после решения O-3. Записи №5 и №7 дублируют ядро и увеличивают нагрузку на сессию, откладывать. №10 в первую версию не входит.

В реестр **не** попадают: диалоги, глобальный поиск, общие чаты, журнал событий администратора, любые методы без `chat_ref`, любые `Send*`/`Edit*`/`Delete*`/`Forward*`/`Join*`/`Leave*`/`React*`, `account.*`, `auth.*`, `invoke` (MCC-11 п.1, 6).

## 7. Разрывы: что менять до M11.3

Размер: S — до дня, M — несколько дней, L — около недели и больше. Риск: низкий / средний / высокий.

| № | Разрыв | Файл | Размер | Риск | Пояснение |
| --- | --- | --- | --- | --- | --- |
| G1 | Код только в ветках, `main` пуст | репозиторий, ветки `claude/m11.1-telegram-service`, `claude/m13-railway` | S | средний | Решить, какую линию взять за базу M11.3 и влить её в `main`. Иначе ветка M11.3 растёт от незамерженной линии |
| G2 | Нет единого шлюза вызовов в Telegram: ни семафора, ни пейсинга, ни общего flood-состояния | новый модуль рядом с `transport.py`; затрагивает `live.py:55-69`, `sync.py:82-89`, `transport.py:107-173` | L | **высокий** | Одно место, через которое идут sync, live-чтение, ext и отправка: очередь на Connection, лимит одновременных запросов, проверка отсрочки до вызова. Отправка требует особой осторожности: `send` нельзя ставить в общую очередь так, чтобы он ждал и потом уходил с опозданием. Рекомендуется сначала приоритет для send |
| G3 | Flood-wait живого пути не записывается в общее состояние | `live.py:65-66`, `store.py` (+ новая SQL-функция в `db/`) | M | высокий | Нужна запись в `connection_state.retry_after` и из live, и из send; роль `gxg_telegram_service` сейчас пишет через функцию применения пачки. Нужна миграция, откат которой не ломает sync |
| G4 | Мета-tools `ext.search/describe/call`: схемы, manifest, обработчики | `gxg-manifest.yaml`, `schemas/`, `tools.py:186-213` | M | средний | `ext.call` принимает `{entry, args}`; схема выхода у каждого tool одна, а у записей разные |
| G5 | Вторая валидация аргументов по схеме записи и валидация выхода/размера | `tools.py:122-139` | M | средний | `TelegramTool.run` проверяет только `self.parameters`; `max_bytes` и `output_schema` не применяются (в ките они есть: `mcp.py:57`, `MAX_RESULT_BYTES`) |
| G6 | Загрузчик реестра и группы прав: ждут M11.2, нужен re-vendor кита | `vendor/gxg-connector-kit/`, `pyproject.toml:49-52`, `Dockerfile` | M | средний | Зависимость от M11.2 (AC-M11.2-03). `Action` в `grants.py:33` не знает групп. Bump = перевендорить и проверить byte-equal |
| G7 | Включение записи по tenant'у (`enabled_default: false`, `effect_disabled`) | `tools.py`, bundle | M | средний | Источник «запись включена для tenant'а» не определён: в bundle или в подписанном дополнении; нужна согласованная форма с Hub (AC-M11.3-03) |
| G8 | Живые обёртки чтения (история, инфо, участники, закреплённые, ответы) и расширение fake Telegram | `transport.py`, `tests/fake_telegram.py`, `live.py` | L | средний | По образцу `get_message`. Нужно проверить сигнатуры на `pyrofork==2.3.69`; peer берётся из кэша, не из сети, поэтому для участников нужна стратегия на случай `ArchivePeerUnavailable` |
| G9 | Сопоставление запись → разрешённые raw-функции, защита «0 write-вызовов» | `transport.py` (обёртка `invoke`) | S–M | низкий | Для AC-M11.3-04 лучше иметь не только тест, но и защиту во время выполнения: обёртка отказывается вызывать TL-функции не из allowlist записи. Тогда баг в обработчике не отправит сообщение |
| G10 | Аудит: имя записи и версия для успеха и отказа, allowlist ключей `fields` | `tools.py:115-152` | S | низкий | См. 4.3. Добавить тест на мутацию: убрать имя записи из аудита (AC-M11.2-04) |
| G11 | Пользовательские данные в ответах участников: `user_ref` или маскирование | новый модуль рядом с `chatrefs.py`, решение O-3 | M | высокий | Возвращать peer ID пользователя нельзя (MCC-04, `service-integration-v1.md` «Ограничения Telegram-фасада»). Если нужен стабильный идентификатор, вводится HMAC-ссылка по образцу `chatrefs.py` |
| G12 | Копия конвейера вызова вместо кита | `tools.py:102-152` (комментарий-shortcut `tools.py:105`) | M | средний | Автор сам пометил: конвейер скопирован с demo. M11.3 добавил бы третий вариант. Либо перейти на `GxgTool` кита (`mcp.py:335`), либо дописать только нужное и записать долг |
| G13 | Лимиты записей реестра (`max_results`, таймаут, размер ответа) | `tools.py:127-131`, `mcp.py` | S | низкий | Таймаут из manifest есть; лимиты записи надо читать из реестра, а не из `limits` tool'а `ext.call` |
| G14 | Нагрузочное влияние на archive-sync | `server.py:328-356` | M | средний | Если ext и sync идут через один аккаунт, flood-wait от ext остановит Signals-сбор. Тест AC-M11.3-07 должен проверять и это |
| G15 | README и manifest-описание сервиса ссылаются на старую ветку Hub | `README.md:3` | S | низкий | Ссылка ведёт на `claude/admiring-planck-vzys0x`; поправить до публикации ext-спеки |

Рекомендованный порядок: G1 → G2 и G3 (шлюз, до первой ext-записи) → G6 после M11.2 → G4, G5, G10, G13 → G8 и G9 по записям → G7, G11, G14.

## 8. Открытые вопросы к владельцу

- **O-1** (уже в плане): правка `resource-grants-v1.md` и схемы grants для групп `telegram.ext:read:*`. Аудит показывает, что без неё M11.3 не стартует.
- **O-2:** какой из рекомендованных наборов записей брать в первую версию. Предлагается №1, №2, №4, №6, №8.
- **O-3:** как отдавать участников и реакции. Варианты: (а) без идентификаторов пользователей, только число и роли; (б) `user_ref` по образцу `chat_ref`, обратимо внутри сервиса; (в) отложить `chat.members.list`, `message.reactions.list`, `user.info.get`.
- **O-4:** нужен ли общий шлюз (G2) как отдельная веха до M11.3. Моя оценка: да, иначе ext делит аккаунт с sync без защиты от flood-wait.
- **O-5:** какую ветку сервиса считать базовой и когда вливать её в `main` (G1).
- **O-6:** где хранится включение записи для tenant'а: в grants bundle Hub или отдельной частью bundle (G7). От ответа зависит AC-M11.3-03.

## 9. Что не проверено

- Сигнатуры и поведение методов Pyrofork на закреплённой версии `2.3.69` не проверялись запуском; таблица в п. 3.3 опирается на документированные свойства MTProto и должна быть сверена по исходникам библиотеки.
- Тесты сервиса не запускались, поэтому утверждения о поведении сделаны по коду и комментариям.
- Содержимое `reports/` и `stand/` просмотрено выборочно, без разбора результатов conformance.
