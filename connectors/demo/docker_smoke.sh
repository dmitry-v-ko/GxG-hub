#!/usr/bin/env bash
# AC-M3-01 and AC-M3-11 on the production image. Run from anywhere: connectors/demo/docker_smoke.sh
#   docker build + docker run: /readyz = 200 with a valid bundle (built by gxg-policy build from
#   fixtures/grants/ with a throw-away key), 503 without one; test switches stop the start (exit != 0);
#   the image has no test controls module; a tools/call writes its JSON audit line to the container's stdout.
# Extra `docker build` flags (e.g. a proxy CA secret in a sandbox): DOCKER_BUILD_FLAGS.
set -euo pipefail
cd "$(dirname "$0")/../.."
image=gxg-demo:smoke
tmp=$(mktemp -d)
prefix="gxg-demo-smoke-$$-"
cleanup() {
  docker ps -aq --filter "name=$prefix" | xargs -r docker rm -f > /dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT

# shellcheck disable=SC2086 # DOCKER_BUILD_FLAGS is a list of flags
docker build -q ${DOCKER_BUILD_FLAGS:-} -t "$image" -f connectors/demo/Dockerfile . > /dev/null

mkdir -p "$tmp/policies/tenants" "$tmp/policies/platform"
cp connectors/demo/fixtures/grants/t-*.yaml "$tmp/policies/tenants/"
cp policies/platform/registry.example.yaml "$tmp/policies/platform/"
read -r seed pub < <(uv run --quiet python -c '
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k = Ed25519PrivateKey.generate()
b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
print(b(k.private_bytes_raw()), b(k.public_key().public_bytes_raw()))')
GXG_BUNDLE_SIGNING_KEY="$seed" uv run --quiet gxg-policy build "$tmp/policies" --out "$tmp/bundle" \
  --bundle-version "$(git rev-parse HEAD)" --kid smoke --manifest connectors/demo/gxg-manifest.yaml > /dev/null
chmod 755 "$tmp" "$tmp/bundle" && chmod 644 "$tmp/bundle"/*

run() {  # run <name> [docker run args...]: start detached as $prefix<name>, print the host port of 8080
  docker run -d --name "$prefix$1" -p 127.0.0.1::8080 "${@:2}" "$image" > /dev/null
  docker port "$prefix$1" 8080/tcp | head -1 | sed 's/.*://'
}
status() { curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1$2" || true; }
wait_up() {  # wait_up <port> <name>: until /healthz answers 200
  for _ in $(seq 1 60); do [ "$(status "$1" /healthz)" = 200 ] && return 0; sleep 0.5; done
  echo "FAIL: /healthz on :$1 never answered"; docker logs "$prefix$2" || true; exit 1
}
expect() {  # expect <want> <got> <what>
  if [ "$1" != "$2" ]; then echo "FAIL: $3: want $1, got $2"; exit 1; fi
  echo "ok: $3 = $2"
}

port=$(run with-bundle -v "$tmp/bundle:/bundle:ro" -e "DEMO_BUNDLE_KEYS=smoke=ed25519:$pub")
wait_up "$port" with-bundle
expect 200 "$(status "$port" /readyz)" "AC-M3-01 /readyz with a valid bundle"
body=$(curl -s "http://127.0.0.1:$port/readyz")
expect '{"status":"ready"}' "$body" "/readyz body"
# MCC-08: production has no identity verifier, so the call is refused, and still audited as a JSON line on stdout.
call='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo.boards.list","arguments":{}}}'
reply=$(curl -s -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d "$call" "http://127.0.0.1:$port/mcp")
grep -q '"code":"unauthenticated"' <<< "$reply" || { echo "FAIL: tools/call reply: $reply"; exit 1; }
echo "ok: tools/call without identity is unauthenticated"
audit='^\{.*"tool": "demo\.boards\.list".*"outcome": "unauthenticated"'
docker logs "${prefix}with-bundle" 2> /dev/null | grep -qE "$audit" \
  || { echo "FAIL: no JSON audit line on stdout"; docker logs "${prefix}with-bundle"; exit 1; }
echo "ok: JSON audit line on the container's stdout"

port=$(run without-bundle -e "DEMO_BUNDLE_KEYS=smoke=ed25519:$pub")
wait_up "$port" without-bundle
expect 503 "$(status "$port" /readyz)" "AC-M3-01 /readyz without a bundle"

port=$(run foreign-key -v "$tmp/bundle:/bundle:ro" -e "DEMO_BUNDLE_KEYS=smoke=ed25519:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
wait_up "$port" foreign-key
expect 503 "$(status "$port" /readyz)" "/readyz with a bundle signed by another key"

for switch in DEMO_TEST_CONTROLS=1 "DEMO_TEST_ISSUER_KEY=ed25519:$pub"; do
  set +e
  docker run --rm -e "$switch" "$image" > "$tmp/out.log" 2>&1
  code=$?
  set -e
  [ "$code" -ne 0 ] || { echo "FAIL: production image started with $switch"; exit 1; }
  grep -q startup_refused "$tmp/out.log" || { cat "$tmp/out.log"; exit 1; }
  echo "ok: AC-M3-11 production image with ${switch%%=*} exits $code"
done

if docker run --rm "$image" python -c "import demo_connector.testctl" > /dev/null 2>&1; then
  echo "FAIL: test controls are in the production image"; exit 1
fi
echo "ok: no test controls module in the image"
echo "docker_smoke: OK"
