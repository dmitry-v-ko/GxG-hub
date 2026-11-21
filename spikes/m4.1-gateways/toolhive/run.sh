#!/usr/bin/env bash
# M4.1 ToolHive stand: vMCP v0.67.0 (pinned digest) + mock OIDC + M4 echo, all on the host network (loopback).
# Variants: VMCP_CONFIG=vmcp-tx.yaml VARIANT=token-exchange (RFC 8693 to backends); VMCP_SED = a sed script applied to
# the config (STS down, bad policy, backends behind thv proxies); WEBHOOK=1 = path C without a fork: vMCP -> token_exchange
# -> thv run proxy per connector (OIDC + mutating webhook, failure_policy: fail) -> echo, JWS in params._meta;
# WEBHOOK_DOWN=1 = the same with the webhook stopped before any call; WEBHOOK_DOWN=mid STEPS=midsession = stopped
# between calls in already open sessions (attacks.py run_midsession).
# Needs: docker, python3 with cryptography. Raw output -> secrets/ (git-ignored), redacted -> evidence/.
set -euo pipefail
cd "$(dirname "$0")"
VMCP=${VMCP_IMAGE:-ghcr.io/stacklok/toolhive/vmcp:v0.67.0@sha256:6416a07efaec881c1eee208f1818dcb997374ee3270a31487a47907d877c2be1}
THV=ghcr.io/stacklok/toolhive:v0.67.0@sha256:32579286003a97e67ab5fe324757d4945950e4aa25d7e84b3a96c8a9e16ef292
OIDC=ghcr.io/navikt/mock-oauth2-server:3.0.1@sha256:4f3660e5f3586b81fe2e0d42d0c9fcd3c8c64268db0d3ade261dadd70d27020b
source ../common.sh
start_webhook_proxies() {  # webhook signer + one thv proxy per connector, each with OIDC and the mutating webhook
  python3 -I -c 'import secrets;print(secrets.token_hex(32),end="")' > secrets/webhook-hmac
  docker run -d --name m41-webhook --network host -u 0:0 -v "$M4/echo:/echo:ro" -v "$PWD/webhook.py:/w/webhook.py:ro" \
    -v "$PWD/secrets:/s:ro" -v "$PWD/webhook-servers.json:/w/servers.json:ro" --entrypoint /app/.venv/bin/python3 \
    "$ECHO_IMAGE" -I /w/webhook.py 9100 /s/adapter_ed25519.pem /s/webhook-hmac /w/servers.json >/dev/null
  for t in alpha:8091 beta:8092; do
    docker run -d --name "m41-thv-${t%:*}" --network host -u 0:0 -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$PWD/webhook-thv.yaml:/w/webhook.yaml:ro" -v "$PWD/secrets/webhook-hmac:/run/gxg/webhook-hmac:ro" "$THV" run \
      "http://127.0.0.1:9000/mcp/t-${t%:*}" --name "echo-${t%:*}" --foreground --proxy-port "${t#*:}" \
      --oidc-issuer http://127.0.0.1:8080/gxg --oidc-insecure-allow-http --jwks-allow-private-ip \
      --webhook-config /w/webhook.yaml >/dev/null
  done
  for p in 8091 8092; do for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:$p/health" && break; sleep 2; done; done
  if [ "${WEBHOOK_DOWN:-}" = 1 ]; then docker stop m41-webhook >/dev/null; fi  # fail closed: proxies up, signer down
}
docker rm -f m41-oidc m41-vmcp m41-webhook m41-thv-alpha m41-thv-beta >/dev/null 2>&1 || true
start_echo host "${WEBHOOK:+-e ECHO_IDENTITY_META=gxg.dev/identity}"
docker run -d --name m41-oidc --network host -e SERVER_PORT=8080 -e JSON_CONFIG="$(cat oidc.json)" "$OIDC" >/dev/null
for _ in $(seq 1 60); do curl -sf http://127.0.0.1:8080/gxg/.well-known/openid-configuration >/dev/null && break; sleep 2; done
[ -n "${WEBHOOK:-}" ] && start_webhook_proxies
sed "${VMCP_SED:-}" "${VMCP_CONFIG:-vmcp.yaml}" > secrets/vmcp.yaml
docker run -d --name m41-vmcp --network host -v "$PWD/secrets/vmcp.yaml:/cfg/vmcp.yaml:ro" "$VMCP" serve -c /cfg/vmcp.yaml --host 127.0.0.1 --port 4483 >/dev/null
for _ in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:4483/health && break; sleep 2; done
tok() { curl -sf -d grant_type=client_credentials -d client_id="$1" -d client_secret=x -d scope=openid http://127.0.0.1:8080/gxg/token | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])'; }
U=http://127.0.0.1:4483/mcp
python3 -I - "${WEBHOOK:-}" "${WEBHOOK_DOWN:-}" "$(tok hermes-alpha)" "$(tok analyst-alpha)" "$(tok hermes-beta)" "$(tok analyst-beta)" "$(tok platform-admin)" <<P
import json, sys
wh, down, *t = sys.argv[1:]
# webhook variant: also the agent's own token straight to the thv proxy, bypassing vMCP and Cedar
def direct(port):
    return [{"label": "thv-proxy:direct", "url": f"http://127.0.0.1:{port}/mcp", "tool": "echo"}] if wh else []
json.dump({"paths": {"t-alpha": [{"label": "vmcp:prefixed", "url": "$U", "tool": "alpha_echo"}] + direct(8091),
                     "t-beta": [{"label": "vmcp:prefixed", "url": "$U", "tool": "beta_echo"}] + direct(8092)},
           "hidden": [{"label": "vmcp:filtered-out", "url": "$U", "tool": "hidden_echo"}],
           "tokens": {"agent:t-alpha:hermes": t[0], "agent:t-alpha:analyst": t[1], "agent:t-beta:hermes": t[2],
                      "agent:t-beta:analyst": t[3], "admin": t[4]},
           "stop_mid": ["docker", "stop", "m41-webhook"] if down == "mid" else None}, open("secrets/stand.json", "w"))
P
python3 -I ../attacks.py . ${STEPS:-calls attacks replay} > secrets/attacks.out
python3 -I ../list_tools.py . > secrets/visibility.json
O=evidence/${VARIANT:-default}; mkdir -p "$O"
docker logs m41-vmcp > secrets/vmcp.log 2>&1
L="secrets/vmcp.log $O/vmcp.log"
[ -n "${WEBHOOK:-}" ] && for c in webhook thv-alpha thv-beta; do docker logs "m41-$c" > "secrets/$c.log" 2>&1; L="$L secrets/$c.log $O/$c.log"; done
redact secrets/summary.json "$O/summary.json" secrets/raw/echo.jsonl "$O/echo-received.jsonl" \
  $L secrets/visibility.json "$O/visibility.json" secrets/observations.jsonl "$O/observations.jsonl"
echo "done; stop: docker rm -f m41-echo m41-oidc m41-vmcp m41-webhook m41-thv-alpha m41-thv-beta"
