#!/usr/bin/env bash
# Full option C run, from scratch. Writes evidence/results-c-*.json and evidence/failclosed-c.log.
#   1. fresh stand on v1.1.1                     -> results-c-v1.1.1.json
#   2. fresh stand on v1.1.0                     -> results-c-v1.1.0.json
#   3. upgrade ContextForge to v1.1.1, same DB   -> results-c-upgraded.json (criterion 5)
#   4. adapter cannot load, default fail flag    -> failclosed-c.log
#   5. adapter cannot load, PLUGINS_FAIL_ON_PLUGIN_ERROR=true
set -euo pipefail
cd "$(dirname "$0")/.."
V111=ghcr.io/ibm/mcp-context-forge@sha256:16c7a8985c1a3528ba49fcab32951e06c9a41a171fc5c41d24b085283c4556a5
V110=ghcr.io/ibm/mcp-context-forge@sha256:7eb21fb9a8070df59435c03c76c40d8c71230c55ba89f884f2482d2cc81cff1c
DC="docker compose -f compose.yaml -f compose.c.yaml"
wait_up() { for _ in $(seq 1 90); do curl -sf localhost:4444/health >/dev/null && return 0; sleep 2; done; return 1; }
fresh() {
  $DC down -v >/dev/null 2>&1; rm -f secrets/state.json
  CONTEXTFORGE_IMAGE=$1 $DC up -d >/dev/null 2>&1; wait_up
  python3 scripts/setup.py >/dev/null
}
mkdir -p evidence/raw; : > evidence/echo-c.jsonl; : > evidence/raw/jws.jsonl
fresh "$V111"; python3 scripts/attacks_c.py c-v1.1.1
fresh "$V110"; python3 scripts/attacks_c.py c-v1.1.0 --paths-only
CONTEXTFORGE_IMAGE=$V111 $DC up -d contextforge >/dev/null 2>&1; wait_up   # upgrade in place, postgres kept
$DC exec -T contextforge sh -c 'grep -m1 "^version\|__version__" /app/mcpgateway/__init__.py' || true
python3 scripts/attacks_c.py c-upgraded

LOG=evidence/failclosed-c.log; : > "$LOG"
echo "## adapter broken, PLUGINS_FAIL_ON_PLUGIN_ERROR=false (ContextForge default)" >> "$LOG"
CONTEXTFORGE_IMAGE=$V111 PLUGINS_CONFIG=/app/gxg/plugins-broken.yaml FAIL_FLAG=false \
  docker compose -f compose.yaml -f compose.c.yaml -f compose.c-broken.yaml up -d contextforge >/dev/null 2>&1
if wait_up; then echo "gateway started: yes" >> "$LOG"; else echo "gateway started: no" >> "$LOG"; fi
$DC logs --no-log-prefix contextforge 2>&1 | grep -E "GxgIdentityAdapter|Plugin manager initialized" | tail -3 | cut -c1-260 >> "$LOG"
python3 scripts/attacks_c.py c-broken-default --paths-only | grep -E "^P-direct-rpc|^P-vs-direct" >> "$LOG"
echo "## adapter broken, PLUGINS_FAIL_ON_PLUGIN_ERROR=true" >> "$LOG"
CONTEXTFORGE_IMAGE=$V111 PLUGINS_CONFIG=/app/gxg/plugins-broken.yaml FAIL_FLAG=true \
  docker compose -f compose.yaml -f compose.c.yaml -f compose.c-broken.yaml up -d contextforge >/dev/null 2>&1
if wait_up; then echo "gateway started: yes" >> "$LOG"; else echo "gateway started: no (health not reached in 180 s)" >> "$LOG"; fi
$DC ps -a --format '{{.Service}} {{.State}} {{.Status}}' | grep contextforge >> "$LOG"
$DC logs --no-log-prefix contextforge 2>&1 | grep -E "GxgIdentityAdapter|missing.pem|Failed to load" | tail -3 | cut -c1-260 >> "$LOG"
python3 scripts/redact_jws.py evidence/echo-c.jsonl evidence/results-c-*.json
cat "$LOG"
