"""`gxg-approval <process>`: one code base, one process per command (approval-service-v1.md «Процессы»)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gxg-approval")
    parser.add_argument("process", choices=["migrate", "api", "mcp", "sweeper"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from . import config

    if args.process == "migrate":
        from .db import migrate

        migrate(config.secret("GXG_DB_DSN"))
    elif args.process == "sweeper":
        from .sweeper import run

        run(config.secret("GXG_DB_DSN"), interval_s=float(config.env("GXG_SWEEP_INTERVAL_S", "5")))
    elif args.process == "api":
        import uvicorn
        from psycopg_pool import ConnectionPool

        from .api import Deps, create_app
        from .crypto import Encryptor

        pool = ConnectionPool(config.secret("GXG_DB_DSN"), kwargs={"autocommit": True})
        deps = Deps(
            config.env("GXG_SERVICE_ID", "approval-api"),
            pool,
            config.bundle_store(),
            Encryptor.from_file(Path(config.env("GXG_PAYLOAD_PUBLIC_KEYS_FILE"))),
            json.loads(Path(config.env("GXG_EXEC_JWKS_FILE")).read_text("utf-8")),
        )
        host, _, port = config.env("GXG_LISTEN", "0.0.0.0:8090").rpartition(":")
        uvicorn.run(create_app(deps), host=host, port=int(port))
    else:
        raise SystemExit(
            "mcp: no production IdentityVerifier yet (ADR-0001 variant C lands with M5); refusing to start (SEC-03)"
        )
