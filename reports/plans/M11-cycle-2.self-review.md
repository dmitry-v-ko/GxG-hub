# M11 цикл 2 — самопроверка: ponytail-review своего диффа

Дифф: `2cebc81..HEAD` (цикл 2) ветки `claude/m11-telegram-read`. Нагрузка: dev-стенд; в production — сотни вызовов в минуту от нескольких агентов на один процесс коннектора.

**What this change does:** Коннектор читает архив `telegram_archive` существующего сервиса напрямую ролью только на чтение (ADR-0004) вместо fake-порта. Схема базы — дословные миграции gxg_agents_v2; тесты в процессе идут на настоящем PostgreSQL, стенд получил базу архива, живые тесты проверяют отказы на реальной базе (остановка, блокировка таблицы, flood-wait в `connection_state`). Документы M4/M5 восстановлены и слиты с решениями владельца.

**Should fix**

1. **Копия конвейера вызова и конфигурации из demo** (`tools.py: TelegramTool.run`, `server.py: Settings/parse_keys/BundleFiles/NoVerifier`)
   - **What this is:** Порядок проверок и загрузка bundle.
   - **Problem:** ~150 строк повторяют `connectors/demo`; правка в одном месте не попадёт в другое.
   - **Fix:** В `gxg-connector-kit` вместе с шаблоном коннектора. Записано в `docs/backlog.md` (M11, F7), в коде — `shortcut:`.
   - **If we skip it:** Третий коннектор скопирует ещё раз.

**Nice to have**

2. **Подключение к базе на каждый вызов** (`archive.py: ArchiveService._run`)
   - **What this is:** Каждый вызов tool'а открывает и закрывает соединение (`shortcut:` в коде).
   - **Problem:** Под нагрузкой — лишние миллисекунды на TLS/аутентификацию; у роли `CONNECTION LIMIT 32`, за пределом вызов получает `temporarily_unavailable`, а не очередь.
   - **Fix:** `psycopg_pool.AsyncConnectionPool` с `max_size` ниже лимита роли, когда нагрузка это покажет.
   - **If we skip it:** При всплеске > 32 одновременных вызовов часть получит повторяемую ошибку.

3. **Повторная выборка при листании поиска** (`tools.py: Connector.search`)
   - **What this is:** У архива нет offset в запросе сервиса; страница N читает `offset + limit + 1` строк.
   - **Problem:** Квадратично по страницам, но предел — 100 результатов.
   - **Fix:** Не нужен при этом пределе.

Проверено также: grant до любого запроса к архиву во всех трёх tools (in-process счётчик порта и на стенде — счётчик подключений `application_name=gxg-telegram` в `log_connections`); маска grant'а и чат вне scope → `not_found` во всех tools; удаление чата из scope Connection → триггер сервиса очищает архив → `not_found`; роль не пишет даже с выключенным `default_transaction_read_only` и не читает `author_id`, `embedding`, `capability_hub`; DSN, пароль роли и текст ошибок PostgreSQL не попадают в логи (только класс исключения); позиция сбора считается одним выражением в курсоре и в сравнении; обрезка текста до экранирования; flood-wait не укорачивается (ceil, ≥ 1).

Verdict: Ship.
Lean: -150 lines possible (после выноса в kit).
Not checked: архив настоящего сервиса (только локальная база по его миграциям, ADR-0004); производительность полнотекстового поиска на 47 тыс. сообщений production.
