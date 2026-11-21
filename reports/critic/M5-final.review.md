# M5 — финальный независимый критик: ponytail-review

Ревью навыком `.claude/skills/ponytail-review/SKILL.md`. Дифф `6a75c62..d39a1ff`, из него смотрю только код и стенд M5: `635caae..d39a1ff` и `c54346b` (правка спек). Влитые закрытые M3 и M4.1 не смотрю. Код — `9add475`, сверху только отчёты. Отчёты исполнителя и критика цикла 2 перепроверены, а не взяты на веру.

Нагрузка, которую я предполагал: dev-стенд на одной машине, одна реплика `demo`, одна ContextForge, 4 синтетических агента, сотни вызовов в минуту от тестов. Для production это PoC (`shortcut:` помечены).

**What this change does:** Поднимает dev-стенд: ContextForge v1.1.1 по digest, PostgreSQL и коннектор `demo` во внутренней сети, куда есть доступ только у ContextForge. Свой плагин ContextForge `gxg_identity` подписывает каждый вызов tool'а заголовком `X-GxG-Identity` (JWS Ed25519: кто, какой tenant, какой tool, хеш аргументов, срок 60 с, одноразовый `jti`). `demo` проверяет его через `ContextForgeVerifier` из кита. Встроенный `unified_pdp` запрещает всё, что не разрешено явно. Тесты этапа B (SC-01…SC-10, MC-01…MC-04) гоняют этот стенд через ContextForge, потом после рестарта и после пересоздания всех контейнеров.

## Что я перезапустил сам

| Проверка | Результат |
| --- | --- |
| `dockerd` вручную, свежий стенд с нуля (нет `.env`, `stand/secrets`, томов), `KEEP=1 DOCKER_BUILD_FLAGS="--network host --secret id=ca-bundle,…" stand/run.sh` | exit 0: `fresh` 29/0, `after-restart` 29/0, `after-recreate` 29/0. SC-01…SC-10 и MC-01…MC-04 есть в каждом прогоне. Сгенерированный `M5.json` откатил, закоммиченный не трогал |
| AC-M5-04: рестарт и пересоздание | `docker compose restart`, затем `down` + `up` без `-v` и без bootstrap: 29/29 оба раза |
| AC-M5-03: изоляция `demo` | `curl http://10.250.0.10:8080/healthz` с хоста Docker: exit 7 (нет соединения). Сеть `connectors`: `internal: true`, `inhibit_ipv4: true`; у `demo` нет опубликованных портов. Тест SC-06 из сети `edge` и `--network host`: ошибка соединения; изнутри `connectors` без identity — `unauthenticated` |
| AC-M5-02: чужой tool по имени | `test_sc01_other_team_tool_is_refused_by_name_on_every_path` ×3: отказ на `/rpc`, `/mcp`, `/sse`, своём и чужом virtual server, в аудите `demo` ноль вызовов |
| Fail closed: плагины выключены (`PLUGINS_ENABLED=false`, compose override) | 4 вызова (reader и nobody, `/rpc` и virtual server) → `demo` отвечает `unauthenticated`, причина в аудите `missing`, backend +0 |
| Fail closed: плагин не загрузился (ключ адаптера — не PEM) | ContextForge в цикле рестартов, `Plugin initialization failed: GxgIdentity`, порт 4444 не отвечает |
| Fail closed: плагин падает во время вызова (ключ RSA: загружается, подпись EdDSA падает) | вызов блокирует ContextForge (`Plugin Error: InvalidKeyError…`), до `demo` не доходит, аудит пуст |
| Подделанная подпись, неизвестный `kid`, истёкшая, повтор | SC-04 ×3, 13 случаев с причинами (`signature`, `kid`, `exp`, `replay`, …), backend +1 только для контроля |
| Повтор **настоящей** assertion ContextForge | перехватил `X-GxG-Identity` снифером в netns `demo` (AF_PACKET), отправил его же напрямую в `demo` из сети `connectors`: `unauthenticated`, причина `replay`; тот же JWS на другой tool — тоже отказ; backend +0 |
| Token passthrough (SEC-05) | снифер в netns `demo` на 6 вызовах (оба tenant'а, `/rpc`, virtual server, `/sse`): заголовка `Authorization`, слова `Bearer` и токенов агентов в трафике к `demo` нет, только `X-GxG-Identity` |
| Агент меняет конфигурацию своей team | токеном `t-alpha:nobody`: `PUT /v1/tools/{id}` (переименовать), `PUT /v1/gateways/{id}`, `POST /v1/gateways`, `/v1/tools`, `/v1/servers`, `/v1/plugins` → 403; `POST /v1/tokens` → 403 (API-токеном нельзя выпускать токены). Роль `member` = `viewer` ContextForge |
| Откуда плагин берёт `server_id` и `tenant_id` | в образе `tool_service.py:5704,5713`: оба из записи tool'а (gateway и team), не из запроса |
| mutmut | `scripts/ci/mutmut.sh` exit 0: gxg-policy 2555/2590, demo 832/841, gateway/contextforge 132/133, kit 1066/1077, root 1211/1235; не убито 80, все 80 в `mutmut-equivalent.txt` с причиной, лишних в списке нет. Новые для M5: `gxg_identity.core.x_args_hash__mutmut_5` и `cf_identity.x_args_hash__mutmut_5` (`ensure_ascii=None`): `json.dumps` проверяет только истинность — эквивалентны. Перенесённые в цикл M1-мутанты `_jwt` (`and` вместо `or` у `kid`, переименованные опции `verify_nbf`/`verify_iss`) перепроверил по коду PyJWT: эквивалентны для наших токенов |
| Остальные job'ы CI локально (CI на GitHub для `d39a1ff` не подтверждён: раннеров не было) | `uv lock --check` ×2, `ruff check`/`format`, 4 прогона mypy (включая `stand` и `gateway/contextforge`), root pytest 798, kit 360 + покрытие, demo 64, `prove_schema_guard`, gitleaks tree и range `6a75c62..HEAD`, `pytest tests/secrets` 21, `gxg-policy validate`/`lint`, `bundle_smoke`, `docker_smoke` — зелёные. Оговорка: в shallow-клоне `test_this_repository_is_clean` падает на 5 «находках» в граничных коммитах (так же на базе `6a75c62`); после `git fetch --unshallow` — зелёный. Это артефакт клона, не код |

## Находки

Перенос в рубрику (`M5-final.json`): 1–3 → `major`, `origin: missed_earlier` (было до цикла 2, критики не нашли) — по пункту 3 «Ускорения процесса» идут в backlog и оценку не снижают; 4–6 → `minor`. `critical` нет, `new_regression`/`new_scope` уровня major нет. Итог: **9.5**, веха закрывается.

### Should fix

1. **Fail closed на стороне ContextForge не проверяется автотестом** (`stand/tests/`, `compose.yaml:53-60`)
   - **What this is:** Если плагин identity выключен, не загрузился или падает, вызов не должен дойти до данных. ADR-0002, Roadmap и threat model критика цикла 2 называют это «closed» по конфигу (`PLUGINS_FAIL_ON_PLUGIN_ERROR=true`).
   - **Problem:** Ни один тест не запускает ContextForge с выключенным или сломанным плагином. Я проверил руками: все три случая закрыты. Но если кто-то уберёт флаг из `compose.yaml` или обновит ContextForge (MC-05), набор тестов останется зелёным.
   - **Fix:** Один тест стенда: override с `PLUGINS_ENABLED=false` → вызов агента даёт `unauthenticated` с причиной `missing`, backend +0. И проверка, что с битым ключом `/health` не отвечает. Короткий compose override, как сделал я.
   - **If we skip it:** Регрессия конфига останется незамеченной. Данные защищает только проверка `demo`, но она есть и протестирована (SC-06).

2. **Нет автотеста, что токен агента не уходит к коннектору (SEC-05)** (`stand/tests/test_sc.py:460-489`)
   - **What this is:** SC-09 ищет токены агентов в логах `demo`.
   - **Problem:** `demo` вообще не пишет заголовки в лог. Поэтому если ContextForge начнёт пересылать `Authorization` (например, `ENABLE_HEADER_PASSTHROUGH=true` или новая версия), тест не упадёт. Снифер показал, что сейчас заголовка нет.
   - **Fix:** В тестовом контроле `demo` (уже есть `/_test/backend/calls`) отдавать имена заголовков последнего `tools/call`. Тест проверяет, что `authorization` среди них нет.
   - **If we skip it:** Token passthrough можно включить незаметно.

3. **Запрет агенту менять tools, gateways и токены не в автотесте** (`stand/tests/test_sc.py:308-330`)
   - **What this is:** Правила `unified_pdp` привязаны к имени tool'а. Если агент сможет переименовать tool своей team или зарегистрировать gateway (SC-07 «регистрация только оператором», SEC-15), он обойдёт правило.
   - **Problem:** SC-07 проверяет регистрацию только от admin. 403 для агента я проверил руками (выше). Тест на это есть только как проба критика цикла 1.
   - **Fix:** В SC-07 добавить 4 запроса токеном агента (`PUT /v1/tools/{id}`, `POST /v1/gateways`, `POST /v1/tools`, `POST /v1/tokens`) → 403.
   - **If we skip it:** Смена роли `member` по умолчанию (`default_team_member_role`) в новой версии ContextForge даст обход PDP без падения тестов. Данные всё равно режут grants коннектора.

### Nice to have

4. **Текст исключения плагина уходит агенту** (`gateway/contextforge/gxg_identity/plugin.py:34-62`)
   - **What this is:** `tool_pre_invoke` ловит только `Denied`.
   - **Problem:** Любое другое исключение (я вызвал его ключом RSA) ContextForge возвращает агенту как `Plugin Error: InvalidKeyError('… RSAPrivateKey …')`. Это не секрет, но внутренняя деталь адаптера.
   - **Fix:** `except Exception` после `Denied` → тот же `PluginViolation` с общим текстом, подробность — в лог.
   - **If we skip it:** Агент видит тип ключа и класс ошибки. Fail closed не нарушен.

5. **Проверка `context_tenant != tool_team` на v1.1.1 не может сработать** (`gateway/contextforge/gxg_identity/core.py:76`)
   - **What this is:** Плагин сравнивает `gc.tenant_id` с `team_id` записи tool'а.
   - **Problem:** ContextForge v1.1.1 сам берёт `tenant_id` из той же записи (`tool_service.py:5704`). Сообщение «context tenant disagrees with the tool record» обещает защиту, которой нет. Настоящая проверка — `teams == [tool_team]` строкой ниже, и она протестирована.
   - **Fix:** Оставить как защиту на будущее, но сказать это в комментарии (одна строка). Или убрать параметр (−3 строки).
   - **If we skip it:** Читатель решит, что tenant проверяется дважды.

6. **Runbook «Рестарт хоста» не говорит про bundle старше 24 ч** (`stand/README.md:54`)
   - **What this is:** Строка обещает «`docker compose up -d`… повторного bootstrap не нужно (AC-M5-04)».
   - **Problem:** Если стенд простоял больше суток, `demo` после `up` не готов (GR-13), пока не запустить `stand/init.py`. Это правильный fail closed, но runbook ведёт не туда. Та же тема, что F13 цикла 2 (`один раз` в строке 32).
   - **Fix:** Дописать «если прошло больше 24 ч — сначала `uv run python stand/init.py`».
   - **If we skip it:** Оператор увидит 503 и пойдёт искать поломку.

## Есть ли готовое решение (ступени 2–4)

| Компонент | Готовая альтернатива | Вердикт |
| --- | --- | --- |
| Плагин `gxg_identity` (подпись JWS) | встроенное identity propagation ContextForge (HMAC только `user_id:email`, без tenant, срока и nonce, ADR-0001), плагины `header_injector`, `jwt_claims_extraction` из образа — не подписывают асимметрично и не привязывают к tool и аргументам | justified_custom (записано в плане и ADR-0001) |
| `ContextForgeVerifier` | PyJWT через `_jwt.verify` кита (уже есть); своё — только привязка `tool`/`ahs`/`sub`, которой нет в библиотеках | use_existing |
| `args_hash` в двух местах (`core.py`, `cf_identity.py`) | импорт из кита невозможен: плагин работает в образе ContextForge без кита; контрактный тест `test_adapter_and_kit_verifier_agree` держит их вместе | justified_custom |
| Правила tools | `unified_pdp` native из образа | use_existing |
| Сетевая изоляция | Docker `internal` network + `inhibit_ipv4` | use_existing |
| SSRF | встроенная SSRF-защита ContextForge (`SSRF_*`) | use_existing |
| `stand/bootstrap.py` | `/admin/import` и CLI ContextForge не создают пользователей и токены (записано в docstring) | justified_custom |
| `stand/init.py` (секреты, ключи, bundle) | `secrets` stdlib, `cryptography`, `gxg-policy build` | use_existing |
| `stand/report.py` (JUnit → M5.json) | `pytest --junitxml` + `xml.etree` stdlib; формат задаёт Conformance Matrix | justified_custom (≈90 строк) |
| `Stand.probe` (сырой HTTP из сети стенда) | `docker run` пинованного `python:3.12-slim` со stdlib: curl в образе нет | use_existing |
| `Stand._sse` | официальный `mcp.client.sse` | use_existing |
| Режимы отказа backend (`testctl`) | toxiproxy ломает транспорт, а коды `rate_limited`/`timeout_before_start` рождает коннектор; спека `demo` задаёт режимы fake provider | justified_custom |
| Таймаут tool'а (`asyncio.wait_for`) | FastMCP не применяет `Tool.timeout` к своему `run()` (комментарий в коде) | use_existing (stdlib) |

Самописных компонентов при наличии зрелого готового инструмента без обоснования нет.

## Вопросы владельцу #1–#4

Варианты по умолчанию нарушением MUST спеки не считаю. #1: у `demo` нет provider credentials, SEC-16 проверять не на чем, credentials цепочки этапа B проверены (MC-04). #2: MCC-05 — требование к коннектору, коннектор отвечает `invalid_input` сам (проверено напрямую в MC-02); `-32603` рождает upstream. #3: SEC-20 — kill switch эффектов, этап C (EX-13 ≤ 30 с); 5 с укладывается. #4: для read повтор безопасен, `timeout_before_start` запрещён без доказательства — выбран безопасный код.

Verdict: Ship.
Lean: -3 lines possible.
Not checked: CI на GitHub для `d39a1ff` (раннеров не было, прогон 44932350759; job'ы проверены локально); реальное ожидание 24 ч (проверено bundle'ом с `built_at` 25 ч назад); ContextForge с несколькими worker'ами под нагрузкой.
