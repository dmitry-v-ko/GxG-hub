# Reference-коннектор `demo`

Спека: [demo-connector-v1](../../docs/specs/demo-connector-v1.md). Сейчас этап B (M3): только чтение, без ContextForge.

| Tool | Что делает |
| --- | --- |
| `demo.boards.list` | Доски, которые вызывающему можно читать |
| `demo.notes.search` | Поиск по разрешённым доскам: `index`, при его отказе до запуска — `scan` (2 с); cursor подписан и привязан к вызывающему и запросу |
| `demo.note.get` | Заметка; доска определяется по backend'у. Чужая или скрытая заметка — `not_found`, как несуществующая |

Порядок проверок каждого вызова: identity из заголовка (верификатор кита) → входная схема → grants (`allowed()` до backend'а, `check()` на каждом элементе) → backend. Аргументы и `_meta` полномочий не дают.

## Запуск

```bash
docker build -f connectors/demo/Dockerfile .          # из корня репозитория
docker run -p 8080:8080 -v "$PWD/bundle:/bundle:ro" -e DEMO_BUNDLE_KEYS=<kid>=ed25519:<key> <image>
```

Переменные окружения описаны в `src/demo_connector/server.py`. MCP — `POST /mcp` (Streamable HTTP), `GET /healthz`, `GET /readyz` (200 при валидном bundle, иначе 503; причина только в логе).

Identity от ContextForge (ADR-0001, вариант C): `DEMO_IDENTITY_KEYS=<kid>=ed25519:<key>[,…]` — публичные ключи адаптера `gxg_identity`. Коннектор проверяет `X-GxG-Identity` через `gxg_connector_kit.cf_identity.ContextForgeVerifier`, включая привязку к tool и хешу аргументов вызова (SEC-03). Без ключей каждый вызов tool'а — `unauthenticated`. Тестовый issuer (`DEMO_TEST_ISSUER_KEY`) и `DEMO_TEST_CONTROLS=1` вне тестового профиля не дают сервису стартовать. Тестовый issuer и `DEMO_IDENTITY_KEYS` вместе тоже не стартуют.

Тайм-аут tool'а — `limits.timeout_s` из `gxg-manifest.yaml`. Без ответа backend'а за это время — `temporarily_unavailable` (`retryable`).

Тестовые контроли (только `GXG_PROFILE=test`, `DEMO_TEST_CONTROLS=1`, `127.0.0.1:8090`; в production-образе модуля нет, стенд M5 собирает образ с `KEEP_TEST_CONTROLS=1`): `POST /_test/index/fail`, `POST /_test/backend/fail {"mode": "rate_limited" | "temporarily_unavailable" | "timeout_before_start" | "slow:<ms>" | null}`, `GET /_test/backend/calls`. Стенд — [stand/README.md](../../stand/README.md).

## Тесты

- `uv run pytest` здесь или из корня: официальный MCP SDK-клиент через ASGI-транспорт, bundle собирает `gxg-policy build` из `fixtures/grants/` с ключом, созданным в тесте.
- `connectors/demo/docker_smoke.sh`: сборка и запуск образа (AC-M3-01, AC-M3-11).
- `uv run mutmut run` здесь (или `scripts/ci/mutmut.sh` для всех вех): `tools.py`, `backend.py`.
