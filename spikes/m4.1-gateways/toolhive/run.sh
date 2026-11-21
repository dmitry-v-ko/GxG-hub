#!/usr/bin/env bash
# M4.1 ToolHive stand: vMCP v0.67.0 (pinned digest) + mock OIDC + M4 echo, all on the host network (loopback).
# Variants: VMCP_CONFIG=vmcp-tx.yaml VARIANT=token-exchange (RFC 8693 to backends); vmcp-tx-down.yaml = STS down.
# Needs: docker, python3 with cryptography. Raw output -> secrets/ (git-ignored), redacted -> evidence/.
set -euo pipefail
cd "$(dirname "$0")"
VMCP=${VMCP_IMAGE:-ghcr.io/stacklok/toolhive/vmcp:v0.67.0@sha256:6416a07efaec881c1eee208f1818dcb997374ee3270a31487a47907d877c2be1}
OIDC=ghcr.io/navikt/mock-oauth2-server:3.0.1@sha256:4f3660e5f3586b81fe2e0d42d0c9fcd3c8c64268db0d3ade261dadd70d27020b
source ../common.sh
docker rm -f m41-oidc m41-vmcp >/dev/null 2>&1 || true
start_echo host
docker run -d --name m41-oidc --network host -e SERVER_PORT=8080 -e JSON_CONFIG="$(cat oidc.json)" "$OIDC" >/dev/null
for _ in $(seq 1 60); do curl -sf http://127.0.0.1:8080/gxg/.well-known/openid-configuration >/dev/null && break; sleep 2; done
docker run -d --name m41-vmcp --network host -v "$PWD/${VMCP_CONFIG:-vmcp.yaml}:/cfg/vmcp.yaml:ro" "$VMCP" serve -c /cfg/vmcp.yaml --host 127.0.0.1 --port 4483 >/dev/null
for _ in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:4483/health && break; sleep 2; done
tok() { curl -sf -d grant_type=client_credentials -d client_id="$1" -d client_secret=x -d scope=openid http://127.0.0.1:8080/gxg/token | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])'; }
U=http://127.0.0.1:4483/mcp
python3 -I - "$(tok hermes-alpha)" "$(tok analyst-alpha)" "$(tok hermes-beta)" "$(tok analyst-beta)" "$(tok platform-admin)" <<P
import json, sys
t = sys.argv[1:]
json.dump({"paths": {"t-alpha": [{"label": "vmcp:prefixed", "url": "$U", "tool": "alpha_echo"}],
                     "t-beta": [{"label": "vmcp:prefixed", "url": "$U", "tool": "beta_echo"}]},
           "hidden": [{"label": "vmcp:filtered-out", "url": "$U", "tool": "hidden_echo"}],
           "tokens": {"agent:t-alpha:hermes": t[0], "agent:t-alpha:analyst": t[1], "agent:t-beta:hermes": t[2],
                      "agent:t-beta:analyst": t[3], "admin": t[4]}}, open("secrets/stand.json", "w"))
P
python3 -I ../attacks.py . ${STEPS:-calls attacks replay} > secrets/attacks.out
python3 -I ../list_tools.py . > secrets/visibility.json
docker logs m41-vmcp > secrets/vmcp.log 2>&1
O=evidence/${VARIANT:-default}; mkdir -p "$O"
redact secrets/summary.json "$O/summary.json" secrets/raw/echo.jsonl "$O/echo-received.jsonl" \
  secrets/vmcp.log "$O/vmcp.log" secrets/visibility.json "$O/visibility.json" secrets/observations.jsonl "$O/observations.jsonl"
echo "done; stop: docker rm -f m41-echo m41-oidc m41-vmcp"
