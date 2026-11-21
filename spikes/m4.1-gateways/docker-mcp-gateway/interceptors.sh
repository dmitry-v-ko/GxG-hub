#!/usr/bin/env bash
# M4.1 Docker MCP Gateway: can a --interceptor change the request (path to variant C)? Same image, catalog and echo
# as run.sh. stdio: what a before:exec interceptor gets on stdin, and that its stdout replaces the result instead of
# calling the backend. streaming: any before interceptor breaks tools/call. Output -> evidence/interceptors/.
set -euo pipefail
cd "$(dirname "$0")"
GW=${GW_IMAGE:-docker/mcp-gateway:v0.44.3@sha256:bae58d801fd4b73f6ad91e609b8ce5ec8868713825f33a2bb20a98f956d0bd46}
source ../common.sh
docker network create m41 >/dev/null 2>&1 || true
docker rm -f m41-gw-icpt >/dev/null 2>&1 || true
start_echo m41 "-p 127.0.0.1:9000:9000"
O=evidence/interceptors; mkdir -p "$O"
FLAGS=(--catalog=gxg.yaml --servers=echo-alpha --static)
RUN=(--network m41 -e DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1 -v "$PWD/catalog.yaml:/root/.docker/mcp/catalogs/gxg.yaml:ro")
stdio() {  # $1 = interceptor spec; prints the tools/call response line (id 7)
  { echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"spike","version":"0"}}}'
    echo '{"jsonrpc":"2.0","method":"notifications/initialized"}'; sleep 3
    echo '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"alpha__echo","arguments":{"text":"hi"},"_meta":{"tenant_id":"t-beta"}}}'; sleep 5
  } | docker run -i --rm "${RUN[@]}" -v "$PWD/secrets:/out" "$GW" --transport=stdio "${FLAGS[@]}" --interceptor "$1" 2>/dev/null | grep '"id":7'
}
stdio 'before:exec:cat > /out/stdio-before-input.json' > /dev/null
cp secrets/stdio-before-input.json "$O/stdio-before-input.json"
stdio "before:exec:echo '{\"content\":[{\"type\":\"text\",\"text\":\"replaced by interceptor\"}]}'" > "$O/stdio-before-replaces-result.json"
: > "$O/streaming.jsonl"
for spec in 'before:exec:cat > /tmp/req.json' 'before:exec:exit 1'; do
  T=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
  docker run -d --name m41-gw-icpt "${RUN[@]}" -p 127.0.0.1:8814:8814 -e MCP_GATEWAY_AUTH_TOKEN="$T" "$GW" \
    --transport=streaming --host=0.0.0.0 --port=8814 "${FLAGS[@]}" --interceptor "$spec" >/dev/null
  for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8814/health >/dev/null && break; sleep 2; done
  python3 -I - "$spec" "$T" >> "$O/streaming.jsonl" <<'P'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd().parents[1] / "m4-contextforge"))
import spike
s, r = spike.call_tool("mcp", "http://127.0.0.1:8814/mcp", sys.argv[2], "alpha__echo")
print(json.dumps({"interceptor": sys.argv[1], "transport": "streaming", "status": s,
                  "reached_echo": spike.echo_seen(r) is not None, "response": r}))
P
  docker rm -f m41-gw-icpt >/dev/null
done
echo "done; tools/call that reached echo: $(grep -c tools/call secrets/raw/echo.jsonl || true); stop: docker rm -f m41-echo"
