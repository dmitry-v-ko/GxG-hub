# M4 — спайк ContextForge: identity propagation

Стенд и сценарий для [ADR-0001](../../docs/adr/0001-identity-propagation.md). Только локально, только синтетические данные.

## Что здесь

| Файл | Назначение |
| --- | --- |
| `compose.yaml` | ContextForge `v1.1.1` + PostgreSQL 18 + echo-MCP, всё по digest. Вариант A включён: `IDENTITY_PROPAGATION_ENABLED`, `IDENTITY_SIGN_CLAIMS` |
| `compose.variant-c.yaml` | Overlay варианта C: плагин `plugin/gxg_identity` (`tool_pre_invoke`, JWS Ed25519) |
| `compose.upgrade-from.yaml` | Overlay для критерия 5: старт на `v1.1.0`, затем апгрейд на закреплённый `v1.1.1` |
| `compose.passthrough-misconfig.yaml` | Худший случай конфигурации: `ENABLE_HEADER_PASSTHROUGH=true` и все identity-заголовки в allowlist |
| `echo/echo_mcp.py` | Echo-MCP-сервер на stdlib: логирует все заголовки и `_meta`, tool `echo` возвращает то, что получил |
| `spike.py` | Настройка (2 team × 2 агента, team-scoped API-токены, gateway на team, virtual server, алиас), вызовы по всем путям, атаки |
| `verify_hmac.py` | Проверка подписи варианта A на стороне коннектора, формула из исходника v1.1.1 |
| `verify_jws.py` | Проверка варианта C на стороне коннектора (прообраз `IdentityVerifier`) по каждому запросу, который получил echo |
| `redact.py` | Копирует сырой вывод из `secrets/` в `evidence/` без токенов и подписей |
| `run_spike.sh` | Весь прогон: A, C, апгрейд |
| `evidence/` | Доказательства без секретов |

## Как воспроизвести

```bash
cd spikes/m4-contextforge
./run_spike.sh            # ~10 мин: A, C, апгрейд; вывод в evidence/
python3 -I spike.py visibility   # что видит каждый агент + claims токена агента
docker compose -f compose.yaml -f compose.variant-c.yaml down -v
```

Нужно: Docker с compose, `python3` с `cryptography` (только для `verify_jws.py` и генерации ключа), доступ к `ghcr.io` и `docker.io`.
`gen_env.sh` генерирует тестовые секреты в `.env`, ключ адаптера лежит в `secrets/`. Оба пути игнорируются git.

## Пути вызова

| Путь в ADR | Как вызывается на стенде |
| --- | --- |
| Прямой tool | `POST /rpc` `tools/call`; глобальный `POST /mcp/` (streamable HTTP) |
| Virtual server | `POST /servers/{id}/mcp/` |
| Алиас | tool переименован через `PUT /v1/tools/{id}` `customName`, вызов по новому имени через `/rpc` и через virtual server |

У t-alpha исходное имя tool после переименования больше не существует (`Tool not found`). Поэтому прямые вызовы под исходным именем проверяются на t-beta, а алиас — на t-alpha.
