"""One-off setup of telegram-db (M13). Runs inside the gxg-telegram-service image (python + psycopg from /app/.venv, the
SQL in /app/db), as the job `telegram-db-init`; railway_deploy.py seeds this file into the variable GXG_TGDB_INIT_PY and
the start command execs it. Idempotent: migrations only when the archive schema is missing, roles only when missing,
the role passwords are always set, the Connection row is inserted once. No value is printed.

Environment: GXG_TGDB_SUPERUSER_DSN, GXG_TGDB_ROLE_PASSWORDS (JSON role -> password), GXG_TGDB_CONNECTION (default tg-pilot),
GXG_TGDB_CREDENTIAL_REF, GXG_TGDB_DIR (default /app/db).
"""

import json
import os
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


def main() -> None:
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
