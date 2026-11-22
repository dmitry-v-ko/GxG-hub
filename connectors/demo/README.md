# Reference-коннектор `demo`

Спека: [demo-connector-v1](../../docs/specs/demo-connector-v1.md). Этап B (M3): чтение. Этап C (M8): `demo.message.propose`, внутренние `execute`/`reconcile`, fake provider — включаются переменной `DEMO_APPROVAL_URL`; без неё коннектор read-only (так на стенде M5).

| Tool | Что делает |
| --- | --- |
| `demo.boards.list` | Доски, которые вызывающему можно читать |
| `demo.notes.search` | Поиск по разрешённым доскам: `index`, при его отказе до запуска — `scan` (2 с); cursor подписан и привязан к вызывающему и запросу |
| `demo.note.get` | Заметка; доска определяется по backend'у. Чужая или скрытая заметка — `not_found`, как несуществующая |
| `demo.message.propose` (M8) | Права `propose` на доску (нет права вообще — `not_found`, есть только `read` — `resource_forbidden`), канонизация `{recipient, text}` (NFC, EFF-05), preview из канонических args и названия доски, `POST /v1/intents` с service JWT. Ответ сразу: `{intent_id, status}`. Approval Service недоступен — `temporarily_unavailable`, intent не создан |

## Внутренний порт 8081 (M8, только для executor'а, SEC-14)

`POST /internal/v1/execute/demo.message.execute` — по EFF-18: service JWT вида `executor` → execution token (kit: подпись по `kid` из JWKS `api`, кеш ≤ 10 мин, `aud` = `demo`, `exp`, локальный кеш `jti`) и его привязка к телу → хеш args = `ahs` → tool, connection, доска из токена — этого коннектора; у requester'а всё ещё есть `propose` по текущему bundle → `dispatch` в Approval Service (CAS, kill switch) → один вызов провайдера с ключом идемпотентности провайдера = `intent_id` (EFF-18 п.5; не `idempotency_key` агента — он уникален только в паре с requester). До `dispatch`: 401 (токены), 409 (повтор уже исполненного токена или отказ `dispatch`), 422 (args), 423 (kill switch), 403 (цель или право), 503 (`dispatch` недоступен) — провайдер не вызывается. После `dispatch` любой сбой — `outcome_unknown` в effect-result.

`POST /internal/v1/reconcile/demo.message.execute` — поиск в журнале провайдера по `intent_id`: `executed` / `not_executed`.

Fake provider (`provider.py`): журнал вызовов в SQLite (`DEMO_PROVIDER_DB`, переживает рестарт), идемпотентность по `provider_key` = `intent_id`; `reconcile` ищет по нему же (EFF-25). Режимы (только тестовый профиль): `PUT /_test/provider/mode {"mode": "<ok|reject|rate_limited|timeout_before_start|commit_then_drop>[,no_dedupe][,slow:<ms>]", "count": n}` — режим на n вызовов, затем `ok`; `GET /_test/provider/calls?provider_key=…`. Провайдер «получает» запрос сразу (строка журнала), `slow` задерживает только ответ.

Порядок проверок каждого вызова: identity из заголовка (верификатор кита) → входная схема → grants (`allowed()` до backend'а, `check()` на каждом элементе) → backend. Аргументы и `_meta` полномочий не дают.

## Запуск

```bash
docker build -f connectors/demo/Dockerfile .          # из корня репозитория
docker run -p 8080:8080 -v "$PWD/bundle:/bundle:ro" -e DEMO_BUNDLE_KEYS=<kid>=ed25519:<key> <image>
```

Переменные окружения описаны в `src/demo_connector/server.py` (этап C: `DEMO_APPROVAL_URL`, `DEMO_APPROVAL_AUDIENCE`, `DEMO_SVC_KEY_FILE` — секретный файл, `DEMO_PROVIDER_DB`). С эффектами `/readyz` требует ещё JWKS executor'а из `api` (MCC-09). MCP — `POST /mcp` (Streamable HTTP), `GET /healthz`, `GET /readyz` (200 при валидном bundle, иначе 503; причина только в логе).

Identity от ContextForge (ADR-0001, вариант C): `DEMO_IDENTITY_KEYS=<kid>=ed25519:<key>[,…]` — публичные ключи адаптера `gxg_identity`. Коннектор проверяет `X-GxG-Identity` через `gxg_connector_kit.cf_identity.ContextForgeVerifier`, включая привязку к tool и хешу аргументов вызова (SEC-03). Без ключей каждый вызов tool'а — `unauthenticated`. Тестовый issuer (`DEMO_TEST_ISSUER_KEY`) и `DEMO_TEST_CONTROLS=1` вне тестового профиля не дают сервису стартовать. Тестовый issuer и `DEMO_IDENTITY_KEYS` вместе тоже не стартуют.

Тайм-аут tool'а — `limits.timeout_s` из `gxg-manifest.yaml`. Без ответа backend'а за это время — `temporarily_unavailable` (`retryable`).

Тестовые контроли (только `GXG_PROFILE=test`, `DEMO_TEST_CONTROLS=1`, `127.0.0.1:8090`; в production-образе модуля нет, стенд M5 собирает образ с `KEEP_TEST_CONTROLS=1`): `POST /_test/index/fail`, `POST /_test/backend/fail {"mode": "rate_limited" | "temporarily_unavailable" | "timeout_before_start" | "slow:<ms>" | null}`, `GET /_test/backend/calls`. Стенд — [stand/README.md](../../stand/README.md).

## Тесты

- `uv run pytest` здесь или из корня: официальный MCP SDK-клиент через ASGI-транспорт, bundle собирает `gxg-policy build` из `fixtures/grants/` с ключом, созданным в тесте.
- `connectors/demo/docker_smoke.sh`: сборка и запуск образа (AC-M3-01, AC-M3-11).
- `uv run mutmut run` здесь (или `scripts/ci/mutmut.sh` для всех вех): `tools.py`, `backend.py`.
