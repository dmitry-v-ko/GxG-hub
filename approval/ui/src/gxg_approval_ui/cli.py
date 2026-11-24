"""`gxg-approval-ui`: process wiring — variables, secret files, bundle loader (variables: config.py docstring)."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gxg_connector_kit.grants import bundle_store_from_env

from .config import LOOPBACK, operators, preview_keys, require_tls

log = logging.getLogger("gxg_approval_ui")
LOGGING: dict[str, Any] = {"level": logging.INFO, "format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
POOL: dict[str, Any] = {"kwargs": {"autocommit": True}, "open": True, "min_size": 1}  # opens in the background


class NoQuery(logging.Filter):
    """SEC-17: uvicorn's access log line without the query string (`/auth/callback?code=…&state=…` → path only)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):  # client, method, path, http, code
            record.args = (args[0], args[1], args[2].split("?", 1)[0], args[3], args[4])
        return True


def env(name: str, environ: Mapping[str, str] = os.environ) -> str:
    value = environ.get(name)
    if not value:
        raise SystemExit(f"missing configuration: {name}")
    return value


def secret(name: str, environ: Mapping[str, str] = os.environ) -> str:
    return Path(env(f"{name}_FILE", environ)).read_bytes().decode().strip()


def main() -> None:
    import httpx2
    import uvicorn
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from psycopg_pool import ConnectionPool

    from gxg_connector_kit import _jwt

    from .app import Deps, create_app
    from .tgadmin import DEFAULT_AUDIENCE, TelegramAdmin

    logging.basicConfig(**LOGGING)
    logging.getLogger("uvicorn.access").addFilter(NoQuery())
    public_url, issuer = env("GXG_UI_PUBLIC_URL"), env("GXG_OIDC_ISSUER")
    require_tls(public_url, "GXG_UI_PUBLIC_URL")
    require_tls(issuer, "GXG_OIDC_ISSUER")
    host, _, port = os.environ.get("GXG_LISTEN", "0.0.0.0:8443").rpartition(":")
    tls = {}
    if os.environ.get("GXG_UI_TLS_CERT_FILE"):
        tls = {"ssl_certfile": env("GXG_UI_TLS_CERT_FILE"), "ssl_keyfile": env("GXG_UI_TLS_KEY_FILE")}
    elif host not in LOOPBACK:
        raise SystemExit("GXG_UI_TLS_CERT_FILE/GXG_UI_TLS_KEY_FILE are required on a non-loopback listener")
    telegram_admin = None
    if os.environ.get("GXG_TELEGRAM_ADMIN_URL"):  # the operator wizard; without it the page says "not configured"
        telegram_admin = TelegramAdmin(
            httpx2.Client(base_url=env("GXG_TELEGRAM_ADMIN_URL")),  # private network only
            Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(secret("GXG_UI_SVC_KEY"))),
            os.environ.get("GXG_TELEGRAM_ADMIN_AUDIENCE") or DEFAULT_AUDIENCE,
        )
    deps = Deps(
        pool=ConnectionPool(secret("GXG_DB_DSN"), **POOL),
        bundles=bundle_store_from_env(os.environ),
        preview_keys=preview_keys(Path(env("GXG_PREVIEW_KEYS_FILE"))),
        session_key=secret("GXG_UI_SESSION_KEY"),
        public_url=public_url,
        oidc_issuer=issuer,
        oidc_client_id=env("GXG_OIDC_CLIENT_ID"),
        oidc_client_secret=secret("GXG_OIDC_CLIENT_SECRET"),
        decider=httpx2.Client(base_url=env("GXG_DECIDER_URL")),  # internal network only (AS-30)
        operators=operators(os.environ.get("GXG_UI_OPERATORS", "")),
        telegram_admin=telegram_admin,
    )
    uvicorn.run(create_app(deps), host=host, port=int(port), **tls)  # type: ignore[arg-type]
