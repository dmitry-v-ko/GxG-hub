# M4 cycle 3 — plan (ponytail full)

Вход: `reports/critic/M4-cycle-2.json` (6.0, капы DoD-4 и major) и `.review.md`; реплика владельца `claude/m4-live@b6918a5` (`echo/gxg_verify.py`, PyJWT). Задача — 6 `required_improvements`. Новых возможностей нет. Спеки и критерии не меняются; правки только в ADR и в уже разрешённых VERIFY-местах SEC-03.

## Что проверено до кода

- Образ `v1.1.1@sha256:16c7a898…` содержит PyJWT 2.15.1 и cryptography 50.0.2 (`/app/.venv/bin/python3`, Python 3.12).
- PyJWT 2.15.1 (`jwt/api_jwt.py:401`) берёт `now = datetime.now()` и не принимает «текущее время» параметром. Поэтому проверку времени нельзя оставить «по логу задним числом» и одновременно отдать её PyJWT. Решение: верификатор вызывается **в echo в момент приёма** (как на m4-live), echo запускается на том же закреплённом образе ContextForge — новых образов и зависимостей нет.
- `jwt.get_unverified_header` отклоняет заголовок не-объект (`DecodeError`) и `kid` не-строку (`InvalidTokenError`) — оба падения из находки 2 исчезают.
- `options={"strict_aud": True}`: `aud` только строка, список отклоняется.

## Scope

| # | Часть | Файлы | Ступень лестницы |
| --- | --- | --- | --- |
| 1 | Тесты и mutmut | `spikes/m4-contextforge/tests/test_verify_jws.py` (ключ Ed25519 генерируется в тесте, без сети), `setup.cfg` спайка (`[mutmut]`), `reports/mutmut/M4.txt`; удалить `mutate_verify_jws.py` и `evidence/variant-c.mutations.log`; путь тестов — в `testpaths` корня, PyJWT==2.15.1 (как в образе) — в dev-группу | установленные pytest + mutmut 3.8.1 (`uv run --with`, как M0.1) |
| 2 | Кривой заголовок → отказ | `verify_jws.py` + тесты (`W10.e30.AA`, `kid` списком, не base64, 2/4 сегмента, пусто) | PyJWT, `except jwt.PyJWTError` |
| 3 | PyJWT на обеих сторонах | `plugin.py`: `jwt.encode(..., algorithm="EdDSA", headers={"kid"})`; `echo/verify_jws.py`: `jwt.decode` (подпись, alg, iss, aud, exp/iat с leeway 5, обязательные claims). Свои: `tool`, `ahs`, TTL, `sub` в tenant, `jti` (+ выбор ключа по `kid`) | установленная зависимость |
| 3a | Проверка в момент приёма | `echo/echo_mcp.py` (если задан `ECHO_ADAPTER_PUBKEY`: вердикт в лог и в ответ tool), `compose.yaml` (echo на образе ContextForge), `compose.variant-c.yaml` (публичный ключ в echo) | существующий echo |
| 4 | `run_spike.sh` падает | `check_verdicts.py`: каждый вызов через ContextForge, дошедший до echo, принят с identity вызвавшего агента; ни один вызов не отклонён плагином; каждая атака отклонена с ожидаемой причиной (таблица в коде). Вызывается для C и апгрейда | stdlib; заменяет `verify_jws.py __main__` |
| 5 | Депрекация `ToolPreInvokePayload.headers` | ADR: критерий 5 и «Что дальше» | текст |
| 6 | Перепрогон | `run_spike.sh` → `evidence/`, `reports/conformance/M4.json` | — |
| — | Неванильные аргументы end-to-end | `spike.py calls`: аргументы `{"text": "привет", "lang": "ru"}` — плагин и верификатор считают `ahs` одинаково на не-ASCII и нескольких ключах | одна строка |

## DoD-6: рассмотренные готовые решения

| Компонент | Рассмотрено | Решение |
| --- | --- | --- |
| Плагин `gxg_identity` (сам хук) | Встроенные в v1.1.1: identity propagation (вариант A, отклонён на доказательствах: HMAC общий, нет `jti`/хеша аргументов); `jwt_claims_extraction` (только читает входящий токен); `header_injector` (статические заголовки, `resource_pre_fetch`); `vault` (хранимые токены провайдеров); RFC 8693 token exchange в `tool_service` (нужен внешний AS, токен кешируется на (gateway, user, audience) — нет per-call `jti` и `ahs`) | свой код оправдан: ни одно не даёт критерии 2 и 3 |
| Подпись JWS в плагине | PyJWT `jwt.encode` (в образе); joserfc, authlib (нет в образе) | PyJWT; свои `b64`/ручная подпись удалены |
| Верификатор | PyJWT 2.15.1 (в образе, на m4-live); joserfc (выбор кита M1, в образе нет); python-jose (не поддерживается) | PyJWT; свои только `tool`, `ahs`, TTL, `sub` в tenant, `jti` |
| Время проверки | PyJWT с реальным временем в момент приёма; freezegun/подмена `datetime` (нет, хак); своя проверка окна по времени из лога (запрещено пунктом 3) | проверка в echo при приёме |
| Канонический JSON для `ahs` | stdlib `json.dumps(sort_keys, separators, ensure_ascii=False)`; `rfc8785` (не установлен, расхождение для float записано в SEC-03) | stdlib |
| Мутационная проверка | mutmut 3.8.1 (DoD-4, M0.1); cosmic-ray, mutatest | mutmut; `mutate_verify_jws.py` удалён |
| Проверка вердиктов в `run_spike.sh` | `grep`/`jq` по логу (jq не гарантирован, сопоставление вызова с агентом в bash нечитаемо); pytest по логу (тянет pytest на хост стенда) | stdlib-скрипт `check_verdicts.py`, ~40 строк |
| `redact.py`, `spike.py`, `echo_mcp.py`, `run_spike.sh` | как в цикле 2: для разового стенда готового нет | без изменений решения |

## Не делаем, сознательно

- mutmut на плагине: он импортирует `cpex` (есть только в образе). Плагин после PyJWT — только сбор claims из контекста ContextForge; его поведение проверяет `check_verdicts.py` на каждом прогоне (принятая identity = вызвавший агент, `tool`, `ahs` на не-ASCII). Unit-тест плагина — в production-адаптере (M5).
- Echo не отвечает ошибкой на отклонённый вызов: вердикт пишется в лог и в ответ, tool всё равно «выполняется». Для критериев это эквивалентно; отказ `-32001` — в коннекторе M5 (заметка 5 критика).
- Shared store `jti`: в echo один процесс, требование в SEC-03, реализация — M1.
- Переход плагина на `extensions.http.headers`: задача production-адаптера; в ADR — депрекация и путь миграции.
