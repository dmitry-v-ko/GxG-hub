# M6, цикл 2 — план (ponytail full)

Чек-лист — `required_improvements` критика цикла 1 (`reports/critic/M6-cycle-1.json`, оценка 5.0, cap: AC не выполнены). Пункты 1–4 — `blocked_by_access` (нет доступа к `gxg_agents_v2`), в этом цикле не выполнимы. Делаем 5–9 и дешёвые minor.

| # | Пункт | Что делаем | Ступень |
| --- | --- | --- | --- |
| 1–4 | Аудит, клиент реальной KB, AC-M6-03, AC-M6-04 | Не выполнимы без доступа: `reports/M6-ESCALATION.md` | — |
| 5 (F5) | Порт KB синхронный, без лимита | `Backend` — async; `search(..., offset, limit)` → `(документы, есть ли ещё)`: KB отдаёт не больше `limit` (+1 для признака). Тесты: медленный async backend → `temporarily_unavailable` по таймауту manifest; backend просят не больше `limit+1` | минимальный код |
| 6 (F2) | Данные KB не ограничены схемами выхода | `title` ≤ 300, `content` ≤ 20000 в `get`, поиске и контексте; в выходе `get` поле `truncated`. Тест с длинным документом | одна строка на поле |
| 7 (F3) | Нет `bundle_version` в аудите | `ready()` возвращает версию bundle, `KbTool.run` пишет её в строку аудита | одна строка |
| 8 (F4) | `server.py` вне mutmut | `server.py` в `only_mutate`; настройка логов — функция `log_handlers()`, тесты: выбор verifier'а (ContextForge-ключи: принят токен адаптера, отклонён чужой `aud`), профиль, `main()`. Комментарий `mutmut.sh` | установленные зависимости, тесты |
| 9 (F6, F7) | M5.json переписывается, `\|\| true`, CI не выгружает M6.json, таймаут | M5-отчёт только из M5-тестов (`--exclude stand.tests.test_kb`), M5.json в коммитах M6 не меняется; без `\|\| true` (код выхода — любой красный отчёт); CI выгружает M6.json; таймаут job 45 мин | конфиг |
| F12 | `/readyz` не проверяет KB | порт `Backend.ready()`; `/readyz` = bundle и KB | одна строка |
| F13 | Самописный разбор `--milestone` | `argparse` (stdlib) | stdlib |
| F8 | Слияние grants глушит конфликты | ошибка при одинаковых `id` групп и connections | две строки |
| F14 | Строка M5 final в backlog — 5 ячеек | 4 ячейки | правка |
| F11 | Ключ курсора на процесс | `shortcut`-комментарий (как demo) | комментарий |
| F9, F10, F15 | Копия из demo; `kb_calls`; `kb.context.get` | backlog (`shortcut` уже записан; F15 — `pending_owner_decision`) | — |

## DoD-6

| Компонент | Рассмотрено | Решение |
| --- | --- | --- |
| Разбор аргументов `report.py` | `argparse` | stdlib |
| Таймаут вызова KB | `asyncio.wait_for` (уже есть) | теперь прерывает async порт |
| Вынос plumbing в kit | `gxg-connector-kit` | не в этом цикле: требует fastmcp/логов в kit и правки demo с его списком эквивалентов мутантов; `server.py` покрывается тестами kb и mutmut |
