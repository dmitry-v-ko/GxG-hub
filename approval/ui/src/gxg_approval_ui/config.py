"""Configuration checks of `gxg-approval-ui` (the loader is in cli.py) (AS-28: only the ui's own secrets, as files).

| Variable | Secret |
| --- | --- |
| `GXG_DB_DSN_FILE` (role `gxg_ui`), `GXG_UI_SESSION_KEY_FILE`, `GXG_OIDC_CLIENT_SECRET_FILE` | yes |
| `GXG_PREVIEW_KEYS_FILE`: `{"<tenant>": "<base64 X25519 private key>"}` (preview only) | yes |
| `GXG_UI_TLS_CERT_FILE`, `GXG_UI_TLS_KEY_FILE` (key: secret); not required on a loopback listener only | key |
| `GXG_UI_PUBLIC_URL`, `GXG_OIDC_ISSUER`, `GXG_OIDC_CLIENT_ID`, `GXG_LISTEN` (default `0.0.0.0:8443`) | no |
| `GXG_DECIDER_URL`: the decider's internal URL (AS-30) | no |
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
