"""`gxg-approval-ui`: process wiring — variables, secret files, bundle loader (variables: config.py docstring)."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gxg_connector_kit.grants import bundle_store_from_env

from .config import LOOPBACK, preview_keys, require_tls

log = logging.getLogger("gxg_approval_ui")
LOGGING: dict[str, Any] = {"level": logging.INFO, "format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
POOL: dict[str, Any] = {"kwargs": {"autocommit": True}, "open": True, "min_size": 1}  # opens in the background


def env(name: str, environ: Mapping[str, str] = os.environ) -> str:
    value = environ.get(name)
    if not value:
        raise SystemExit(f"missing configuration: {name}")
    return value


def secret(name: str, environ: Mapping[str, str] = os.environ) -> str:
    return Path(env(f"{name}_FILE", environ)).read_bytes().decode().strip()


def main() -> None:
    import uvicorn
    from psycopg_pool import ConnectionPool

    from .app import Deps, create_app

    logging.basicConfig(**LOGGING)
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
        pool=ConnectionPool(secret("GXG_DB_DSN"), **POOL),
        bundles=bundle_store_from_env(os.environ),
        preview_keys=preview_keys(Path(env("GXG_PREVIEW_KEYS_FILE"))),
        session_key=secret("GXG_UI_SESSION_KEY"),
        public_url=public_url,
        oidc_issuer=issuer,
        oidc_client_id=env("GXG_OIDC_CLIENT_ID"),
        oidc_client_secret=secret("GXG_OIDC_CLIENT_SECRET"),
    )
    uvicorn.run(create_app(deps), host=host, port=int(port), **tls)  # type: ignore[arg-type]
