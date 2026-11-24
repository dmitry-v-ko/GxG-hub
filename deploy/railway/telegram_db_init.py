"""One-off setup of telegram-db (M13). Runs inside the gxg-telegram-service image (python + psycopg from /app/.venv, the
SQL in /app/db), as the job `telegram-db-init`; railway_deploy.py seeds this file into the variable GXG_TGDB_INIT_PY and
the start command execs it. Idempotent: migrations only when the archive schema is missing, roles only when missing,
the role passwords are always set, the Connection row is inserted once. No value is printed.

Environment: GXG_TGDB_SUPERUSER_DSN, GXG_TGDB_ROLE_PASSWORDS (JSON role -> password),
GXG_TGDB_MODE (`check`: report only, SELECT; default: set up), GXG_TGDB_CONNECTION (default tg-pilot),
GXG_TGDB_CREDENTIAL_REF, GXG_TGDB_DIR (default /app/db).
"""

import json
import os
import socket
from pathlib import Path

import psycopg
from psycopg import sql

DB = Path(os.environ.get("GXG_TGDB_DIR", "/app/db"))
MIGRATIONS = [
    "00_capability_hub_connections.sql",
    "20261105010000_telegram_archive.sql",
    "20261106010000_telegram_archive_semantic.sql",
    "10_telegram_service.sql",
]
ROLE_FILES = {"gxg_telegram_service": "service_role.sql", "gxg_telegram_reader": "reader_role.sql"}


def check(dsn: str) -> None:
    """Read-only report (GXG_TGDB_MODE=check): which roles, schemas, tables and Connection rows exist. SELECT only."""
    with psycopg.connect(dsn, autocommit=True, options="-c default_transaction_read_only=on") as conn:
        for role in ROLE_FILES:
            row = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            print(
                f"role {role}: {'login' if row and row[0] else 'present, no login' if row else 'MISSING'}", flush=True
            )
        for schema in ("capability_hub", "telegram_archive"):
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY 1", (schema,)
                )
            ]
            print(f"schema {schema}: {len(tables)} tables {tables}", flush=True)
        if conn.execute("SELECT to_regclass('capability_hub.connections')").fetchone()[0]:
            for cid, enabled in conn.execute("SELECT id, enabled FROM capability_hub.connections ORDER BY 1"):
                print(f"connection {cid}: enabled={enabled}", flush=True)
        else:
            print("capability_hub.connections: MISSING", flush=True)


def probe(targets: str) -> None:
    """TCP connect only (no bytes sent) to `host:port,...`: does the service listen on the private network?"""
    for target in filter(None, (t.strip() for t in targets.split(","))):
        host, _, port = target.rpartition(":")
        try:
            socket.create_connection((host, int(port)), timeout=5).close()
            print(f"listen {target}: open", flush=True)
        except OSError as exc:
            print(f"listen {target}: closed ({type(exc).__name__})", flush=True)


def main() -> None:
    if os.environ.get("GXG_TGDB_MODE") == "check":
        probe(os.environ.get("GXG_TGDB_PROBE", ""))
        return check(os.environ["GXG_TGDB_SUPERUSER_DSN"])
    if not DB.is_dir():  # name the real layout instead of a bare FileNotFoundError
        found = sorted(str(p) for p in Path("/app").rglob("*.sql") if ".venv" not in p.parts)
        raise SystemExit(f"{DB} is not in the image; .sql files under /app: {found}")
    passwords = json.loads(os.environ["GXG_TGDB_ROLE_PASSWORDS"])
    with psycopg.connect(os.environ["GXG_TGDB_SUPERUSER_DSN"], autocommit=True) as conn:
        if conn.execute("SELECT to_regclass('telegram_archive.chats')").fetchone()[0] is None:
            for name in MIGRATIONS:
                conn.execute((DB / name).read_text("utf-8"))
                print(f"applied {name}", flush=True)
        else:
            print("archive schema exists, migrations skipped", flush=True)
        for role, file in ROLE_FILES.items():
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
                conn.execute((DB / file).read_text("utf-8"))
                print(f"created role {role}", flush=True)
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(passwords[role]))
            )
            print(f"password set for {role}", flush=True)
        conn.execute(
            "INSERT INTO capability_hub.connections (id, tenancy_mode, credential_ref) VALUES (%s, 'shared', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                os.environ.get("GXG_TGDB_CONNECTION", "tg-pilot"),
                os.environ.get("GXG_TGDB_CREDENTIAL_REF", "secret://t-pilot/telegram/pilot"),
            ),
        )
        print("connection row ensured", flush=True)


main()
