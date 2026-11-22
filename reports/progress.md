# Журнал выполнения плана

Процесс: [План реализации → Процесс исполнения вех](../docs/implementation-plan-v1.md#процесс-исполнения-вех). Порог закрытия вехи — оценка критика ≥ 9.

## Ждёт владельца

Работа не блокируется: идём по варианту по умолчанию (`pending_owner_decision`). **2026-11-21 владелец поручил оркестратору решить открытые вопросы самому** — решения ниже, записаны в спеки/план.

| Вопрос | Решение | Где |
| --- | --- | --- |
| AC-M0-03, AC-M2-04: защита `main`, метка `widening-approved`, проверка на PR | ждёт владельца (ручные действия в GitHub) | — |
| Мерж `claude/integration` в `main` | ждёт владельца | — |
| Approver для пилота M13 | ждёт владельца | только M13 |
| CI без раннеров (минуты GitHub Actions исчерпаны) | веха закрывается по зелёному локальному `check-all.sh` у исполнителя и критика; GitHub CI — перед мержем в `main` | DoD-1 |
| Доступ к `gxg_agents_v2` | владелец: «кажется открыт»; M6 повторяет `add_repo` | M6 |
| Как Hub читает Telegram | отдельный сервис-коннектор по MCC; чтение из архива ролью только `SELECT`; live — до M13 отдельно | ADR-0004 |
| M11: тестовый аккаунт; RID чата | реальный аккаунт не нужен (коннектор не говорит с Telegram); RID = `chat_ref` | ADR-0004, GR |
| EFF-08 идемпотентность | UNIQUE (tenant, requester, key) | EFF-08, DDL |
| Компрометация `ui` (M9-1) | процесс `decider`, WebAuthn на каждое решение | AS-30, AC-M10-08 |
| M9-2…5 | заморозка approver'ов при T1; `link_codes`/`webauthn_credentials` в AS-27; canonical args в preview-конверте; E2E-драйвер до M8 | AS-27 |
| SEC-20 отзыв токена | ≤ 5 с | SEC-20 |
| M5-1 MC-04, M5-2 коды CF, M5-4 таймаут read | вариант по умолчанию принят; MCC-05: `temporarily_unavailable` и для таймаута read после отправки | MCC-05 |
| M7: компрометация `ui` через `gxg_decide` | закрывается AS-30 | AS-30 |
| Сеть через Tailscale | принято | ADR-0003, AC-M14-04/08/09 |
| CI: GitHub Actions не будет | решено владельцем: локальный гейт мержа `reports/merge-gate/<sha>.md` (DoD-1); AC-M0-02/03, AC-M2-04 — процедурно, без блокировки на стороне GitHub | ничего |
| M6 `blocked_on_owner`: репозиторий kb_access (read) + DDL схемы kb, или решение, что сервер KB делается в Hub; правило allow для add_repo | ждёт владельца | AC-M6-04, закрытие M6 |
| M6-Q5/Q7: где код сервера KB (`kb_access`) и DDL схемы `kb` — в gxg_agents_v2 их нет | ждёт владельца: нужен репозиторий kb_access (read) | AC-M6-04 |
| M6-Q1–Q4, Q6 | по умолчанию из аудита: коллекция = entity_type.key; одна connection на tenant; читателям только published; write-tools скрыты от read-ключа; миграции только для тестов | ничего |
| M11-4: ключ дедупликации Telegram для Signals (`chat_ref:message_id` вместо `peer_id:message_id`) | решается в задании M12: сопоставление ключей один раз или переход на новый ключ | M12 |
| M11-5: роль `gxg_telegram_reader` в production (нужен `BYPASSRLS`, выдаёт суперпользователь Supabase) | принято: оператор базы выполняет `connectors/telegram/archive/reader_role.sql`, пароль из secret store; миграций в gxg_agents_v2 нет | ручное действие при развёртывании |

| Веха | Цикл | Роль | Ветка | Commit | Оценка | Решение | Дата |
| --- | --- | --- | --- | --- | --- | --- | --- |
| M0 | 1 | исполнитель | claude/m0-foundation | — | — | запущен (session_01V6d5A8r9tNJf6eQbcBq8sx) | 2026-11-20 |
| M0 | 1 | исполнитель | claude/m0-foundation | f5cf832 (+9d34699 отчёт) | — | завершён: AC-01/02/04 passed, AC-03 manual_pending | 2026-11-20 |
| M0 | 1 | критик | claude/m0-foundation | f5cf832 | — | запущен (session_01V6d5A8DktDagTDtC6VYQvn) | 2026-11-20 |
| M0 | 1 | критик | claude/m0-foundation | e12a7b7 (отчёт) | **5.0** | < 9: critical (сканер секретов), 3× major → цикл 2 | 2026-11-20 |
| M0 | 2 | исполнитель | claude/m0-foundation | — | — | запущен (session_01V6d5A61wg1j1tHkTW5CR9K); ponytail подключается с этого цикла | 2026-11-20 |
| M0 | 2 | решение владельца | — | — | — | Защита main: обязательный `ci-ok` + CODEOWNERS, без обязательного одобрения; AC-M0-03 и AC-M2-04 обновлены | 2026-11-20 |
| M0 | 2 | исполнитель | claude/m0-foundation | 5a92948 (+e296877 отчёт) | — | улучшения 1–6 сделаны, CI 44785143490 зелёный; дополнения (ponytail, решение по защите) не применены → доработка в том же цикле | 2026-11-20 |
| — | — | цель | — | — | — | Цель поставлена: [GOAL.md](GOAL.md), регулярная проверка раз в час | 2026-11-20 |
| M0 | 2 | исполнитель | claude/m0-foundation | 77662af | — | дополнения применены: ponytail, план цикла, branch-protection по решению владельца | 2026-11-20 |
| M0 | 2 | критик | claude/m0-foundation | 77662af | — | запущен (session_01V6d5AANLd1s3ZC9NJriZ8M), с ponytail-review | 2026-11-20 |
| M0 | 2 | критик | claude/m0-foundation | 77662af (отчёт a0e3777) | **8.0** | < 9: 2× major (allowlist целиком на план; шаг CODEOWNERS ненаблюдаем при self-PR) → цикл 3; формулировка AC-M0-03 — на согласование владельцу | 2026-11-20 |
| M0 | 3 | исполнитель | claude/m0-foundation | — | — | запущен | 2026-11-20 |
| M0 | 3 | решение владельца | — | — | — | Новая формулировка AC-M0-03 согласована (CODEOWNERS проверяется наблюдаемо, без запроса ревью) | 2026-11-20 |
| M0 | 3 | исполнитель | claude/m0-foundation | 0678a5f (+a52dc73 отчёт) | — | оба major устранены, CI 44787408919 зелёный; ждём применения согласованного AC-M0-03 | 2026-11-20 |
| M0 | 3 | исполнитель | claude/m0-foundation | 2ab1faf (+88d3f1f отчёт) | — | согласованный AC-M0-03 применён, CI 44788073458 зелёный | 2026-11-20 |
| M0 | 3 | критик | claude/m0-foundation | 2ab1faf | — | запущен | 2026-11-20 |
| M0 | 3 | критик | claude/m0-foundation | 2ab1faf (отчёт 6b3fe48) | **9.5** | ≥ 9 — **веха закрыта** (AC-M0-03 ждёт ручной настройки владельцем, мерж в main — владелец); minor → backlog | 2026-11-20 |
| M1 | 1 | исполнитель | claude/m1-connector-kit | — | — | запущен (session_01V6d5A8BqncdPhSaqQg4TJn), ponytail ultra | 2026-11-20 |
| M2 | 1 | исполнитель | claude/m2-policy-tooling | — | — | запущен (session_01V6d5A6zfJPN82YUQCUJrgv), ponytail ultra | 2026-11-20 |
| M4 | 1 | исполнитель | claude/m4-contextforge-spike | — | — | запущен (session_01V6d5AA9fNoSEufu9cHpFtc), спайк | 2026-11-20 |
| M2 | 1 | исполнитель | claude/m2-policy-tooling | 63d7c18 (+8c180c3 отчёт) | — | AC-01/02/03/05/06 passed, AC-04 partial (нужен реальный PR, метка, secret) ; CI 44792404466 зелёный | 2026-11-20 |
| M2 | 1 | критик | claude/m2-policy-tooling | 63d7c18 | — | запущен | 2026-11-20 |
| M1 | 1 | исполнитель | claude/m1-connector-kit | 52874f5 (+672f4ba отчёт) | — | AC-M1-01…09 passed, CI 44793665616 зелёный; спеки уточнены оркестратором: AS-06a (kid service JWT), GR-13a (переподпись bundle ≤ 12 ч, анти-откат) | 2026-11-20 |
| M1 | 1 | критик | claude/m1-connector-kit | 52874f5 | — | запущен (session_01V6d5AAYpQnkTuxAUe72eQu) | 2026-11-20 |
| M2 | 1 | критик | claude/m2-policy-tooling | 63d7c18 (отчёт 409bf08) | **5.0** | < 9: AC-M2-04 failed — critical (зелёный ci-ok на head SHA через push-run), 3× major → цикл 2 (+GR-13a) | 2026-11-20 |
| M2 | 2 | исполнитель | claude/m2-policy-tooling | — | — | запущен | 2026-11-20 |
| M4 | 1 | исполнитель (live, Mac) | claude/m4-live | — | — | работает локально (session_01V6d5A8g3vkdYrkBpSzDJLf), под наблюдением оркестратора | 2026-11-20 |
| M1 | 1 | критик | claude/m1-connector-kit | 52874f5 | **6.0** | < 9: 3× major (jti TTL, граница AS-11, built_at из будущего) + DoD-4 → цикл 2 | 2026-11-20 |
| M1 | 2 | исполнитель | claude/m1-connector-kit | — | — | запущен (session_01V6d5A8zZAMeDeTHEvThebc) | 2026-11-20 |
| M4 | 1 | исполнитель | claude/m4-contextforge-spike | 0130d78 (+9df9cde отчёт) | — | живой стенд в облаке: вариант A провалил 1–3, C прошёл 1–5; ADR Accepted (C), предварительно GO; digest закреплён | 2026-11-20 |
| M4 | 1 | критик | claude/m4-contextforge-spike | 0130d78 | — | запущен (session_01V6d5A7XEpKRJstUuqRhWW5) | 2026-11-20 |
| — | — | процесс | — | — | — | Сессия оркестратора повышена (доверенные устройства): дочерние треды больше не могут слать send_message → оркестратор опрашивает треды каждые 20 мин | 2026-11-20 |
| M2 | 2 | исполнитель | claude/m2-policy-tooling | e10cf06/fbd522a (+59d7f61 отчёт) | — | F1–F5, GR-13a сделаны; AC-M2-04 ждёт 7 шагов владельца на реальном PR | 2026-11-20 |
| M2 | 2 | критик | claude/m2-policy-tooling | fbd522a | — | запущен | 2026-11-20 |
| M4 | 1 | критик | claude/m4-contextforge-spike | 0130d78 (отчёт 77d04f6) | **8.0** | < 9: 3× major (fail closed плагина не работает, JWS не привязан к tool + jti per-process, пробел мутаций aud) → цикл 2 | 2026-11-20 |
| M4 | 2 | исполнитель | claude/m4-contextforge-spike | — | — | запущен (session_01V6d5A5ydmWfpBpGhdwffAc) | 2026-11-20 |
| — | — | процесс | — | — | — | Ponytail обязателен для любой оценки; критик без ponytail-review → отчёт не принимается | 2026-11-20 |
| A | — | ponytail-audit | claude/m0-foundation | 6b3fe48 | — | запуск аудита этапа A (пропущен при закрытии M0) | 2026-11-20 |
| — | — | решение владельца | — | — | — | gitleaks вместо своего сканера, mutmut вместо самописных мутаций; критики явно проверяют «есть ли готовое решение» (поле existing_solutions); добавлена веха M0.1 | 2026-11-20 |
| M0.1 | 1 | исполнитель | claude/m0.1-standard-tools | — | — | запущен (session_01V6d5A9JGmeLGTTTU3Ebz2D): gitleaks + mutmut; M1/M4/критику M2 отправлены новые правила | 2026-11-20 |
| A | — | ponytail-audit | claude/audit-stage-a | fdae40b | — | Must fix 1–5 и Should fix 6–16, Nice 17 закрыты оркестратором в спеках/схемах (T12a, AS-26 tools из bundle, AS-11a on_behalf_of, MCC-09, схемы bundle + вектор подписи, uuid, rule_ids, public_keys, preview в manifest, JCS UTF-16); остаток → backlog | 2026-11-20 |
| M1 | 2 | исполнитель | claude/m1-connector-kit | c64693a (+5379d42) | — | пункты критика 1 сделаны; мутации — свой скрипт (mutmut не применён) | 2026-11-20 |
| M1 | 2 | критик | claude/m1-connector-kit | c64693a | — | запущен | 2026-11-20 |
| M4 | 2 | исполнитель | claude/m4-contextforge-spike | 98f7ce1/d2b62b0 (+e90d7ea) | — | 8 пунктов критика; 10/10 мутантов (свой скрипт); вердикт «NO-GO-триггеров не найдено» | 2026-11-20 |
| M4 | 2 | критик | claude/m4-contextforge-spike | d2b62b0 | — | запущен | 2026-11-20 |
| — | — | интеграция | claude/integration | e7d245f | — | база волны 3: m0-foundation + спеки оркестратора (аудит A, Must fix 5); check-all зелёный | 2026-11-20 |
| M2 | 2 | критик | claude/m2-policy-tooling | fbd522a | **5.0** | < 9: 2× critical (смена base PR; fast-forward A→B после метки), major (тег origin/main, DoD-4 mutmut) → цикл 3 (+ tools из manifest в bundle, вектор подписи) | 2026-11-20 |
| M2 | 3 | исполнитель | claude/m2-policy-tooling | — | — | запущен | 2026-11-20 |
| M1 | 2 | критик | claude/m1-connector-kit | c64693a | **6.0** | < 9: DoD-4 (самописный скрипт вместо mutmut, 20 выживших), 2× major (thumbprint, jti) → цикл 3 (+ спеки аудита); без роста 1 цикл | 2026-11-20 |
| M1 | 3 | исполнитель | claude/m1-connector-kit | — | — | запущен | 2026-11-20 |
| M4 | 2 | критик | claude/m4-contextforge-spike | d2b62b0 | **6.0** | < 9: DoD-4 (свой скрипт мутаций, 27 неэкв. выживших), major (кривой заголовок → crash; свой JWS вместо PyJWT, DoD-6) → цикл 3 в очереди (лимит 3 исполнителя) | 2026-11-20 |
| M1 | 3 | исполнитель | claude/m1-connector-kit | a0c0b4a (+0eb244b) | — | mutmut, выжившие убиты, back-off JWKS, приведение к спекам аудита | 2026-11-20 |
| M1 | 3 | критик | claude/m1-connector-kit | a0c0b4a | — | запущен | 2026-11-20 |
| M4 | 3 | исполнитель | claude/m4-contextforge-spike | — | — | запущен (слот освободился) | 2026-11-20 |
| M0.1 | 1 | исполнитель | claude/m0.1-standard-tools | 01ca42e (+f7f88f7) | — | gitleaks + mutmut, AC-M0.1-01…04 passed по отчёту, CI 44815259928 | 2026-11-20 |
| M0.1 | 1 | критик | claude/m0.1-standard-tools | 01ca42e | — | запущен | 2026-11-20 |
| M1 | 3 | критик | claude/m1-connector-kit | a0c0b4a | **9.0** | ≥ 9 — **веха закрыта**; 4 minor → backlog | 2026-11-20 |
| M0.1 | 1 | критик | claude/m0.1-standard-tools | 01ca42e | **5.0** | < 9: AC-M0-04 (пустой .env проходит), .envrc/.env-local, свой network guard вместо pytest-socket → цикл 2; вопрос владельцу: допустима ли однострочная проверка имён файлов (AC-M0.1-02) | 2026-11-20 |
| M0.1 | 2 | исполнитель | claude/m0.1-standard-tools | — | — | запущен (session_01V6d5A7pfKjuUdxfWCvZmq7) | 2026-11-20 |
| M0.1 | 2 | решение владельца | — | — | — | AC-M0.1-02: разрешена однострочная проверка запрещённых имён файлов | 2026-11-21 |
| M0.1 | 2 | исполнитель | claude/m0.1-standard-tools | f78e6b3…7e2a984 | — | пустые файлы, .env*, pytest-socket; owner_decision записан | 2026-11-21 |
| M2 | 3 | исполнитель | claude/m2-policy-tooling | e1dae73 (+285a99e) | — | base/FF/тег закрыты, mutmut, tools из manifest, вектор подписи | 2026-11-21 |
| M4 | 3 | исполнитель | claude/m4-contextforge-spike | 27c676a (+140dbd3) | — | PyJWT, pytest+mutmut, кривой заголовок → отказ, депрекация headers в ADR | 2026-11-21 |
| M0.1, M2, M4 | — | критики | — | — | — | запущены (M0.1 ц2, M2 ц3, M4 ц3) | 2026-11-21 |
| M0.1 | 2 | критик | claude/m0.1-standard-tools | 7e2a984 | **8.0** | < 9: major (merge-коммит с пустым .env не покрыт тестом), grep fail-open → цикл 3 | 2026-11-21 |
| M2 | 3 | критик | claude/m2-policy-tooling | e1dae73 | **8.0** | < 9: major (gate исполняется из PR; manifest не в diff) → цикл 4 (последний по лимиту); остаточный риск ci.yml → решение владельца | 2026-11-21 |
| M2 | 4 | решение владельца | — | — | — | Остаточный риск: PR, правящий .github/workflows/, может отключить gate — риск принят, владелец смотрит такие PR сам (CODEOWNERS) | 2026-11-21 |
| M4 | 3 | критик | claude/m4-contextforge-spike | 27c676a | **9.5** | ≥ 9 — **веха закрыта**: GO ContextForge v1.1.1 + вариант C (PyJWT); minor → backlog | 2026-11-21 |
| — | — | интеграция | claude/integration | 98287f6 | — | влиты закрытые M1 и M4 + спеки оркестратора; check-all зелёный; база для M3/M5/M7 | 2026-11-21 |
| M0.1 | 3 | исполнитель | claude/m0.1-standard-tools | 91e6e09 (+9dd74c1) | — | merge-коммит, grep fail closed, без opt-out сети; CI 44887393111 | 2026-11-21 |
| M2 | 4 | исполнитель | claude/m2-policy-tooling | dd0beb1 (+40c6595, 8f6754e) | — | gate из main, manifest = widening, duplicate-tool lint, mutmut | 2026-11-21 |
| M0.1, M2 | — | критики | — | — | — | запущены (M0.1 ц3, M2 ц4) | 2026-11-21 |
| M0.1 | 3 | критик | claude/m0.1-standard-tools | 91e6e09 | **8.0** | < 9 (без роста 1 цикл): major — сетевой opt-out через фикстуру, политика не под тестом → цикл 4 | 2026-11-21 |
| M2 | 4 | критик | claude/m2-policy-tooling | dd0beb1 | **8.0** | < 9 (без роста 1 цикл): дубли tool внутри manifest; повышение effect_class при матче по классу → цикл 5 (последний) | 2026-11-21 |
| M0.1 | 4 | исполнитель | claude/m0.1-standard-tools | e52a20d (+3b7648e) | — | opt-out сети → красный прогон, хук удалён; критик запущен (с метками origin) | 2026-11-21 |
| — | — | решение владельца | — | — | — | 4 правила против холостых циклов: origin находок, исчерпывающий критик + модель угроз, правила не меняются в идущем цикле, предел для вех с противником | 2026-11-21 |
| M0.1 | 4 | критик | claude/m0.1-standard-tools | e52a20d | **9.0** | ≥ 9 — **веха закрыта**; 3 minor (2 missed_earlier, 1 new_regression) → backlog | 2026-11-21 |
| M2 | 5 | исполнитель | claude/m2-policy-tooling | 6ffcadd (+e169154) | — | дубли внутри manifest, любое изменение effect_class = widening; критик ц5 запущен (threat_model) | 2026-11-21 |
| M2 | 5 | критик | claude/m2-policy-tooling | 6ffcadd | **9.5** | ≥ 9 — **веха закрыта**; threat_model: открытых путей нет; AC-M2-04 ждёт ручной проверки владельца | 2026-11-21 |
| — | — | интеграция | claude/integration | — | — | тред слияния M0.1 + M2 запущен (конфликты CI/pyproject/uv.lock); после него — M3 и M7 | 2026-11-21 |
| M4.1 | — | решение владельца | — | — | — | новая веха: спайк ADR-0002 (ToolHive, Docker MCP Gateway vs ContextForge) до M5 | 2026-11-21 |
| M4.1 | 1 | исполнитель | claude/m4.1-gateway-comparison | — | — | запущен (спайк ADR-0002) | 2026-11-21 |
| — | — | решение владельца | — | — | — | Изоляция процессов Approval Service: AS-27 роли БД на процесс, AS-28 секреты на процесс, AS-29 отдельные deployable ui/notifier; AC-M7-13, AC-M9-12 | 2026-11-21 |
| — | — | интеграция | claude/integration | 6a75c62 | — | M0.1 + M2 влиты; все проверки зелёные; grants-widening красный вне PR по дизайну M2 (нужен мерж в main с меткой владельца) | 2026-11-21 |
| M3 | 1 | исполнитель | claude/m3-demo-read | — | — | запущен (волна 3) | 2026-11-21 |
| M7 | 1 | исполнитель | claude/m7-approval-core | — | — | запущен (волна 3, с AS-27…29) | 2026-11-21 |
| M4.1 | 1 | исполнитель | claude/m4.1-gateway-comparison | 1054d05 (+5ece6e4) | — | ADR-0002 Proposed: оставить ContextForge + C; ToolHive — запасной/гибрид; Docker MCP Gateway отклонить; критик запущен | 2026-11-21 |
| M4.1 | 1 | критик | claude/m4.1-gateway-comparison | 1054d05 | **8.0** | < 9: ToolHive недооценён (путь C через mutating webhook без форка), неполные advisories ToolHive → цикл 2 | 2026-11-21 |
| M4.1 | 2 | исполнитель | claude/m4.1-gateway-comparison | 28fba91 (+bcff838) | — | рекомендация не изменилась: ContextForge + C; ToolHive — проверенный запасной (путь C без форка, ~7–10 дн.); критик запущен | 2026-11-21 |
| M3 | 1 | исполнитель | claude/m3-demo-read | 4690309 | — | AC-M3-01…11 passed по отчёту; критик запущен | 2026-11-21 |
| M4.1 | 2 | критик | claude/m4.1-gateway-comparison | 28fba91 | **8.0** | < 9 (без роста 1 цикл): находки только по запасному ToolHive (docker socket, Bearer, jti per connector, webhook advisories) → узкий цикл 3 | 2026-11-21 |
| M3 | 1 | критик | claude/m3-demo-read | 4690309 | **8.0** | < 9: major — нет проверки bundle до handler; пустой набор досок всё равно идёт в backend → цикл 2 | 2026-11-21 |
| M4.1 | 3 | исполнитель | claude/m4.1-gateway-comparison | 185162e (+c2f505f) | — | рекомендация та же; ToolHive production-топология не проверена (K8s ~12–17 дн. / docker socket — отклонён); критик запущен | 2026-11-21 |
| M4.1 | 3 | критик | claude/m4.1-gateway-comparison | 185162e | **9.0** | ≥ 9 — **веха закрыта**; 7 minor → backlog; ADR-0002 Proposed → решение владельца | 2026-11-21 |
| M4.1 | — | решение владельца | — | — | — | ADR-0002 → Accepted: ContextForge v1.1.1 + вариант C; ToolHive — запасной при провале условия 4 в M5; Docker MCP Gateway отклонён | 2026-11-21 |
| M3 | 2 | исполнитель | claude/m3-demo-read | f54b9f1 (+fc8d17d) | — | GR-12 отказ без bundle, GR-16 пустой набор без backend, internal_error; mutmut 749/710 (+39 экв.); критик запущен | 2026-11-21 |
| M3 | 2 | критик | claude/m3-demo-read | f54b9f1 | **6.0** | < 9: DoD-4 (basicConfig мутанты ложно эквивалентны, аудит в stdout не проверен) new_regression → цикл 3 | 2026-11-21 |
| M3 | 3 | исполнитель | claude/m3-demo-read | 3bcd176 | — | код запушен (аудит-лог реального процесса, курсор на пустом наборе, internal_error); отчёт cycle 3 ожидается | 2026-11-21 |
| M7 | 1 | исполнитель | claude/m7-approval-core | 7c68ff8 (+b752d31) | — | AC-M7-01…13 passed по отчёту, mutmut; критик запущен | 2026-11-21 |
| M3 | 3 | исполнитель | claude/m3-demo-read | 3bcd176 (+08c1150) | — | AC-M3-01…11 passed, CI 44913137254; критик ц3 запущен | 2026-11-21 |
| — | — | решение владельца | — | — | — | Ускорение процесса: тред-владелец вехи + финальный независимый критик; вопросы не блокируют; закрытый чек-лист, missed_earlier ≤ major → backlog; порог ≥ 8 для M0.x/M4.x; самопроверка исполнителя; опрос раз в минуту. Со следующего цикла | 2026-11-21 |
| M3 | 3 | критик | claude/m3-demo-read | 3bcd176 | **9.5** | ≥ 9 — **веха закрыта**; 2 minor (логи) → backlog | 2026-11-21 |
| M5 | 1 | владелец вехи | claude/m5-readonly-poc | — | — | запущен по новому процессу (тред-владелец вехи + финальный независимый критик) | 2026-11-21 |
| M7 | 1 | критик | claude/m7-approval-core | 7c68ff8 | **6.0** | < 9: DoD-4 (8 SQL-мутантов выжили: tenant-фильтр и др.), major: path traversal intent_id в mcp, T1-счётчики без индексов, самописное сравнение OpenAPI без DoD-6 (все new_scope) → цикл 2 | 2026-11-21 |
| M7 | 2 | владелец вехи | claude/m7-approval-core | — | — | запущен по новому процессу (закрытый чек-лист из 6 пунктов, threat_model ц1) | 2026-11-21 |
| M5 | 1 | владелец вехи | claude/m5-readonly-poc | e810df0 | — | стенд через ContextForge (unified_pdp, вариант C, сетевая изоляция), тесты этапа B; 3 вопроса `pending_owner_decision` (MC-04, коды ошибок CF, TTL отзыва) — работа идёт | 2026-11-21 |
| M5 | 1 | критик-субагент | claude/m5-readonly-poc | c01c5c3 | **8.0** | < 9: 5 major (общий пароль агентов, bundle стенда истекает через 24 ч, SC-10 stale проверяет откат, SSE не под тестом, неизвестный kid не логируется) → цикл 2 в треде вехи | 2026-11-21 |
| M5 | 2 | критик-субагент | claude/m5-readonly-poc | d39a1ff | **9.5** | чек-лист ц1 выполнен; 3 minor → backlog; M5-READY.md; финальный независимый критик запущен | 2026-11-21 |
| M7 | 2 | критик-субагент | claude/m7-approval-core | c6bb52f | **9.0** | чек-лист ц1 выполнен; 2 major missed_earlier (тест индекса не ловит удаление; окна auto-лимита 1 h/1 day не под мутациями) и 4 minor → по правилу в backlog; ждём M7-READY | 2026-11-21 |
| M7 | 2 | владелец вехи | claude/m7-approval-core | a843c11 (+0fce3f3) | — | 2 major missed_earlier закрыты тестами; M7-READY.md; финальный независимый критик запущен | 2026-11-21 |
| M5 | final | независимый критик | claude/m5-readonly-poc | d39a1ff | **9.5** | ≥ 9 — **веха закрыта**: GO через ContextForge; 3 major + 3 minor missed_earlier → backlog | 2026-11-21 |
| M6 | 1 | владелец вехи | claude/m6-kb | — | — | запущен (нужен read-доступ к gxg_agents_v2) | 2026-11-21 |
| M11 | 1 | владелец вехи | claude/m11-telegram-read | — | — | запущен (нужен read-доступ к gxg_agents_v2; без живых аккаунтов) | 2026-11-21 |
| M7 | final | независимый критик | claude/m7-approval-core | a843c11 | **9.0** | ≥ 9 — **веха закрыта**; major: затёрты результаты M4 в спеках ветки (missed_earlier, исправлено в ветке оркестратора), идемпотентность между агентами tenant'а (new_scope, вне AC M7 → M8 + вопрос владельцу), окно execution_deadline не под тестом (missed_earlier → backlog) | 2026-11-21 |
| — | — | оркестратор | claude/admiring-planck-vzys0x | — | — | спеки ветки оркестратора выровнены с результатами M4/M4.1/M5 (ADR-0001 вариант C, SEC-02/03, TEN-01, ADR-0002, Roadmap GO) | 2026-11-21 |
| — | — | интеграция | claude/integration | — | — | запущен интегратор: M3+M4.1+M5 (claude/m5-readonly-poc) и M7 → integration; затем M8, M9 | 2026-11-21 |
| M11 | 1 | владелец вехи | claude/m11-telegram-read | 3d590e4 | — | коннектор на fake-порту, стенд, live-тесты через ContextForge; доступ к gxg_agents_v2 получен; 3 вопроса `pending_owner_decision` | 2026-11-21 |
| — | — | интеграция | claude/integration | fc87e27 (+3c2f0b0) | — | M3+M4.1+M5+M7 влиты; 868 + 307 (PG) passed, стенд M5 29/29 ×3, gitleaks чисто; спеки = ветка оркестратора; mutmut идёт (~2 ч) | 2026-11-21 |
| M8 | 1 | владелец вехи | claude/m8-executor | — | — | запущен от integration (+ идемпотентность по requester, окно execution_deadline) | 2026-11-21 |
| M9 | 1 | владелец вехи | claude/m9-ui-bot | — | — | запущен от integration (+ компрометация ui через gxg_decide — по умолчанию AS-27) | 2026-11-21 |
| M6 | 1 | критик-субагент | claude/m6-kb | 6b75b6b | **5.0** | AC-M6-01/03/04 не выполнены без доступа к gxg_agents_v2 (AC-M6-02 только на fake); 4 major (схемы выхода, bundle_version в аудите, server.py вне mutmut, синхронный порт без лимита) → цикл 2; без доступа — стоп после цикла 2 | 2026-11-21 |
| M11 | 1 | владелец вехи | claude/m11-telegram-read | 2cebc81 | — | conformance: AC-M11-01…05 + backlog M5 (2),(3) через ContextForge, 3 × 46 passed; ждёт критика-субагента | 2026-11-21 |
| M8 | 1 | владелец вехи | claude/m8-executor | e1a5c64 | — | executor, demo propose/execute/reconcile, fake provider, ротация JWKS, EX-01 на стенде | 2026-11-21 |
| M9 | 1 | владелец вехи | claude/m9-ui-bot | f0d0308 | — | ui/notifier, E2E Playwright; 5 вопросов `pending_owner_decision` (работа идёт) | 2026-11-21 |
| — | — | решение владельца | — | — | — | Сеть и доступ через Tailscale: в интернет ничего не открыто, ContextForge/Web UI/admin только из tailnet, ACL в Git с тестами, Funnel запрещён (ADR-0003, SEC-13, SEC-25…27, OPS-06/07); вводится в M14 | 2026-11-21 |
| — | — | решения (поручение владельца) | — | — | — | ADR-0004 Telegram-коннектор как отдельный сервис; EFF-08 по requester; AS-30 decider; SEC-20 ≤ 5 с; MCC-05; DoD-1 локальный CI; AC-M14-04/08/09 (Tailscale); AC-M10-08 | 2026-11-21 |
| M11 | 1 | критик-субагент | claude/m11-telegram-read | 2cebc81 | **5.0** | AC не выполнены: нет адаптера к реальному сервису (только fake), порт не совпадает с реальным, тест «сессий не прибавилось» тавтологичен, M5.json перезаписан, не хватает SC-06/MC-01 для telegram; спеки затёрты устаревшими (взяты до 717dabd) → цикл 2 по ADR-0004 (архив, роль SELECT) | 2026-11-21 |
| M6 | 2 | владелец вехи | claude/m6-kb | 697fdac | — | 4 major ц1 исправлены (async порт с лимитом, схемы выхода, bundle_version, server.py в mutmut); доступ к gxg_agents_v2 — повторная попытка | 2026-11-21 |
| M9 | 1 | критик-субагент | claude/m9-ui-bot | 6453cf0 | **6.0** | critical: httpx пишет URL с токеном бота в лог (воспроизведено на образе notifier); major: 2 «эквивалентных» мутанта не эквивалентны (rstrip), main()/cli.py вне тестов и mutmut, нет ключа preview одного tenant'а → падает весь список → цикл 2 в треде | 2026-11-21 |
| M11 | 2 | владелец вехи | claude/m11-telegram-read | 2ec1b44 | — | коннектор на архиве (ADR-0004), стенд с базой архива, SC-01/06/08/MC-01 для telegram; вопросы #4/#5 приняты оркестратором | 2026-11-21 |
| M6 | 2 | владелец вехи | claude/m6-kb | 80e3f20 | — | самопроверка ц2 (стенд M6 3×35, M5 3×29, mutmut OK); доступа к gxg_agents_v2 нет → отдельный тред gap-аудита KB (claude/m6-kb-audit) | 2026-11-21 |
| M6 | — | аудит KB | claude/m6-kb-audit | 04bb0b3 | — | add_repo gxg_agents_v2 снова отклонён auto-mode классификатором; нужен шаг владельца (правило allow или режим с подтверждением) | 2026-11-21 |
| M8 | 1 | владелец вехи | claude/m8-executor | 481ef5f | — | отчёт conformance цикла 1; ждёт критика-субагента | 2026-11-21 |
| M6 | 2 | критик-субагент | claude/m6-kb | 80e3f20 | **5.0** | всё без доступа закрыто, critical/major нет; потолок 5 из-за AC-M6-01/03/04 (нужен gxg_agents_v2) → ждём аудит KB (сессия ждёт Approve «Register Repo Root») | 2026-11-22 |
| M8 | 1 | критик-субагент | claude/m8-executor | 481ef5f | **7.0** | 3 major: схема выхода propose не допускает статусы после повтора, reserve без ключа подписи сжигает попытку, ротация ключа ломает single-file Docker secret → цикл 2 (bc98b3f: исправлено + EFF-08 миграция 0003) | 2026-11-22 |
| M9 | 2 | критик-субагент | claude/m9-ui-bot | 4f33c0e | **9.0** | ≥ 9; minor → backlog; ждём M9-READY.md, затем финальный критик | 2026-11-22 |
| M9 | final | независимый критик | claude/m9-ui-bot | 244000b | — | запущен (не дожидаясь M9-READY.md: оценка ц2 9.0 уже есть) | 2026-11-22 |
| M9 | final | независимый критик | claude/m9-ui-bot | 40bc8a5 | **9.0** | ≥ 9 — **веха закрыта**; 7 minor → backlog (access log с OIDC code, очистка outbox — в M10); threat_model open закрывается AS-30 в M10 | 2026-11-22 |
| M8 | 2 | критик (отдельная сессия) | claude/m8-executor | d487b4a | — | запущен оркестратором: тред-владелец 3,5 ч в check-all/полном mutmut и не видит сообщений; mutmut критика — только модули M8 | 2026-11-22 |
| M11 | 2 | владелец вехи | claude/m11-telegram-read | 11f61a8 | — | conformance ц2: AC-M11-01…05 на локальном архиве через ContextForge (3 × 50), ci_local, mutmut exit 0; ждёт критика-субагента | 2026-11-22 |
| M9 | — | владелец вехи | claude/m9-ui-bot | — | — | после закрытия: локальный check-all rc=1 (3 мутанта зависли, в backlog) — проверить при интеграции | 2026-11-22 |
| M8 | 2 | критик (отдельная сессия) | claude/m8-executor | d487b4a | **6.0** | critical: после EFF-08 по requester ключ идемпотентности провайдера — голый idempotency_key, два requester'а схлопываются в один эффект (следствие решения оркестратора) → спека: ключ провайдера = intent_id (EFF-18 п.5, EFF-23/25, demo, AC-M8-01/02/08); major: exec-keys не в .gitignore, нет доказательств ц2, строки SQL-драйвера выживают, gitleaks на базовой истории → цикл 3 | 2026-11-22 |
| M11 | 2 | критик-субагент | claude/m11-telegram-read | 11f61a8 | **8.0** | AC выполнены, чек-лист ц1 закрыт; 2 major: порядок пагинации поиска не полный (дубли/пропуски), нет уведомления об изменении схемы архива (ADR-0004) → цикл 3 в треде | 2026-11-22 |
| M8 | 3 | владелец вехи | claude/m8-executor | 45017be | — | цикл 3 начат (ключ провайдера = intent_id, чек-лист ц2) | 2026-11-22 |
| M8, M11 | 3 | критики (отдельные сессии) | claude/m8-executor, claude/m11-telegram-read | ddf8c9f, 4ebe3ec | — | треды-владельцы встали посреди цикла 3 → оркестратор запустил критиков цикла 3; тред M11 переключён на gap-аудит KB для M6 (клон gxg_agents_v2 у него есть) | 2026-11-22 |
| M8 | 3 | критик (отдельная сессия) | claude/m8-executor | ddf8c9f | **6.0** | critical ц2 закрыт (ключ провайдера = intent_id), AC-M8-01…14 passed; DoD: устаревший id эквивалентного мутанта (mutmut.sh красный), доказательства ц3 не закоммичены → цикл 4 (формальный) | 2026-11-22 |
| M11 | 3 | критик (отдельная сессия) | claude/m11-telegram-read | 4ebe3ec | **8.0** | 2 major: проверка дрейфа схемы ложно «чисто» под pipefail (уже исправлено da3e25f), тест flood-wait зависит от времени; + перегенерировать M11.json по полному check-all → цикл 4 в треде после аудита KB | 2026-11-22 |
| M11 | 4 | независимый критик (отдельная сессия) | claude/m11-telegram-read | a484ec2 | **9.0** | ≥ 9 — **веха закрыта** (5.0 → 8.0 → 8.0 → 9.0); minor → backlog | 2026-11-22 |
| M6 | — | аудит KB (тред M11) | claude/m11-telegram-read | — | — | gap-аудит готов: кода kb_access и схемы kb.* в gxg_agents_v2 нет (G1/G2 blocker); ключи общие на роль (G3/G4 critical); M6 → цикл 3 по реальному контракту, AC-M6-04 ждёт репозиторий kb_access | 2026-11-22 |
| — | — | решение владельца | — | — | — | DoD-1: для закрытия вехи mutmut в check-all — только по пакетам вехи; полный — при интеграции и перед main | 2026-11-22 |
| M6 | 3 | критик-субагент | claude/m6-kb | 93a049f | **5.0** | потолок из-за AC-M6-04 (нужен репозиторий kb_access); 2 major (таймаут readiness, kb_get → not_found) — исправлены в ц4 (ef87e9a…34ceb14, check-all OK) | 2026-11-22 |
| — | — | решение владельца | — | — | — | Ускорение процесса, 3-я редакция: закрытие по гейту при только формальных замечаниях, blocked_on_owner, таймауты в DoD-1, heartbeat 30/60 мин, проверка решений по спекам субагентом, CI в «Ждёт владельца» | 2026-11-22 |
| M6 | — | оркестратор | claude/m6-kb | 34ceb14 | 5.0 | статус **blocked_on_owner** (только AC-M6-04, нужен kb_access); циклы остановлены | 2026-11-22 |
| — | — | решение владельца | — | — | — | Формат планов и статусов владельцу: «Что \| Зачем \| Почему», шаг без «Почему» убирается; для оркестратора и тредов-владельцев | 2026-11-22 |
| — | — | решение владельца | — | — | — | CI без GitHub Actions: закрытие вех — локальный check-all; мерж в main — локальный гейт мержа с отчётом reports/merge-gate/<sha>.md | 2026-11-22 |
| M11.1 | — | решение владельца | dmitry-v-ko/gxg-telegram-service | — | — | новая веха: отдельный Telegram-сервис (чтение + отправка), перенос кода с обновлением фреймворка, обёртка по контрактам GxG-hub; ADR-0005 (MCP или HTTP) заменяет ADR-0004; ждём название фреймворка от треда M11 | 2026-11-22 |
