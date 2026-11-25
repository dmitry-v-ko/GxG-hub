"""One-off setup of telegram-db (M13). Runs inside the gxg-telegram-service image (python + psycopg from /app/.venv, the
SQL in /app/db), as the job `telegram-db-init`; railway_deploy.py seeds this file into the variable GXG_TGDB_INIT_PY and
the start command execs it. Idempotent: migrations only when the archive schema is missing, roles only when missing,
the role passwords are always set, the Connection row is inserted once. No value is printed.

Environment: GXG_TGDB_SUPERUSER_DSN, GXG_TGDB_ROLE_PASSWORDS (JSON role -> password),
GXG_TGDB_MODE (`check`: report only, SELECT; default: set up), GXG_TGDB_CONNECTION (default tg-pilot),
GXG_TGDB_CREDENTIAL_REF, GXG_TGDB_DIR (default /app/db), GXG_TGDB_PROBE, GXG_TGDB_CA (pilot CA certificate).
"""

import json
import os
import re
import socket
import ssl
from pathlib import Path

import psycopg
from psycopg import errors, sql

DB = Path(os.environ.get("GXG_TGDB_DIR", "/app/db"))
MIGRATIONS = [
    "00_capability_hub_connections.sql",
    "20261105010000_telegram_archive.sql",
    "20261106010000_telegram_archive_semantic.sql",
    "10_telegram_service.sql",
]
ROLE_FILES = {"gxg_telegram_service": "service_role.sql", "gxg_telegram_reader": "reader_role.sql"}


def statements(text: str) -> list[str]:
    """Split SQL at `;` outside quotes, dollar-quoted bodies and comments (enough for the service's own files)."""
    out, buf, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "'":
            j = i + 1
            while j < n and not (text[j] == "'" and text[j + 1 : j + 2] != "'"):
                j += 2 if text[j] == "'" else 1
            buf.append(text[i : j + 1])
            i = j + 1
            continue
        if c == "$":
            tag = re.match(r"\$[A-Za-z_]*\$", text[i:])
            if tag:
                j = text.find(tag.group(0), i + len(tag.group(0)))
                j = n if j < 0 else j + len(tag.group(0))
                buf.append(text[i:j])
                i = j
                continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    return out + ([tail] if tail else [])


def reapply(conn: psycopg.Connection, name: str) -> None:
    """Apply a service SQL file statement by statement; what already exists (a role, a function without OR REPLACE) is
    skipped, anything else fails. The files are then re-runnable: a newer head adds its functions and grants."""
    done = skipped = 0
    for stmt in statements((DB / name).read_text("utf-8")):
        try:
            conn.execute(stmt)
            done += 1
        except (errors.DuplicateObject, errors.DuplicateFunction, errors.DuplicateTable, errors.DuplicateSchema):
            skipped += 1
    print(f"{name}: {done} statements applied, {skipped} already there", flush=True)


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
        for proc in ("telegram_service.set_scope(text, jsonb)",):
            row = conn.execute("SELECT to_regprocedure(%s)", (proc,)).fetchone()
            print(f"function {proc}: {'present' if row and row[0] else 'MISSING'}", flush=True)
            if row and row[0]:
                for role in ROLE_FILES:
                    ok = conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, proc)).fetchone()
                    print(f"  EXECUTE for {role}: {bool(ok and ok[0])}", flush=True)
        try:  # the chats of the saved scope (the archive keeps only these): stable chat_ref ids, no peer ids, no texts
            for chat_ref, state in conn.execute(
                "SELECT chat_ref, state FROM telegram_archive.chats ORDER BY connection_id, chat_ref"
            ):
                print(f"scope chat {chat_ref}: state={state}", flush=True)
        except psycopg.Error as exc:
            print(f"scope report unavailable ({type(exc).__name__})", flush=True)
        if conn.execute("SELECT to_regclass('capability_hub.connections')").fetchone()[0]:
            for cid, enabled in conn.execute("SELECT id, enabled FROM capability_hub.connections ORDER BY 1"):
                print(f"connection {cid}: enabled={enabled}", flush=True)
        else:
            print("capability_hub.connections: MISSING", flush=True)


def probe(targets: str) -> None:
    """TCP connect and a TLS hello (nothing else is sent) to `host:port,...`: TLS or not, and is the certificate
    signed by the pilot CA (GXG_TGDB_CA, a public certificate) for that name?"""
    ca = os.environ.get("GXG_TGDB_CA", "")
    for target in filter(None, (t.strip() for t in targets.split(","))):
        host, _, port = target.rpartition(":")
        try:
            verdict = "no TLS"
            for trusted in (True, False):
                ctx = ssl.create_default_context(cadata=ca) if trusted and ca else ssl.create_default_context()
                if not trusted:
                    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                elif not ca:
                    continue
                try:
                    with socket.create_connection((host, int(port)), timeout=5) as raw:
                        with ctx.wrap_socket(raw, server_hostname=host) as tls:
                            verdict = f"TLS {tls.version()}, " + (
                                "signed by the pilot CA" if trusted else "not trusted"
                            )
                    break
                except ssl.SSLCertVerificationError:
                    continue
                except (ssl.SSLError, OSError):
                    break
            print(f"listen {target}: open, {verdict}", flush=True)
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
            reapply(conn, "10_telegram_service.sql")  # the service's own objects: a newer head may add some
        for role, file in ROLE_FILES.items():
            reapply(conn, file)  # creates the role the first time, later adds what a newer head brought
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
    probe(os.environ.get("GXG_TGDB_PROBE", ""))
    check(os.environ["GXG_TGDB_SUPERUSER_DSN"])  # the same read-only report after the setup


if __name__ == "__main__":  # `python file` and `python -c exec(...)` both run it; an import (tests) does not
    main()
