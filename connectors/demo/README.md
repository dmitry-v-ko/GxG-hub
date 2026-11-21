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

Вне `GXG_PROFILE=test` верификатора identity пока нет (вариант C ADR-0001 ещё не в ките), поэтому каждый вызов tool'а — `unauthenticated`. Тестовый issuer (`DEMO_TEST_ISSUER_KEY`) и `DEMO_TEST_CONTROLS=1` вне тестового профиля не дают сервису стартовать.

## Тесты

- `uv run pytest` здесь или из корня: официальный MCP SDK-клиент через ASGI-транспорт, bundle собирает `gxg-policy build` из `fixtures/grants/` с ключом, созданным в тесте.
- `connectors/demo/docker_smoke.sh`: сборка и запуск образа (AC-M3-01, AC-M3-11).
- `uv run mutmut run` здесь (или `scripts/ci/mutmut.sh` для всех вех): `tools.py`, `backend.py`.
