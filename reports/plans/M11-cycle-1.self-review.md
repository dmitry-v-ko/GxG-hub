# M11 цикл 1 — самопроверка: ponytail-review своего диффа

Дифф: `fb14af1..HEAD` ветки `claude/m11-telegram-read`. Нагрузка: dev-стенд и один процесс коннектора на tenant'ов стенда; в production — сотни вызовов в минуту от нескольких агентов (оценка по M5).

**What this change does:** Добавляет MCP-коннектор `telegram` с тремя read-tools поверх порта существующего Telegram-сервиса (`TelegramService`) и его fake для тестового профиля. Подключает коннектор к стенду M5 (ContextForge, PDP, bundle) и добавляет живые тесты SC/MC/AC-M11 и два пункта бэклога M5.

**Should fix**

1. **Копия конвейера вызова и конфигурации из demo** (`connectors/telegram/src/telegram_connector/tools.py:TelegramTool.run`, `server.py: Settings/parse_keys/BundleFiles/NoVerifier`)
   - **What this is:** Порядок проверок (identity → схема → bundle → handler → ошибки → аудит) и загрузка bundle.
   - **Problem:** ~170 строк повторяют `connectors/demo`. Исправление в одном месте не попадёт в другое.
   - **Fix:** Вынести в `gxg-connector-kit` вместе с шаблоном коннектора (mcp-toolkit-v1). Сейчас помечено `shortcut:` в обоих местах и покрыто тестами в каждой копии (mutmut: 0 неэквивалентных выживших).
   - **If we skip it:** Третий коннектор скопирует ещё раз.

**Nice to have**

2. **Повторная выборка при листании поиска** (`tools.py: Connector.search`)
   - **What this is:** Порт сервиса не умеет offset, поэтому страница N запрашивает `offset + limit` результатов.
   - **Problem:** Квадратично по страницам, но предел — 100 результатов (2–14 запросов на весь набор).
   - **Fix:** Offset в порту сервиса, когда появится сетевой endpoint.
   - **If we skip it:** Несколько лишних строк из индекса на страницу.

3. **Обрезка после экранирования** (`tools.py: _message`, excerpt)
   - **What this is:** Текст сначала экранируется (`\u{202E}`), потом режется до 4000/500 символов.
   - **Problem:** Срез может пройти посреди `\u{202E}` и оставить `\u{20`. Опасного символа не остаётся, это косметика.
   - **Fix:** Не нужен.

Проверено также: grant до любого запроса к порту на всех трёх tools (AC-M11-03, тесты и mutmut), повторная проверка каждого элемента поиска (GR-16), подделанный/чужой `chat_ref`, маска grant'а (чат вне scope сервиса → `not_found`), cursor поиска привязан к caller, запросу и offset, cursor сбора не уходит назад, flood-wait не укорачивается (ceil, ≥ 1), в аудите нет запроса, текстов, peer ID; заголовок `Authorization` фиксируется только как флаг.

Verdict: Ship.
Lean: -170 lines possible (после выноса в kit).
Not checked: работа против настоящего сервиса — сетевого порта нет (reports/M11-ESCALATION.md #1).
