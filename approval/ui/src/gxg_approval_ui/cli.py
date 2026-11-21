"""`gxg-approval-ui`: process wiring (configuration: config.py)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .config import LOOPBACK, bundle_store, env, preview_keys, require_tls, secret


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
