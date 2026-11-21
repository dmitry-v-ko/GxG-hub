# M7 — ponytail-review финального независимого критика

Дифф: `6a75c62..a843c11` (ветка `claude/m7-approval-core`, голова с отчётами `0fce3f3`; `0fce3f3` по сравнению с `a843c11` меняет только `reports/` и `docs/backlog.md`).
Навык: `.claude/skills/ponytail-review/SKILL.md`. Нагрузка из репозитория: до ~10 tenant'ов, по одному экземпляру процесса (возможны несколько api/sweeper), сотни intents в час на tenant. Под эту нагрузку и оценивал масштаб.

## What this change does

Ветка добавляет ядро Approval Service:
- схему PostgreSQL со своей ролью БД на каждый процесс (AS-27);
- переходы T1–T18 и T12a: функции `SECURITY DEFINER`, которые сверяются с одной таблицей правил;
- процесс `api` (propose, чтение, отмена, dispatch) и движок политики AP-01…AP-04;
- квоты AP-05…AP-07 под одной advisory-блокировкой на tenant, `sweeper`, MCP-фасад `effects.status`/`effects.cancel`;
- шифрование payload sealed box'ами (у `api` только публичные ключи).

## Что проверено руками

- Чистые worktree на `a843c11`, PostgreSQL 16.15 через Unix-сокет (`initdb`, без TCP):
  - `approval/tests` + `approval/tests_db`: 372 passed;
  - корень: 743 passed;
  - `connectors/demo`: 1 passed; kit с покрытием 332 passed, `_jwt` 100 %;
  - ruff, ruff format, все прогоны mypy из `check-all.sh`;
  - `schema_lock`, `prove_schema_guard` (66/66), `gxg-policy validate`/`lint`, `bundle_smoke`;
  - gitleaks tree и полный range, `tests/secrets` — зелёные.
  - Примечание: на shallow-клоне range-скан gitleaks даёт ложные находки на коммитах границы клона. После `git fetch --unshallow` скан чист, CI использует `fetch-depth: 0`.
- Полный mutmut по `approval` и полный SQL-драйвер `scripts/ci/mutate_m7_sql.py` на `a843c11`. Результаты — в `M7-final.json` (`reproduction`).
- 15 своих SQL-мутантов вне списка драйвера и 12 своих атакующих тестов (вне репозитория):
  - кросс-requester идемпотентность;
  - часовая квота, лимит pending на tenant и дневной/часовой auto-лимит двух правил под 25–40 параллельными запросами;
  - kill switch, закоммиченный, пока T1 ждёт блокировку tenant'а;
  - лишний `effect_class` в теле;
  - requester, connection и коннектор чужого tenant'а.

## Must fix

Нет.

## Should fix

1. **Ветка молча откатывает спеки закрытой вехи M4** (`docs/specs/security-v1.md` SEC-02/SEC-03, `docs/specs/target-platform-v1.md` TEN-01, `docs/specs/resource-grants-v1.md`: пример `agent:` ID; коммит `dd82753`)
   - **What this is:** Первый коммит цикла 1 скопировал спеки из `claude/admiring-planck-vzys0x`, чтобы получить AS-27…AS-29 и AC-M7-13.
   - **Problem:** Вместе с ними вернулся старый текст с пометками `VERIFY`. Он затёр результат M4 (AC-M4-04, веха закрыта с 9.5): вариант C ADR-0001 (`gxg_identity`, `X-GxG-Identity`, claims, fail closed ContextForge) и проверенное TEN-01. В базе `6a75c62` и в текущем `origin/claude/integration` новый текст есть, в `a843c11` — нет: `grep -c X-GxG-Identity`: 2/1 против 0/0.
   - **Fix:** вернуть эти три файла к версии базы, оставив изменения AS-02/AS-27…AS-29: `git checkout 6a75c62 -- docs/specs/security-v1.md docs/specs/target-platform-v1.md docs/specs/resource-grants-v1.md`. Затем проверить `git diff 6a75c62 -- docs/specs` — там должны остаться только строки approval-service.
   - **If we skip it:** PR M7 → integration молча откатит нормативный SEC-03, на который опираются M5 и сам M7 (`cli.py`: «ADR-0001 variant C lands with M5»).

2. **Идемпотентность общая на весь tenant, а не на requester'а** (`approval/src/gxg_approval/intents.py:194-199`; EFF-08, AS-11a)
   - **What this is:** повтор `propose` с тем же `(tenant, idempotency_key)` и тем же хешем возвращает существующий intent.
   - **Problem:** хеш не включает requester'а. Проверено тестом:
     - агент `bot` повторяет ключ `signals:cand-1:v1` агента `signals` с теми же args и получает 200 с intent'ом `signals` (`intent_id`, статус, `rule_ids`, сроки);
     - `bot` первым занимает предсказуемый по спеке ключ Signals (`signals:<candidate>:v<n>`) с другими args, и настоящий `propose` Signals получает 409 навсегда;
     - если `bot` занял ключ с теми же args, Signals получает чужой intent, который `bot` может отменить. При этом `effects.status` Signals отвечает на этот intent `not_found`.
   - **Fix:** в коде — при `existing.requester_principal != requester` отвечать `conflict`, а не 200 (2 строки, AC-M7-04 для одного requester'а не меняется). Это закрывает утечку и подмену. Чтобы закрыть занятие чужого ключа, нужен `UNIQUE (tenant_id, requester_principal, idempotency_key)`. DDL обязателен по спеке, поэтому это решение владельца.
   - **If we skip it:** обманутый агент tenant'а может блокировать или подменять действия другого агента. Эффекта без согласования нет: человек видит настоящего requester'а.

3. **T2 берёт окно исполнения из политики, но тест этого не ловит** (`0001_core.py`, `gxg_decide`: `execution_deadline = now() + make_interval(secs => r.execution_window_s)`)
   - **What this is:** после набора кворума `execution_deadline` = now + окно правила (AP-04, `execution_window` в правиле можно только уменьшить).
   - **Problem:** мутант `make_interval(secs => r.execution_window_s)` → `interval '1 hour'` выживает во всех `approval/tests_db`. `test_t2_needs_the_quorum…` сравнивает дедлайн с `execution_window_s`, но там окно по умолчанию — 3600. Код сейчас верный.
   - **Fix:** в существующем тесте T2 предложить intent по правилу `require_approval` с `execution_window: 10m` и проверить дедлайн 600 с. Добавить мутант в драйвер.
   - **If we skip it:** регресс, при котором одобренный человеком intent живёт дольше, чем разрешает политика (fail open), пройдёт CI.

4. **Ещё три непротестированных побочных эффекта переходов** (`0001_core.py`)
   - **Problem:** выживают мутанты:
     - `gxg_cancel` без `_gxg_outbox('webhook', r)` — у T6 пропадает webhook;
     - в `gxg_decide` убрано `p_decision = 'approve' AND` — reject начинает требовать step-up, а T4 по таблице «как T2, кроме step-up»;
     - в `gxg_record_auto_decision` `rule_ids[1]` → последний элемент: EFF-03 «первый rule_id» нарушен при двух совпавших auto-правилах.
   - **Fix:** по одной проверке в существующих тестах: `test_cancel` — outbox `webhook`; `test_t2_refuses…` — reject без step-up проходит; `test_auto_approval_writes_policy_decision` — два правила, `decided_by` по первому. Добавить три мутанта в драйвер.
   - **If we skip it:** M10 (webhooks) и M9 (reject) получат тихие регрессии.

## Nice to have

5. **Движок политики написан сам, обоснование не записано** (`reports/plans/M7-cycle-1.md`, таблица DoD-6). Для `policy.py` там «minimal pure code» без альтернатив. OPA/Rego и Cedar проверены мной: формат правил задан спекой (YAML + `gxg-policy lint`). AP-01 требует слияния (max `approvals`, OR `step_up`, пересечение `approvers`, min сроков) и лимитов, посчитанных в транзакции T1. Cedar отвечает только allow/deny, OPA добавит рантайм ради ~60 строк. Значит, свой код оправдан, но строку в DoD-6 нужно дописать.
6. **`mcp` в compose ходит в `https://api:8090`, а `api` слушает plain HTTP** (`approval/deploy/compose.yaml`, `cli.py`). Процесс `mcp` до M5 всё равно не стартует (fail closed). Когда его включат, нужен TLS на `api` (AS-07) или URL по факту.

## Что не нашёл (проверено)

- Квоты и auto-лимиты под параллелизмом: одна `pg_advisory_xact_lock` на tenant до всех чтений. В READ COMMITTED каждый оператор видит строки, закоммиченные до получения блокировки. Мои параллельные тесты дают ровно лимит и для часа, и для суток, и для двух правил сразу. Проверял и с уже использованным дневным лимитом (часть окна в прошлом).
- Kill switch: T1 — внутри транзакции после блокировки. Переключатель, закоммиченный, пока T1 ждёт, даёт 423. T7/T9 — внутри функций, мутанты убиты.
- AS-11a: фильтр `tenant + principal` в `_SQL_OWN` и `gxg_cancel`; фасад ограничен своими tenant'ами; путь в `mcp` строится только из канонического UUID.
- AS-25/AS-26: tool, класс и `executes_via` берутся только из bundle. Лишнее поле `effect_class` в теле → 400. Requester, connection и коннектор чужого tenant'а → 403/400/403, строк 0.
- AS-27: матрица проверена каталогом и реальными SQL от каждой роли. Владелец — `NOSUPERUSER`. У всех точек входа `SECURITY DEFINER` и фиксированный `search_path`.
- AS-28: секрет на процесс только файлом, ни одного секрета в env.
- Шифрование: `api` только шифрует (публичные X25519), ключи args и preview разделены по ролям столбцов. В outbox и аудите нет payload.
- «Эквивалентные» мутанты SQL (4) и mutmut (11) проверены по коду. Все эквивалентны (обоснования — в JSON).

Verdict: Ship для оценки M7. До PR в integration исправить 1 (откат спек). 2 — вопрос владельцу, 3–4 — в backlog.
Lean: -0 lines possible (лишнего кода не нашёл; `_responses(ok=)` из цикла 2 — 3 строки, уже в backlog).
Not checked: CI на GitHub (раннеры не выдаются; CI повторён локально по `check-all.sh`, кроме самого `mutmut.sh` с Docker — его части запущены напрямую).
