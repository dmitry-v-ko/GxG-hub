# M4.1 — спайк ADR-0002: ToolHive и Docker MCP Gateway

Стенды для [ADR-0002](../../docs/adr/0002-gateway-choice.md). Только локально, только синтетические данные. ContextForge здесь не гоняется: его результаты — в ADR-0001 и `reports/conformance/M4.json`.

## Что здесь

| Файл | Назначение |
| --- | --- |
| `common.sh` | Общее для обоих стендов: echo-MCP из M4 (`../m4-contextforge/echo/`, монтируется, не копируется) с верификатором варианта C, ключ адаптера, редактирование секретов в `evidence/` |
| `attacks.py` | Вызовы и атаки M4 для любого кандидата: вызов каждым агентом, подделка заголовков/`_meta`/аргументов, вызов без токена, admin-токен, чужой tenant, скрытый tool по имени, повтор/обход/подделка напрямую на echo. Импортирует `spike.py` (HTTP, MCP-сессия, `tools/call`, поддельный JWS) и `check_verdicts.EXPECTED` из M4 |
| `list_tools.py` | `tools/list` каждого агента через шлюз: что видит tenant |
| `toolhive/run.sh` | vMCP `v0.67.0` (static backends, без Kubernetes) + mock OIDC (`tenant_id` в claims) + echo; Cedar deny by default; варианты — `VMCP_SED` поверх `vmcp.yaml`/`vmcp-tx.yaml`: `token_exchange` к коннекторам, STS недоступен, плохая политика, апгрейд с `v0.66.0`; `WEBHOOK=1` — путь C без форка (ниже) |
| `toolhive/webhook.py`, `webhook-thv.yaml`, `webhook-servers.json` | Путь C на ToolHive: mutating webhook `thv run` подписывает JWS формата ADR-0001 и кладёт его в `params._meta["gxg.dev/identity"]`; цепочка vMCP → `token_exchange` → `thv run` на коннектор (OIDC + webhook, `failure_policy: fail`) → echo (читает JWS из `_meta`: `ECHO_IDENTITY_META`) |
| `toolhive/verify_tx.py` | Как коннектор проверил бы `token_exchange`-токен: подпись IdP, claims, повторы, хранилище `jti` |
| `tests/test_webhook.py` | Self-check webhook: HMAC, отказы, JWS принимается верификатором M4 |
| `docker-mcp-gateway/run.sh` | `docker/mcp-gateway` `v0.44.3`: инстанс на tenant + общий инстанс на двоих; без Docker Desktop и docker socket (`--static`) |
| `docker-mcp-gateway/interceptors.sh` | Перехватчики `before:exec` на stdio и streaming: что получают, меняют ли запрос |
| `*/evidence/` | Доказательства без секретов (токены и JWT вырезаны) |

## Как воспроизвести

```bash
dockerd &   # в облачном контейнере — вручную
cd spikes/m4.1-gateways/toolhive
S_WH='s#9000/mcp/t-alpha#8091/mcp#;s#9000/mcp/t-beta#8092/mcp#'   # backends alpha/beta -> thv proxies
./run.sh                                                    # default: unauthenticated к коннекторам
VMCP_CONFIG=vmcp-tx.yaml VARIANT=token-exchange ./run.sh && python3 -I verify_tx.py > evidence/token-exchange/verify_tx.json
STEPS=calls VMCP_CONFIG=vmcp-tx.yaml VMCP_SED='s#8080/gxg/token#8099/down/token#' VARIANT=token-exchange-sts-down ./run.sh
VMCP_IMAGE=ghcr.io/stacklok/toolhive/vmcp:v0.66.0@sha256:4f5ac6d239c6e7de6d6b141cb9e406bd38c3144dca8f618011fe491c7777f352 \
  STEPS="calls attacks" VMCP_CONFIG=vmcp-tx.yaml VARIANT=upgrade-from-v0.66.0 ./run.sh
WEBHOOK=1 VMCP_CONFIG=vmcp-tx.yaml VMCP_SED="$S_WH" VARIANT=webhook-c ./run.sh             # путь C без форка
WEBHOOK=1 WEBHOOK_DOWN=1 STEPS=calls VMCP_CONFIG=vmcp-tx.yaml VMCP_SED="$S_WH" VARIANT=webhook-c-down ./run.sh
WEBHOOK=1 WEBHOOK_DOWN=mid STEPS=midsession VMCP_CONFIG=vmcp-tx.yaml VMCP_SED="$S_WH" VARIANT=webhook-c-mid-down ./run.sh  # webhook стоп посреди сессии
sed 's#Action::"list_tools", resource);#Action::"list_tools", resource) when { this is not cedar };#' vmcp.yaml > secrets/vmcp-badpolicy.yaml
docker run --rm --network host -v $PWD/secrets/vmcp-badpolicy.yaml:/c.yaml:ro ghcr.io/stacklok/toolhive/vmcp:v0.67.0 serve -c /c.yaml  # не стартует
docker rm -f m41-echo m41-oidc m41-vmcp m41-webhook m41-thv-alpha m41-thv-beta

cd ../docker-mcp-gateway
./run.sh
GW_IMAGE=docker/mcp-gateway:v0.44.2@sha256:bc104370585ec26e2e7243ace354caa8ba7b05d9f8cea01b177016b0598dbcb1 \
  STEPS="calls attacks" VARIANT=previous-v0.44.2 ./run.sh
docker rm -f m41-echo m41-gw-alpha m41-gw-beta m41-gw-shared
./interceptors.sh && docker rm -f m41-echo                  # -> evidence/interceptors/
```

Нужно: Docker (для `WEBHOOK=1` — docker socket `/var/run/docker.sock`: `thv` без container runtime не стартует даже для remote URL), `python3` с `cryptography` и PyJWT (для `verify_tx.py`), доступ к `ghcr.io` и `docker.io`.
