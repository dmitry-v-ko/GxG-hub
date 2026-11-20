#!/usr/bin/env bash
# Generates the adapter Ed25519 key pair into secrets/ (gitignored). Private key -> ContextForge only.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p secrets
[ -e secrets/adapter_ed25519.pem ] && { echo "keys exist"; exit 0; }
IMG=$(grep -E '^CONTEXTFORGE_IMAGE=' .env | cut -d= -f2-)
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/secrets:/out" --entrypoint /app/.venv/bin/python "$IMG" -c '
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as s
k = Ed25519PrivateKey.generate()
open("/out/adapter_ed25519.pem","wb").write(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))
open("/out/adapter_ed25519.pub.pem","wb").write(k.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo))
'
chmod 644 secrets/adapter_ed25519.pem secrets/adapter_ed25519.pub.pem  # container uid must read it; local stand only
