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
| M5 | final | reports/critic/M5-final.json | 3 major missed_earlier: (1) fail closed при выключенном/упавшем плагине проверен только конфигурацией и вручную — нужен тест стенда; (2) SC-09 не поймает проброс Authorization (demo не логирует заголовки) — нужен эхо-коннектор/проверка заголовков; (3) unified_pdp завязан на имена tools, переименование/регистрацию gateway агентом держит только роль viewer — тест для роли member. 3 minor: необработанные исключения плагина, недостижимая проверка context_tenant, runbook рестарта после > 24 ч | До M14 (hardening); (2) и (3) — до M11 |
| M7 | reports/critic/M7-final.json | major missed_earlier: мутант `execution_deadline = now() + interval '1 hour'` (вместо окна, зафиксированного на T1) выживает — тест T2 использует умолчание 3600; minor: выживают gxg_cancel без webhook-outbox, reject требует step-up, decided_by из последнего правила; схема MCP не совпадает с ответом api | В M8 (execution window — путь исполнения) |
| M9 | reports/critic/M9-cycle-1.json | F8: `/passkeys` регистрирует ключ любому вошедшему без роли и без повторного входа (украденная сессия → свой passkey → step-up) | AS-30 (M10): регистрация через `decider` с подтверждением вне `ui` |
| M9 | reports/critic/M9-cycle-1.json | F10: в тексте Telegram `title`/`target_label`/`requester` без маркеров невидимых символов (U+202E переворачивает получателя в уведомлении; UI показывает маркер) | Та же замена Cc/Cf/Zl/Zp → `⟦U+XXXX⟧` в `bot.message` |
| M9 | reports/critic/M9-cycle-1.json | F11: `oidc-egress` — реле на один хост; IdP с discovery/token/JWKS на разных хостах не войдут | Реле на хост или egress-прокси после выбора IdP; Tailscale-модель — M14 |
| M9 | reports/critic/M9-cycle-1.json | F12: у `notifier` нет сигнала живости (спека: `/healthz` у каждого процесса vs AS-29 «входящих нет») | Heartbeat-файл + Docker HEALTHCHECK (exec) или решение владельца |
| M9 | reports/critic/M9-cycle-1.json | Скомпрометированный `notifier` может пометить доставленной любую строку outbox, в т.ч. `execute` (права AS-27 на все topics с M7) | Ограничение по topic (функция или RLS) — решение по матрице AS-27, вместе с M8/M10 |
| M9 | reports/critic/M9-cycle-2.json | N1: при отсутствии `GXG_BUNDLE_*` ui падает с `KeyError` (общий загрузчик kit), а не с «missing configuration» | Читать три имени через `env()` в `cli.main` или `SystemExit` в загрузчике kit; добавить в `REQUIRED` |
| M9 | reports/critic/M9-cycle-2.json | N2: тесты оставляют поток перезагрузки bundle (50 мс) и открытые пулы psycopg до конца сессии | Stop-событие для загрузчика в тестах; закрывать/подменять `ConnectionPool` в фикстуре cli |
| M9 | reports/critic/M9-cycle-2.json | N3: цикл уровней httpx/httpx2/httpcore в `ui/app.py` без теста | Одна проверка уровней, как `test_bot_module_sets_the_httpx_level` |
| M9 | reports/critic/M9-cycle-2.json | N4: `approval/config.bundle_store` — однострочная обёртка | Звать `bundle_store_from_env` напрямую, обёртку удалить |
