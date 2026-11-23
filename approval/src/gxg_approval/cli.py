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
    parser.add_argument(
        "process",
        choices=["migrate", "api", "mcp", "sweeper", "executor", "decider", "admin", "keys", "switch", "passkeys"],
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["rotate", "remove", "release", "set", "clear", "invite"],
        help="keys: rotate|remove|release; switch: set|clear; passkeys: invite",
    )
    parser.add_argument("--kid", help="keys remove: the key to remove")
    parser.add_argument("--scope", help="switch: global | tenant:<id> | tool:<tenant>:<tool_key>")
    parser.add_argument("--reason", help="switch: why (audited)")
    parser.add_argument("--human", help="passkeys invite: oidc:<iss>:<sub> of the approver")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from . import config

    if args.process == "migrate":
        from .db import migrate

        migrate(config.secret("GXG_DB_DSN"))
    elif args.process == "sweeper":
        from .sweeper import run

        run(
            config.secret("GXG_DB_DSN"),
            interval_s=float(config.env("GXG_SWEEP_INTERVAL_S", "5")),
            retention_days=int(config.env("GXG_RETENTION_DAYS", "30")),
        )
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
    elif args.process == "decider":
        _decider()
    elif args.process == "admin":
        _admin()
    elif args.process in ("switch", "passkeys"):
        _operator(args)
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
        key = f"{service.id} {service.internal_url}"  # a new internal_url in the bundle gets a new client
        if key not in clients:
            clients[key] = httpx.Client(base_url=str(service.internal_url))  # TLS verified by default (AS-07)
        return clients[key]

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
    from prometheus_client import CollectorRegistry, start_http_server

    from .executor import RESERVE_CONFLICTS

    scrape = CollectorRegistry(auto_describe=False)
    scrape.register(RESERVE_CONFLICTS)  # the database figures are on api /metrics
    host, _, port = c.env("GXG_METRICS_LISTEN", "0.0.0.0:9101").rpartition(":")
    start_http_server(int(port), addr=host, registry=scrape)
    executor.run(threading.Event(), poll_s=float(c.env("GXG_POLL_S", "1")))


def _keys(args: argparse.Namespace) -> None:
    """rotate/remove: on the host, over the executor's key directory and api's JWKS (README «Ротация ключа»).
    release: inside the executor (its DB role), right after removing a compromised key (AS-10)."""
    from . import config as c
    from . import keys

    if args.action == "release":
        import psycopg

        from .executor import release_reserved

        with psycopg.connect(c.secret("GXG_DB_DSN"), autocommit=True) as conn:
            print(f"released {release_reserved(conn, 'keys release')} reservations")
        return
    keys_file, jwks_file = Path(c.env("GXG_EXEC_SIGNING_KEYS_FILE")), Path(c.env("GXG_EXEC_JWKS_FILE"))
    if args.action == "rotate":
        print(keys.rotate(keys_file, jwks_file))
    elif args.action == "remove" and args.kid:
        keys.remove(keys_file, jwks_file, args.kid)
    else:
        raise SystemExit("keys: rotate | remove --kid <kid> | release")


def _serve(app: object, default_listen: str) -> None:
    import uvicorn

    from . import config as c

    host, _, port = c.env("GXG_LISTEN", default_listen).rpartition(":")
    uvicorn.run(app, host=host, port=int(port))  # type: ignore[arg-type]


def _decider() -> None:
    from urllib.parse import urlsplit

    from psycopg_pool import ConnectionPool

    from . import config as c
    from .decider import Deps, create_app

    public = urlsplit(c.env("GXG_UI_PUBLIC_URL"))  # the WebAuthn RP is the ui's public origin
    pool = ConnectionPool(c.secret("GXG_DB_DSN"), kwargs={"autocommit": True})
    _serve(create_app(Deps(pool, str(public.hostname), f"{public.scheme}://{public.netloc}")), "0.0.0.0:8093")


def _admin() -> None:
    import json
    import urllib.request

    import jwt
    from psycopg_pool import ConnectionPool

    from . import config as c
    from .admin import Deps, create_app

    issuer = c.env("GXG_ADMIN_OIDC_ISSUER")
    with urllib.request.urlopen(issuer.removesuffix("/") + "/.well-known/openid-configuration", timeout=10) as r:  # noqa: S310 (configured https issuer)
        jwks_uri = json.load(r)["jwks_uri"]
    operators = frozenset(o.strip() for o in c.env("GXG_ADMIN_OPERATORS").split(",") if o.strip())
    if not operators:
        raise SystemExit("GXG_ADMIN_OPERATORS: no platform_admin configured")
    deps = Deps(
        ConnectionPool(c.secret("GXG_DB_DSN"), kwargs={"autocommit": True}),
        issuer=issuer,
        audience=c.env("GXG_ADMIN_OIDC_AUDIENCE"),
        operators=operators,
        signing_key=jwt.PyJWKClient(jwks_uri, cache_keys=True).get_signing_key_from_jwt,
    )
    _serve(create_app(deps), "0.0.0.0:8092")


def _operator(args: argparse.Namespace) -> None:
    """`switch set|clear`, `passkeys invite`: the operator's CLI over the admin API (AS-24)."""
    import httpx

    from . import config as c

    if args.process == "switch" and args.action in ("set", "clear") and args.scope and args.reason:
        path, body = "/v1/switches", {"scope": args.scope, "blocked": args.action == "set", "reason": args.reason}
    elif args.process == "passkeys" and args.action == "invite" and args.human:
        path, body = "/v1/passkey-invites", {"human": args.human}
    else:
        raise SystemExit("switch set|clear --scope <scope> --reason <text> | passkeys invite --human <oidc:…>")
    token = c.secret("GXG_OPERATOR_TOKEN")
    r = httpx.post(c.env("GXG_ADMIN_URL").removesuffix("/") + path, json=body,
                   headers={"Authorization": f"Bearer {token}"}, timeout=30)  # fmt: skip
    print(r.text)
    if r.status_code != 200:
        raise SystemExit(f"admin: HTTP {r.status_code}")
