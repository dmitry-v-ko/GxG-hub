# Shared stand pieces for M4.1 candidates (sourced by <candidate>/run.sh, cwd = candidate dir).
# Echo = M4 echo-MCP + variant C verifier (mounted from ../../m4-contextforge/echo, not copied), on the pinned
# ContextForge image as in M4 (its Python has PyJWT). The adapter key pair is generated once into secrets/ (the private key is used only by the ToolHive webhook variant).
ECHO_IMAGE=ghcr.io/ibm/mcp-context-forge:v1.1.1@sha256:16c7a8985c1a3528ba49fcab32951e06c9a41a171fc5c41d24b085283c4556a5
M4=$(cd ../../m4-contextforge && pwd)
mkdir -p secrets/raw evidence; : > secrets/raw/echo.jsonl; : > secrets/observations.jsonl
[ -f secrets/adapter_ed25519.pem ] || python3 -I -c '
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K
from cryptography.hazmat.primitives import serialization as s
k=K.generate()
open("secrets/adapter_ed25519.pem","wb").write(k.private_bytes(s.Encoding.PEM,s.PrivateFormat.PKCS8,s.NoEncryption()))
open("secrets/adapter_ed25519.pub.pem","wb").write(k.public_key().public_bytes(s.Encoding.PEM,s.PublicFormat.SubjectPublicKeyInfo))'
start_echo() {  # $1 = docker network (host for ToolHive, a bridge for Docker MCP Gateway)
  docker rm -f m41-echo >/dev/null 2>&1 || true
  docker run -d --name m41-echo --network "$1" ${2:-} -u 0:0 -v "$M4/echo:/echo:ro" -v "$PWD/secrets/raw:/data" \
    -v "$PWD/secrets/adapter_ed25519.pub.pem:/run/gxg/pub.pem:ro" -e ECHO_ADAPTER_PUBKEY=/run/gxg/pub.pem \
    --entrypoint /app/.venv/bin/python3 "$ECHO_IMAGE" -I /echo/echo_mcp.py 9000 >/dev/null
}
redact() {  # evidence without tokens: bearer values and JWT-looking strings cut
  python3 -I - "$@" <<'P'
import re, sys
for src, dst in zip(sys.argv[1::2], sys.argv[2::2]):
    t = open(src).read()
    t = re.sub(r"(Bearer )[A-Za-z0-9._~+/=-]+", r"\1<redacted>", t)
    t = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{20,}", "<jwt-redacted>", t)
    open(dst, "w").write(t)
P
}
