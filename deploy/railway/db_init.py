"""One-shot bootstrap of the Approval database on Railway (AS-27): roles of `approval/deploy/roles.sql`, LOGIN and a
password per role, ownership of the database for `gxg_owner`. Idempotent. Runs inside the private network (the database
has no public address), as the `db-init` job; `railway_deploy.py --finalize` removes that job and its secrets afterwards.

Environment (the entrypoint turns the GXG_SECRET__ variables into files): `GXG_SUPERUSER_DSN_FILE`,
`GXG_ROLE_PASSWORDS_FILE` (JSON {"gxg_api": "<password>", ...}), `GXG_ROLES_SQL` (default /opt/gxg/roles.sql).
Nothing is printed except role names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
from psycopg import sql


def main() -> None:
    dsn = Path(os.environ["GXG_SUPERUSER_DSN_FILE"]).read_text("utf-8").strip()
    passwords: dict[str, str] = json.loads(Path(os.environ["GXG_ROLE_PASSWORDS_FILE"]).read_text("utf-8"))
    roles_sql = Path(os.environ.get("GXG_ROLES_SQL", "/opt/gxg/roles.sql")).read_text("utf-8")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(roles_sql.encode())
        for role, password in sorted(passwords.items()):
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
            print(f"db-init: role {role} ready")
        db = conn.execute("SELECT current_database()").fetchone()
        assert db is not None
        conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO gxg_owner").format(sql.Identifier(db[0])))
        conn.execute("ALTER SCHEMA public OWNER TO gxg_owner")
        print("db-init: done")


if __name__ == "__main__":
    main()
