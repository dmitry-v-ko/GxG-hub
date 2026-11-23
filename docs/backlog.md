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
| M9 | reports/critic/M9-final.json | minor: access log uvicorn пишет `?code=` OIDC callback (SEC-17 — исправить в M10); outbox уведомлений хранит title/requester бессрочно (очистка — M10, RET-01); кнопки решения видны approver'у вне замороженного набора (UX); двойной префикс в логе getUpdates; gitleaks на shallow clone ловит граничные коммиты. threat_model open: bidi-символы в тексте Telegram, step-up через passkey с украденной сессией и компрометация `ui` (закрывает AS-30 в M10), нет серверного отзыва сессии | M10 (access log, очистка outbox, AS-30, отзыв сессий) |
| M11 | reports/critic/M11-cycle-4.json | minor: The bare 'search_path' match flags any migration with a SECURITY DEFINER function (SET search_path = pg_catalo; Unquoted word splitting and git's C-quoting of non-ASCII names: an archive rename in '20270101 next.sql' or '2; Quoted or mixed-case identifiers pass: alter table "capability_hub"."connections" drop column "metadata" (supa | при следующем касании |

## M12 (критик цикла 1, 2026-11-23; ветка gxg_agents_v2 codex/m12-signals-gxg-hub @ 1487186)

- Счётчик старого пути перенести в legacy-handler'ы (`run-action-workflow.ts:353`), чтобы AC-M12-05 мерил исполнение, а не конфигурацию.
- AC-M12-01: фикстура соответствия `peer_id:message_id` ↔ `chat_ref:message_id`, legacy-сторона через настоящий `HubCollectionDriver`, медиа без текста и edited.
- AC-M12-06: отдельный harness без webhook'ов со сравнением множеств; случай «чат в конфигурации без grant» через webhook.
- AC-M12-02: assert, что множество `business_task_id` равно множеству id кандидатов.
- Верхняя граница или alert для бесконечно отложенных gxg-кандидатов (`hub_transport_failed`).
- Секундный cron Signals: wakeup по `next_poll_at` или не создавать run при пустой выборке.
- Webhook для tenant'а не того workspace: 2xx-ignore или 403 вместо 503.
- mastra typecheck зависит от `frontend/node_modules`: `npm ci` в обоих каталогах в CI.
- M12 ц2 (критик 8.5): при deferral записывать safe-код последней ошибки и время начала ожидания; метрика/эскалация для intent, ждущего дольше `expires_at` + запас.
- M12 ц2: reconciliation для кандидата, терминализированного по `permission_denied`/`not_found`, если Hub позже вернёт терминальный статус intent'а.
- M12 ц2: один источник поддерживаемых `action@version` для фильтра выборки и для claim.
- M12 ц2: индекс/сгенерированная колонка под фильтр `action_key@version` при росте очереди.
- M12 ц2: явно ограничить клиентские повторы `telegram.message.propose` по `internal_error`.
