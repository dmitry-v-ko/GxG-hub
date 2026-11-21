"""`gxg-approval <process>`: one code base, one process per command (approval-service-v1.md «Процессы»)."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gxg-approval")
    parser.add_argument("process", choices=["migrate", "api", "mcp", "sweeper", "executor", "keys"])
    parser.add_argument("action", nargs="?", choices=["rotate", "remove"], help="keys: rotate | remove")
    parser.add_argument("--kid", help="keys remove: the key to remove")
    parser.add_argument("--compromised", action="store_true", help="keys remove: also release every reservation")
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
            config.JsonFile(Path(config.env("GXG_EXEC_JWKS_FILE"))),
        )
        host, _, port = config.env("GXG_LISTEN", "0.0.0.0:8090").rpartition(":")
        uvicorn.run(create_app(deps), host=host, port=int(port))
    elif args.process == "executor":
        _executor()
    elif args.process == "keys":
        _keys(args)
    else:
        raise SystemExit(
            "mcp: no production IdentityVerifier yet (ADR-0001 variant C lands with M5); refusing to start (SEC-03)"
        )


def _executor() -> None:
    import httpx
    import psycopg

    from gxg_connector_kit.svc_jwt import Service

    from . import config as c
    from .crypto import Decryptor
    from .executor import Executor, SigningKeys

    keys = SigningKeys(c.JsonFile(Path(c.env("GXG_EXEC_SIGNING_KEYS_FILE"))))
    keys.current()  # fail at start without an active signing key (readyz of the spec, «ключ подписи»)
    store = c.bundle_store()
    if not store.readiness().ready:
        raise SystemExit("executor: no valid bundle (GR-12)")
    clients: dict[str, httpx.Client] = {}

    def http(service: Service) -> httpx.Client:
        if service.id not in clients:
            clients[service.id] = httpx.Client(base_url=str(service.internal_url))  # TLS verified by default (AS-07)
        return clients[service.id]

    executor = Executor(
        psycopg.connect(c.secret("GXG_DB_DSN"), autocommit=True),
        instance=f"{c.env('GXG_SERVICE_ID', 'executor')}:{socket.gethostname()}:{os.getpid()}",
        service_id=c.env("GXG_SERVICE_ID", "executor"),
        store=store,
        decryptor=Decryptor("args", c.JsonFile(Path(c.env("GXG_PAYLOAD_PRIVATE_KEYS_FILE")))()),
        keys=keys,
        svc_key=c.signing_key("GXG_SVC_KEY"),
        http=http,
    )
    executor.run(threading.Event(), poll_s=float(c.env("GXG_POLL_S", "1")))


def _keys(args: argparse.Namespace) -> None:
    from . import config as c
    from . import keys

    keys_file, jwks_file = Path(c.env("GXG_EXEC_SIGNING_KEYS_FILE")), Path(c.env("GXG_EXEC_JWKS_FILE"))
    if args.action == "rotate":
        print(keys.rotate(keys_file, jwks_file))
    elif args.action == "remove" and args.kid:
        keys.remove(keys_file, jwks_file, args.kid)
        if args.compromised:  # AS-10: as the executor role (its own DB secret)
            import psycopg

            with psycopg.connect(c.secret("GXG_DB_DSN"), autocommit=True) as conn:
                row = conn.execute("SELECT gxg_release_reserved('keys:remove --compromised')").fetchone()
            print(f"released {row[0] if row else 0} reservations")
    else:
        raise SystemExit("keys: rotate | remove --kid <kid> [--compromised]")
