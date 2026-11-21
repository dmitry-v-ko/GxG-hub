"""Per-process configuration from the environment (AS-28: each process reads only its own secrets).

Secrets are never values of environment variables: a variable ending in `_FILE` names a mounted secret
file (Docker/Kubernetes secrets). Everything else is public configuration.

| Variable | Processes | Secret |
| --- | --- | --- |
| `GXG_DB_DSN_FILE` | migrate (owner), api, sweeper — each its own role | yes |
| `GXG_SVC_KEY_FILE` | mcp (service JWT to `api`) | yes |
| `GXG_PAYLOAD_PUBLIC_KEYS_FILE` | api (encrypt only) | no |
| `GXG_EXEC_JWKS_FILE` | api (AS-09) | no |
| `GXG_BUNDLE_DIR`, `GXG_BUNDLE_KEYS`, `GXG_BUNDLE_STATE_FILE` | api | no |
| `GXG_SERVICE_ID`, `GXG_API_URL`, `GXG_API_AUDIENCE`, `GXG_LISTEN`, `GXG_SWEEP_INTERVAL_S` | as named | no |
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import _jwt
from gxg_connector_kit.grants import BundleStore, bundle_store_from_env

log = logging.getLogger("gxg_approval")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing configuration: {name}")
    return value


def secret(name: str) -> str:
    """Content of the secret file named by `<name>_FILE`."""
    return Path(env(f"{name}_FILE")).read_text("utf-8").strip()


def signing_key(name: str) -> Ed25519PrivateKey:
    """Ed25519 seed, base64url without padding (the format of the M2 bundle signing key)."""
    return Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(secret(name)))


def bundle_store(environ: Mapping[str, str] = os.environ) -> BundleStore:
    return bundle_store_from_env(environ)
