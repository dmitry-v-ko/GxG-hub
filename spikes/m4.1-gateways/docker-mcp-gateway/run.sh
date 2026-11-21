#!/usr/bin/env bash
# M4.1 Docker MCP Gateway stand: docker/mcp-gateway v0.44.3 (pinned digest) + M4 echo on a bridge network.
# The gateway has one shared bearer token per instance and no caller identity, so tenants = instances:
#   gw-alpha (127.0.0.1:8811): echo-alpha + echo-hidden, --tools=echo-alpha:echo hides hidden__echo
#   gw-beta  (127.0.0.1:8812): echo-beta
#   gw-shared(127.0.0.1:8813): echo-alpha + echo-beta in one instance (what one instance gives two tenants)
# No Docker Desktop, no docker socket (--static: without it the gateway queries the Docker API at start and exits). Raw -> secrets/ (git-ignored), redacted -> evidence/.
set -euo pipefail
cd "$(dirname "$0")"
GW=${GW_IMAGE:-docker/mcp-gateway:v0.44.3@sha256:bae58d801fd4b73f6ad91e609b8ce5ec8868713825f33a2bb20a98f956d0bd46}
source ../common.sh
docker network create m41 >/dev/null 2>&1 || true
docker rm -f m41-gw-alpha m41-gw-beta m41-gw-shared >/dev/null 2>&1 || true
start_echo m41 "-p 127.0.0.1:9000:9000"
r() { python3 -c 'import secrets;print(secrets.token_urlsafe(32))'; }
TA=$(r); TB=$(r); TS=$(r)
gw() {  # name port token servers [extra flags]
  docker run -d --name "m41-gw-$1" --network m41 -p "127.0.0.1:$2:$2" -e MCP_GATEWAY_AUTH_TOKEN="$3" \
    -e DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1 -v "$PWD/catalog.yaml:/root/.docker/mcp/catalogs/gxg.yaml:ro" \
    "$GW" --transport=streaming --host=0.0.0.0 --port="$2" --catalog=gxg.yaml --servers="$4" --static ${5:-} >/dev/null
}
gw alpha 8811 "$TA" echo-alpha,echo-hidden "--tools=echo-alpha:echo"
gw beta 8812 "$TB" echo-beta
gw shared 8813 "$TS" echo-alpha,echo-beta
for p in 8811 8812 8813; do for _ in $(seq 1 40); do curl -sf "http://127.0.0.1:$p/health" >/dev/null && break; sleep 2; done; done
python3 -I - "$TA" "$TB" "$TS" <<'P'
import json, sys
a, b, s = sys.argv[1:]
A, B, S = "http://127.0.0.1:8811/mcp", "http://127.0.0.1:8812/mcp", "http://127.0.0.1:8813/mcp"
json.dump({"paths": {"t-alpha": [{"label": "gw-alpha:prefixed", "url": A, "tool": "alpha__echo"},
                                 {"label": "gw-shared:prefixed", "url": S, "tool": "alpha__echo"}],
                     "t-beta": [{"label": "gw-beta:prefixed", "url": B, "tool": "beta__echo"},
                                {"label": "gw-shared:prefixed", "url": S, "tool": "beta__echo"}]},
           "hidden": [{"label": "gw-alpha:--tools-filtered", "url": A, "tool": "hidden__echo", "agent": "agent:t-alpha:hermes"}],
           # one token per instance: both agents of a tenant hold the same credential; no admin concept.
           # shortcut: on gw-shared both tenants hold the shared token, so cross-tenant uses it too.
           "tokens": {"agent:t-alpha:hermes": a, "agent:t-alpha:analyst": a, "agent:t-beta:hermes": b,
                      "agent:t-beta:analyst": b, "admin": None},
           "token_for_url": {S: s}}, open("secrets/stand.json", "w"))
P
python3 -I ../attacks.py . ${STEPS:-calls attacks replay} > secrets/attacks.out
python3 -I ../list_tools.py . > secrets/visibility.json
for g in alpha beta shared; do docker logs "m41-gw-$g" > "secrets/gw-$g.log" 2>&1; done
O=evidence/${VARIANT:-default}; mkdir -p "$O"
redact secrets/summary.json "$O/summary.json" secrets/raw/echo.jsonl "$O/echo-received.jsonl" \
  secrets/visibility.json "$O/visibility.json" secrets/observations.jsonl "$O/observations.jsonl" \
  secrets/gw-alpha.log "$O/gw-alpha.log" secrets/gw-beta.log "$O/gw-beta.log" secrets/gw-shared.log "$O/gw-shared.log"
echo "done; stop: docker rm -f m41-echo m41-gw-alpha m41-gw-beta m41-gw-shared"
