# M11 цикл 2 — ponytail-review (критик)

Дифф вехи: `fb14af1..11f61a8`, дифф цикла: `2cebc81..11f61a8`, ветка `claude/m11-telegram-read`. Нагрузка: dev-стенд; в production — несколько агентов, сотни вызовов в минуту на один процесс коннектора, архив ~47 тыс. сообщений (самопроверка исполнителя).

**What this change does:** Fake-порт заменён адаптером `ArchiveService`. Он читает базу `telegram_archive` существующего сервиса ролью `gxg_telegram_reader`, у которой есть только `SELECT` по колонкам (ADR-0004). Схема в тестах и на стенде собирается из дословных миграций gxg_agents_v2 @ f1c4d6d. Документы M4/M5 восстановлены и слиты с решениями владельца. На стенд добавлены база архива и тесты SC-01, SC-06, SC-08 и MC-01 для telegram.

Что проверено: SQL адаптера сверен с `archive-repository.ts` (search, folder collection). Схема и функции миграций проверены по коду. Права роли проверены на собственном кластере PG16: EXECUTE нет ни на одной функции `telegram_archive`/`capability_hub`, SECURITY DEFINER с EXECUTE — 0. Хеши vendored-миграций совпадают с клоном (`check_upstream.sh`). Порядок «grant до порта» соблюдён во всех трёх tools, как и маска с чатом вне scope. Отдельно проверены курсоры, срезание до экранирования, логи и список эквивалентных мутантов.

## Must fix

1. **Поиск теряет и дублирует результаты на равных ключах сортировки** (`archive.py: ArchiveService.search`, `ORDER BY ts_rank DESC, m.sent_at DESC, m.message_id DESC`; `tools.py: Connector.search`, offset-курсор)
   - **What this is:** Страница N запрашивает `offset + limit + 1` строк и отрезает первые `offset`.
   - **Problem:** `message_id` уникален только внутри чата. У двух супергрупп с одинаковыми `message_id` и `sent_at` (секундная точность Telegram) при равном `ts_rank` порядок не определён. Top-N сортировка PostgreSQL расставляет такие строки по-разному при разном `LIMIT`. Проверено на PG16: 2 чата × 200 сообщений с равными ключами, листание как у коннектора. При странице 3 выдано 102 элемента, уникальных 93; при странице 5 — 100 и 94. Часть сообщений не попала ни на одну страницу, часть пришла дважды.
   - **Fix:** Добавить в `ORDER BY` `c.chat_ref` как последний ключ. Это одно слово, и порядок становится полным.
   - **If we skip it:** В редких случаях агент не увидит совпадение и решит, что его нет, хотя `complete: true`.

## Should fix

2. **Об изменении схемы архива в gxg_agents_v2 никто не узнает** (`connectors/telegram/archive/SOURCE.md`, `check_upstream.sh`, `test_vendored_schema_is_the_pinned_upstream`)
   - **What this is:** Схема архива — контракт между двумя сервисами. ADR-0004 («Последствия») требует две вещи: контрактный тест по закреплённому commit **и уведомление при обновлении**.
   - **Problem:** Обе проверки сравнивают vendored-файлы с тем же закреплённым commit, то есть сами с собой. Новые коммиты gxg_agents_v2 не смотрит ничего. Пример: в gxg_agents_v2 выходит миграция, которая переименовывает `messages.text` или переносит purge при снятии чата со scope в фоновую задачу. CI Hub остаётся зелёным. В production коннектор либо отвечает `temporarily_unavailable` на каждый вызов, либо (во втором случае) отдаёт по маске grant'а чат, уже выведенный из scope. Ведь вывод «в архиве только чаты scope» держится только на триггере `purge_out_of_scope`.
   - **Fix:** Плановая проверка (cron job или шаг `check-all`): `git -C <clone> fetch && git log <pinned>..origin/main -- supabase/migrations` по файлам, которые упоминают `telegram_archive` или `capability_hub.connections`. Если список не пуст — красный статус и сообщение. Готовый вариант: git submodule с закреплённым commit плюс Dependabot или Renovate (ecosystem `gitsubmodule`). Он сам открывает PR при обновлении.
   - **If we skip it:** Расхождение схемы обнаружится в production, а не в CI.

3. **Чат из scope, ещё не синхронизированный, отдаёт `not_found` вместо «неполного» ответа** (`archive.py: _state`, `len(rows) != len(set(refs))` → `chat_forbidden`)
   - **What this is:** Строка `telegram_archive.chats` появляется только после первой синхронизации чата (комментарий в миграции: «A row exists only for an explicit scope chat that has been synced at least once»).
   - **Problem:** Оператор добавил чат в scope и выдал grant. До первого прохода archive-sync `thread.read` и `collect` отвечают `not_found`. Поиск с этим чатом в `chat_refs` тоже отвечает `not_found` целиком, вместе с соседними чатами. Настоящий `search` сервиса в этом случае отвечает `not_synced` (`archive-repository.ts:325-327`). Безопасность не страдает (fail closed), но контракт расходится с реальным портом, а runbook README называет такой чат «вне scope».
   - **Fix:** Записать это расхождение в README и в описание tool'а. Или, после решения владельца, отдавать `complete: false` / `other`, но только при доступе к scope (`capability_hub.connections.metadata`), которого у роли нет.
   - **If we skip it:** Новые чаты в первые минуты выглядят как «нет доступа».

4. **`collect` целиком блокируется на время flood-wait, хотя данные уже в архиве** (`archive.py: collect`, `if wait is not None: raise ServiceError("flood_wait")`)
   - **What this is:** Это трактовка AC-M11-04 для чтения архива, записанная в плане цикла.
   - **Problem:** `retry_after` у Connection ставит любой flood-wait синхронизации, даже по чужому чату этого аккаунта. Верхняя граница — 86400 с (`apply_chat_batch`). Всё это время Signals не забирает уже сохранённые и ещё не собранные сообщения, хотя `search` и `thread.read` отдают их с `rate_limited` в `partial_reasons`. Существующий сбор Chat folders (`readFolderMessages`) в этом случае не блокируется. Решение сознательное, но в спеку (service-integration-v1, Telegram) оно не внесено.
   - **Fix:** Записать решение в service-integration-v1 рядом с `collect` или отдавать страницу с `partial_reasons: ["rate_limited"]`, как в двух других tools.
   - **If we skip it:** Свежесть Signals падает на длительность flood-wait, до суток.

5. **Ключ дедупликации и контракт «сбор по чату» не записаны в спеку** (`docs/specs/service-integration-v1.md`, Telegram; `reports/M11-ESCALATION.md` #4)
   - **What this is:** Пункт 3 чек-листа цикла 1 требовал записать в service-integration-v1 одно из двух: сбор по Chat folders или одобренный сбор по чату со сопоставлением ключей для M12.
   - **Problem:** Решение «по чату, ключ `chat_ref:message_id`, сопоставление — в M12» записано только в отчёте эскалации и в progress.md оркестратора. В спеке у `collect` стоит лишь «cursor-based сбор из архива». Исполнитель M12 этого ограничения в спеке не увидит, а AC-M12-01 сравнивает ключи `peer_id:message_id`.
   - **Fix:** Одна строка в service-integration-v1 под таблицей tools.

6. **Копия конвейера вызова и конфигурации из demo** (`tools.py: TelegramTool.run`, `server.py: Settings/parse_keys/BundleFiles/NoVerifier`) — помечена `shortcut:` и стоит в backlog (F7). Lean ~150 строк. Не блокирует.

## Nice to have

7. **Доказательства исполнителя собраны из нескольких частичных прогонов** (`reports/conformance/M11.json`: `commit` 7d90948, `ci_local.runs`)
   - **What this is:** DoD-1 (решение владельца) требует зелёного локального `check-all.sh`.
   - **Problem:** Ни один прогон исполнителя не прошёл целиком на одном commit: на 7d90948 упал mutmut, на b1976fd стенд не запускался. В AC-M11-01 написано «105 passed», а telegram-тестов 104. Корневой pytest критика — 902 passed, исполнитель пишет 903.
   - **Fix:** Записывать `ci_local` одним прогоном на HEAD. Критик сделал такой прогон в этом цикле (см. JSON).

8. **Счётчик AC-M11-03 на стенде считает подключения, а не запросы** (`stand/tests/test_telegram.py: requests`). Сейчас это одно и то же: shortcut «одно подключение на вызов». С пулом (`psycopg_pool`, следующий шаг из самопроверки) счётчик перестанет что-либо доказывать. При введении пула тест надо перевести на `pg_stat_statements` или `log_statement`.

Verdict: fix 1 and 2 first.
Lean: -150 lines possible.
Not checked: реальная база Supabase (выдача `BYPASSRLS` и права PUBLIC в чужих схемах), GitHub Actions (нет минут), производительность на production-объёме.
