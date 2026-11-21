# M4.1 — спайк ADR-0002: ToolHive и Docker MCP Gateway

Стенды для [ADR-0002](../../docs/adr/0002-gateway-choice.md). Только локально, только синтетические данные. ContextForge здесь не гоняется: его результаты — в ADR-0001 и `reports/conformance/M4.json`.

## Что здесь

| Файл | Назначение |
| --- | --- |
| `common.sh` | Общее для обоих стендов: echo-MCP из M4 (`../m4-contextforge/echo/`, монтируется, не копируется) с верификатором варианта C, ключ адаптера, редактирование секретов в `evidence/` |
| `attacks.py` | Вызовы и атаки M4 для любого кандидата: вызов каждым агентом, подделка заголовков/`_meta`/аргументов, вызов без токена, admin-токен, чужой tenant, скрытый tool по имени, повтор/обход/подделка напрямую на echo. Импортирует `spike.py` (HTTP, MCP-сессия, `tools/call`, поддельный JWS) и `check_verdicts.EXPECTED` из M4 |
| `list_tools.py` | `tools/list` каждого агента через шлюз: что видит tenant |
| `toolhive/run.sh` | vMCP `v0.67.0` (static backends, без Kubernetes) + mock OIDC (`tenant_id` в claims) + echo; Cedar deny by default; варианты: `token_exchange` к коннекторам, STS недоступен, апгрейд с `v0.66.0` |
| `toolhive/verify_tx.py` | Как коннектор проверил бы `token_exchange`-токен: подпись IdP, claims, повторы |
| `docker-mcp-gateway/run.sh` | `docker/mcp-gateway` `v0.44.3`: инстанс на tenant + общий инстанс на двоих; без Docker Desktop и docker socket (`--static`) |
| `*/evidence/` | Доказательства без секретов (токены и JWT вырезаны) |

## Как воспроизвести

```bash
dockerd &   # в облачном контейнере — вручную
cd spikes/m4.1-gateways/toolhive
./run.sh                                                    # default: unauthenticated к коннекторам
VMCP_CONFIG=vmcp-tx.yaml VARIANT=token-exchange ./run.sh && python3 -I verify_tx.py
STEPS=calls VMCP_CONFIG=vmcp-tx-down.yaml VARIANT=token-exchange-sts-down ./run.sh
VMCP_IMAGE=ghcr.io/stacklok/toolhive/vmcp:v0.66.0@sha256:4f5ac6d239c6e7de6d6b141cb9e406bd38c3144dca8f618011fe491c7777f352 \
  STEPS="calls attacks" VMCP_CONFIG=vmcp-tx.yaml VARIANT=upgrade-from-v0.66.0 ./run.sh
docker run --rm --network host -v $PWD/vmcp-badpolicy.yaml:/c.yaml:ro ghcr.io/stacklok/toolhive/vmcp:v0.67.0 serve -c /c.yaml  # не стартует
docker rm -f m41-echo m41-oidc m41-vmcp

cd ../docker-mcp-gateway
./run.sh
GW_IMAGE=docker/mcp-gateway:v0.44.2@sha256:bc104370585ec26e2e7243ace354caa8ba7b05d9f8cea01b177016b0598dbcb1 \
  STEPS="calls attacks" VARIANT=previous-v0.44.2 ./run.sh
docker rm -f m41-echo m41-gw-alpha m41-gw-beta m41-gw-shared
```

Перехватчики Docker MCP Gateway проверялись отдельными командами `docker run … --interceptor=…` (streaming и stdio), вывод — в `docker-mcp-gateway/evidence/interceptors/`; команды — в ADR-0002, пункт 2.

Нужно: Docker, `python3` с `cryptography` и PyJWT (для `verify_tx.py`), доступ к `ghcr.io` и `docker.io`.
