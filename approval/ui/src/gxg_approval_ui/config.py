"""Configuration checks of `gxg-approval-ui` (the loader is in cli.py) (AS-28: only the ui's own secrets, as files).

| Variable | Secret |
| --- | --- |
| `GXG_DB_DSN_FILE` (role `gxg_ui`), `GXG_UI_SESSION_KEY_FILE`, `GXG_OIDC_CLIENT_SECRET_FILE` | yes |
| `GXG_PREVIEW_KEYS_FILE`: `{"<tenant>": "<base64 X25519 private key>"}` (preview only) | yes |
| `GXG_UI_TLS_CERT_FILE`, `GXG_UI_TLS_KEY_FILE` (key: secret); not required on a loopback listener only | key |
| `GXG_UI_PUBLIC_URL`, `GXG_OIDC_ISSUER`, `GXG_OIDC_CLIENT_ID`, `GXG_LISTEN` (default `0.0.0.0:8443`) | no |
| `GXG_DECIDER_URL`: the decider's internal URL (AS-30) | no |
| `GXG_UI_OPERATORS`: confirmed emails of the operators (comma-separated; empty = nobody) | no |
| `GXG_TELEGRAM_ADMIN_URL` (private network, `:8082`) with `GXG_UI_SVC_KEY_FILE` (service JWT key `approval-ui`) | key |
| `GXG_TELEGRAM_ADMIN_AUDIENCE`: the Telegram service ID in the JWT `aud` (default `telegram`) | no |
| `GXG_BUNDLE_DIR`, `GXG_BUNDLE_KEYS`, `GXG_BUNDLE_STATE_FILE` | no |
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

from nacl.public import PrivateKey, SealedBox

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def require_tls(url: str, what: str) -> None:
    """HTTPS everywhere outside a loopback test (AS-15 Secure cookies, AS-29 inbound HTTPS only)."""
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in LOOPBACK:
        raise SystemExit(f"{what} must be https: {url}")


def preview_keys(path: Path) -> dict[str, SealedBox[PrivateKey]]:
    doc = json.loads(path.read_bytes())
    return {tenant: SealedBox(PrivateKey(base64.b64decode(key, validate=True))) for tenant, key in doc.items()}


def operators(raw: str) -> frozenset[str]:
    """`GXG_UI_OPERATORS`: comma-separated emails, compared in lower case."""
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
