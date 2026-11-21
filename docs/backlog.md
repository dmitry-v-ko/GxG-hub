# Бэклог

Замечания `minor` из закрытых вех и находки «Should fix» аудитов. Формат: веха, источник, суть, предлагаемое исправление.

| Веха | Источник | Суть | Исправление |
| --- | --- | --- | --- |
| M0 | reports/critic/M0-cycle-3.json | Исключённая цитата заголовка вырезается из плана в любом месте: можно переместить её и добавить тело ключа с END-строкой | Вырезать цитату только в строке `\| AC-M0-04 \|` или ловить END-строку приватного ключа; один тест |
| M0 | reports/critic/M0-cycle-2.json | Сжатые файлы (gzip) не сканируются | Записано в known_limitations; общий сканер (например, gitleaks) — отдельной задачей |
| A | reports/audit/stage-A.md | `error.schema.json` не связывает `retryable` с `timeout_before_start` | if/then в схеме |
| A | reports/audit/stage-A.md | `conftest.py` не блокирует UDP `sendto` | Расширить блок сети |
| A | reports/audit/stage-A.md | Пример политики в спеке и `policies/approval/example.yaml` называют разные чаты | Выровнять |
| M1 | reports/critic/M1-cycle-3.json | `uv run mutmut run` в каталоге пакета падает без корневого `uv sync` | Документировать запуск из корня или добавить mutmut в dev-группу пакета |
| M1 | reports/critic/M1-cycle-3.json | Свой `check_coverage.py` (75 строк), хотя есть coverage-threshold (per-file branch min) | Заменить на coverage-threshold |
| M1 | reports/critic/M1-cycle-3.json | Файл состояния GR-13a переименовывается без fsync; после сбоя питания пустой файл блокирует старт | fsync файла и каталога; явная процедура восстановления |
| M1 | reports/critic/M1-cycle-3.json | OSError при записи состояния выходит из load() не как BundleError | Оборачивать в BundleError с причиной |
| M4 | reports/critic/M4-cycle-3.json | `check_verdicts.py` оценивает только вызовы, дошедшие до echo; вызов через ContextForge с иной ошибкой (Unknown tool, 5xx) не считается FAIL | В M5/MC-05: каждый ожидаемо принятый вызов обязан дойти до коннектора |
| M0.1 | reports/critic/M0.1-cycle-4.json | Opt-out сети через `request.getfixturevalue('socket_enabled')` в теле теста или фикстуре не ловится | Проверять также runtime-запрос фикстуры (хук на fixture setup) |
| M0.1 | reports/critic/M0.1-cycle-4.json | pytest-socket не блокирует сеть при импорте модуля и в дочерних процессах; CI запускает тесты с полной сетью | Сетевой sandbox CI-job (например, `--network none` в контейнере) |
| M0.1 | reports/critic/M0.1-cycle-4.json | Устаревшие base/notes в отчёте и mutmut-заметке | Поправить при следующем касании |
| M2 | reports/critic/M2-cycle-5.json | Устаревшая ссылка на `dd0beb1` в отчёте conformance | Поправить при следующем касании |
| M4.1 | reports/critic/M4.1-cycle-3.json | ADR-0002: стоимость ContextForge = 0 не учитывает работу M5 (unified_pdp, сетевая изоляция), статус «частично» для п.4 не из списка; webhook W пропускает не-tools/call методы; GHSA без patched-версий; run.sh midsession падает без WEBHOOK_DOWN | Уточнить ADR-0002 при принятии решения; починить скрипт |
| M3 | reports/critic/M3-cycle-3.json | Логи: стек internal_error многострочным текстом в stderr (`tools.py:93-94`); корневой логгер INFO пускает текстовые строки FastMCP в stdout рядом с JSON-аудитом (`server.py:175-181`) | Однострочный JSON для ошибок, поднять уровень логгеров SDK |
| M5 | reports/critic/M5-cycle-2.json | F11: чужой virtual server по SSE (`/servers/{other}/sse`) не в тесте SC-01; `Stand._sse` не возвращает статус отказанного handshake (путь закрыт, проба критика: 403) | Вернуть статус из `_sse`, добавить путь в `test_sc01_other_team_tool_is_refused_by_name_on_every_path` |
| M5 | reports/critic/M5-cycle-2.json | F12: в `reports/conformance/M5.json` ссылка на CI-прогон цикла 1; зелёного CI на голове цикла 2 нет (сбой раннеров GitHub) | Перезапустить CI на голове до PR в main, записать run id |
| M5 | reports/critic/M5-cycle-2.json | F13: `stand/README.md:32` — «один раз» про `init.py`, а bundle переподписывается при каждом запуске | Поправить формулировку |
| M5 | reports/critic/M5-cycle-1.json | F6: код таймаута read после отправки (MCC-05) — вопрос владельцу (`reports/M5-ESCALATION.md` #4) | После решения владельца |
| M5 | final | reports/critic/M5-final.json | 3 major missed_earlier: (1) fail closed при выключенном/упавшем плагине проверен только конфигурацией и вручную — нужен тест стенда; (2) SC-09 не поймает проброс Authorization (demo не логирует заголовки) — нужен эхо-коннектор/проверка заголовков; (3) unified_pdp завязан на имена tools, переименование/регистрацию gateway агентом держит только роль viewer — тест для роли member. 3 minor: необработанные исключения плагина, недостижимая проверка context_tenant, runbook рестарта после > 24 ч | До M14 (hardening); (2) и (3) — до M11 |
