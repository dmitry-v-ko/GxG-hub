"""`gxg-approval-ui`: configuration from the environment (AS-28: only the ui's own secrets, as files).

| Variable | Secret |
| --- | --- |
| `GXG_DB_DSN_FILE` (role `gxg_ui`), `GXG_UI_SESSION_KEY_FILE`, `GXG_OIDC_CLIENT_SECRET_FILE` | yes |
| `GXG_PREVIEW_KEYS_FILE`: `{"<tenant>": "<base64 X25519 private key>"}` (preview only) | yes |
| `GXG_UI_TLS_CERT_FILE`, `GXG_UI_TLS_KEY_FILE` (key: secret); not required on a loopback listener only | key |
| `GXG_UI_PUBLIC_URL`, `GXG_OIDC_ISSUER`, `GXG_OIDC_CLIENT_ID`, `GXG_LISTEN` (default `0.0.0.0:8443`) | no |
| `GXG_BUNDLE_DIR`, `GXG_BUNDLE_KEYS`, `GXG_BUNDLE_STATE_FILE` | no |
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from nacl.public import PrivateKey, SealedBox

from gxg_connector_kit import _jwt
from gxg_connector_kit.grants import BundleError, BundleStore

log = logging.getLogger("gxg_approval_ui")
BUNDLE_RELOAD_S = 60  # GR-13
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def env(name: str, environ: Mapping[str, str] = os.environ) -> str:
    value = environ.get(name)
    if not value:
        raise SystemExit(f"missing configuration: {name}")
    return value


def secret(name: str, environ: Mapping[str, str] = os.environ) -> str:
    return Path(env(f"{name}_FILE", environ)).read_text("utf-8").strip()


def require_tls(url: str, what: str) -> None:
    """HTTPS everywhere outside a loopback test (AS-15 Secure cookies, AS-29 inbound HTTPS only)."""
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in LOOPBACK:
        raise SystemExit(f"{what} must be https: {url}")


def preview_keys(path: Path) -> dict[str, SealedBox]:
    doc = json.loads(path.read_text("utf-8"))
    return {tenant: SealedBox(PrivateKey(base64.b64decode(key, validate=True))) for tenant, key in doc.items()}


def bundle_store(environ: Mapping[str, str] = os.environ) -> BundleStore:
    """The signed grants bundle, reloaded every minute (same loader as approval/config.py; AS-29: own tree)."""
    keys = {kid: _jwt.parse_public_key(k) for kid, k in json.loads(env("GXG_BUNDLE_KEYS", environ)).items()}
    store = BundleStore(keys, state_file=Path(env("GXG_BUNDLE_STATE_FILE", environ)))
    directory = Path(env("GXG_BUNDLE_DIR", environ))

    def reload() -> None:
        try:
            store.load(
                (directory / "grants-bundle.json").read_bytes(), (directory / "grants-bundle.json.sig").read_bytes()
            )
        except (OSError, BundleError) as exc:  # keep the previous bundle; /readyz reports it
            log.warning("bundle not loaded: %s", exc)

    def loop() -> None:
        while not threading.Event().wait(BUNDLE_RELOAD_S):
            reload()

    reload()
    threading.Thread(target=loop, name="bundle-reload", daemon=True).start()
    return store


def main() -> None:
    import uvicorn
    from psycopg_pool import ConnectionPool

    from .app import Deps, create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    public_url, issuer = env("GXG_UI_PUBLIC_URL"), env("GXG_OIDC_ISSUER")
    require_tls(public_url, "GXG_UI_PUBLIC_URL")
    require_tls(issuer, "GXG_OIDC_ISSUER")
    host, _, port = os.environ.get("GXG_LISTEN", "0.0.0.0:8443").rpartition(":")
    tls = {}
    if os.environ.get("GXG_UI_TLS_CERT_FILE"):
        tls = {"ssl_certfile": env("GXG_UI_TLS_CERT_FILE"), "ssl_keyfile": env("GXG_UI_TLS_KEY_FILE")}
    elif host not in LOOPBACK:
        raise SystemExit("GXG_UI_TLS_CERT_FILE/GXG_UI_TLS_KEY_FILE are required on a non-loopback listener")
    deps = Deps(
        pool=ConnectionPool(secret("GXG_DB_DSN"), kwargs={"autocommit": True}, open=True, min_size=1),
        bundles=bundle_store(),
        preview_keys=preview_keys(Path(env("GXG_PREVIEW_KEYS_FILE"))),
        session_key=secret("GXG_UI_SESSION_KEY"),
        public_url=public_url,
        oidc_issuer=issuer,
        oidc_client_id=env("GXG_OIDC_CLIENT_ID"),
        oidc_client_secret=secret("GXG_OIDC_CLIENT_SECRET"),
    )
    uvicorn.run(create_app(deps), host=host, port=int(port), **tls)  # type: ignore[arg-type]
