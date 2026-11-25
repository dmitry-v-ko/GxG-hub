"""One-shot bootstrap of the Approval database on Railway (AS-27): roles of `approval/deploy/roles.sql`, LOGIN and a
password per role, ownership of the database for `gxg_owner`. Idempotent. Runs inside the private network (the database
has no public address), as the `db-init` job; `railway_deploy.py --finalize` removes the job and its secrets afterwards.

Environment (the entrypoint turns GXG_SECRET__ variables into files): `GXG_SUPERUSER_DSN_FILE`,
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
        if db is None:
            raise SystemExit("db-init: no current database")
        conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO gxg_owner").format(sql.Identifier(db[0])))
        conn.execute("ALTER SCHEMA public OWNER TO gxg_owner")
        print("db-init: done")
        report(conn)


def report(conn: psycopg.Connection) -> None:
    """Read-only facts for the operator: who has a registered passkey (principals and counts, no key material)."""
    try:
        rows = conn.execute(
            "SELECT human_principal, count(*), count(*) FILTER (WHERE enrolled_by IS NOT NULL) "
            "FROM webauthn_credentials GROUP BY 1 ORDER BY 1"
        ).fetchall()
    except psycopg.Error as exc:
        print(f"db-init: passkey report unavailable ({type(exc).__name__})")
        return
    for principal, total, enrolled in rows:
        print(f"passkey {principal}: credentials={total} enrolled_by_operator={enrolled}")
    print(f"passkey principals: {len(rows)}")
    try:  # AS-30a: which pending intents are decided by one button, and who may call which decision function
        for intent_id, status, step_up in conn.execute(
            "SELECT intent_id, status, step_up_required FROM intents ORDER BY created_at DESC LIMIT 10"
        ):
            print(f"intent {intent_id}: status={status} step_up_required={step_up}")
        for intent_id, attempt, state, error_code, op_id, completed in conn.execute(
            "SELECT intent_id, attempt, state, error_code, provider_operation_id, completed_at FROM executions "
            "ORDER BY completed_at NULLS LAST, attempt LIMIT 20"
        ):
            print(f"execution {intent_id}: attempt={attempt} state={state} error={error_code} op={op_id}")
        for proc in (
            "gxg_decide_session(text, uuid, integer, text, text, text)",
            "gxg_decide(bytea, bytea, bigint, text, jsonb)",
        ):
            row = conn.execute("SELECT to_regprocedure(%s)", (proc,)).fetchone()
            if row and row[0]:
                grants = [
                    r
                    for r in ("gxg_ui", "gxg_decider")
                    if (
                        conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (r, proc)).fetchone()
                        or [False]
                    )[0]
                ]
                print(f"function {proc.split('(')[0]}: EXECUTE for {grants}")
            else:
                print(f"function {proc.split('(')[0]}: MISSING")
    except psycopg.Error as exc:
        print(f"decision report unavailable ({type(exc).__name__})")


if __name__ == "__main__":
    main()
