#!/usr/bin/env bash
# Reproduces the M4 spike end to end (~10 min). Needs: docker + compose, python3 with `cryptography`, access to ghcr.io and docker.io.
# Raw output (may hold test secrets) -> secrets/ (git-ignored); redacted evidence -> evidence/.
set -euo pipefail
cd "$(dirname "$0")"
./gen_env.sh
mkdir -p secrets/raw evidence
[ -f secrets/adapter_ed25519.pem ] || python3 -I -c '
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K
from cryptography.hazmat.primitives import serialization as s
k=K.generate()
open("secrets/adapter_ed25519.pem","wb").write(k.private_bytes(s.Encoding.PEM,s.PrivateFormat.PKCS8,s.NoEncryption()))
open("secrets/adapter_ed25519.pub.pem","wb").write(k.public_key().public_bytes(s.Encoding.PEM,s.PublicFormat.SubjectPublicKeyInfo))'
chmod 644 secrets/adapter_ed25519.pem   # read by the non-root ContextForge user inside the container
A="-f compose.yaml"; C="$A -f compose.variant-c.yaml"; OLD="$C -f compose.upgrade-from.yaml"
wait_up() { for _ in $(seq 1 60); do curl -sf http://127.0.0.1:4444/health >/dev/null && return; sleep 3; done; echo "ContextForge not healthy"; exit 1; }
fresh() { docker compose $C down -v >/dev/null 2>&1 || true; : > secrets/raw/echo.jsonl; : > secrets/observations.jsonl; }
s() { python3 -I spike.py "$@"; }

echo "== variant A: ContextForge signed identity propagation (HMAC) =="
fresh; docker compose $A up -d; wait_up
s setup >/dev/null; s calls >/dev/null; s attacks >/dev/null; s admin_call >/dev/null; s replay > secrets/replay-a.out
python3 -I verify_hmac.py | tee evidence/variant-a.verify_hmac.json; cp secrets/replay-a.out evidence/variant-a.replay.log; python3 -I redact.py a

echo "== variant C: our tool_pre_invoke plugin (JWS Ed25519) =="
fresh; docker compose $A up -d; wait_up; s setup >/dev/null; s plugin_config
docker compose $C up -d contextforge; wait_up
s calls >/dev/null; s attacks >/dev/null; s admin_call >/dev/null; s replay > secrets/replay-c.out
cp secrets/raw/echo.jsonl secrets/echo.variant-c.jsonl   # raw (signed) log, for mutation checks
python3 -I verify_jws.py | tee evidence/variant-c.verify_jws.jsonl; cp secrets/replay-c.out evidence/variant-c.replay.log; python3 -I redact.py c
python3 -I mutate_verify_jws.py | tee evidence/variant-c.mutations.log

echo "== criterion 5: upgrade v1.1.0 -> pinned v1.1.1 with the plugin, same PostgreSQL =="
fresh; docker compose $OLD up -d; wait_up; s setup >/dev/null; s plugin_config
docker compose $OLD restart contextforge; wait_up; s calls >/dev/null
docker compose $C up -d contextforge; wait_up; s calls >/dev/null; s admin_call >/dev/null
python3 -I verify_jws.py | tee evidence/variant-c-upgrade.verify_jws.jsonl; python3 -I redact.py c-upgrade
docker compose $C logs contextforge | grep -o 'Running upgrade [^,]*' > evidence/variant-c-upgrade.migrations.log
s visibility

echo "== misconfiguration: header passthrough on for every identity header (variant C) =="
: > secrets/raw/echo.jsonl; : > secrets/observations.jsonl
docker compose $C -f compose.passthrough-misconfig.yaml up -d contextforge; wait_up
PASSTHROUGH_LABEL=misconfig-all-identity-headers s attacks >/dev/null; python3 -I redact.py c-passthrough-misconfig
echo "done; stop with: docker compose $C down -v"
