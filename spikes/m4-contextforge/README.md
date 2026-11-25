# M4 — спайк ContextForge: identity propagation

Стенд и сценарий для [ADR-0001](../../docs/adr/0001-identity-propagation.md). Только локально, только синтетические данные.

## Что здесь

| Файл | Назначение |
| --- | --- |
| `compose.yaml` | ContextForge `v1.1.1` + PostgreSQL 18 + echo-MCP (на том же образе ContextForge: там есть PyJWT), всё по digest. Вариант A включён: `IDENTITY_PROPAGATION_ENABLED`, `IDENTITY_SIGN_CLAIMS` |
| `compose.variant-c.yaml` | Overlay варианта C: плагин `plugin/gxg_identity` (`tool_pre_invoke`, JWS Ed25519 через PyJWT), `PLUGINS_FAIL_ON_PLUGIN_ERROR=true`, `MCPGATEWAY_DIRECT_PROXY_ENABLED=false`; echo получает публичный ключ адаптера и проверяет каждый `tools/call` при приёме |
| `compose.upgrade-from.yaml` | Overlay для критерия 5: старт на `v1.1.0`, затем апгрейд на закреплённый `v1.1.1` |
| `echo/echo_mcp.py` | Echo-MCP-сервер: логирует все заголовки и `_meta`, tool `echo` возвращает то, что получил; в варианте C — ещё вердикт верификатора |
| `echo/verify_jws.py` | Проверка варианта C на стороне коннектора (прообраз `IdentityVerifier`): PyJWT + свои `tool`, `ahs`, TTL, `sub` в tenant, `jti` |
| `tests/test_verify_jws.py` | Unit-тесты верификатора (в CI, без сети); `mutmut.sh` + `setup.cfg` — mutmut на нём, отчёт в `reports/mutmut/M4.txt` |
| `check_verdicts.py` | Проверка вердиктов прогона: все вызовы через ContextForge приняты с identity вызвавшего агента, каждая атака отклонена с ожидаемой причиной; иначе `run_spike.sh` падает |
| `spike.py` | Настройка (2 team × 2 агента, team-scoped API-токены, gateway на team, virtual server, алиас), вызовы по всем путям, атаки |
| `verify_hmac.py` | Проверка подписи варианта A на стороне коннектора, формула из исходника v1.1.1 |
| `redact.py` | Копирует сырой вывод из `secrets/` в `evidence/` без токенов и подписей |
| `run_spike.sh` | Весь прогон: A, C, fail closed без ключа адаптера (+ контроль), апгрейд |
| `evidence/` | Доказательства без секретов |

## Как воспроизвести

```bash
cd spikes/m4-contextforge
./run_spike.sh            # ~15 мин: A, C, fail closed, апгрейд; вывод в evidence/
python3 -I spike.py visibility   # что видит каждый агент + claims токена агента
docker compose -f compose.yaml -f compose.variant-c.yaml down -v
```

Нужно: Docker с compose, `python3` с `cryptography` (генерация ключа и подделка JWS в `spike.py`), доступ к `ghcr.io` и `docker.io`.
Тесты верификатора: `uv run pytest spikes/m4-contextforge/tests` из корня; мутации: `spikes/m4-contextforge/mutmut.sh`.
`gen_env.sh` генерирует тестовые секреты в `.env`, ключ адаптера лежит в `secrets/`. Оба пути игнорируются git.

## Пути вызова

| Путь в ADR | Как вызывается на стенде |
| --- | --- |
| Прямой tool | `POST /rpc` `tools/call`; глобальный `POST /mcp/` (streamable HTTP) |
| Virtual server | `POST /servers/{id}/mcp/` |
| Алиас | tool переименован через `PUT /v1/tools/{id}` `customName`, вызов по новому имени через `/rpc` и через virtual server |

У t-alpha исходное имя tool после переименования больше не существует (`Tool not found`). Поэтому прямые вызовы под исходным именем проверяются на t-beta, а алиас — на t-alpha.
