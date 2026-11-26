#!/usr/bin/env python3
"""Restore drill for the Approval backup (AC-M14-01, runbook §«Восстановление»): the NEWEST backup in --dir into a clean
database of a THROWAWAY PostgreSQL (never the production approval-db), then a comparison with production.

Environment: the production connection as for backup (PGHOST, PGUSER=gxg_backup, PGDATABASE, PGPASSWORD) and
GXG_BACKUP_KEY; the throwaway server as DRILL_PGHOST, DRILL_PGUSER, DRILL_PGPASSWORD (a superuser of that server).
Nothing secret is printed.

Steps: roles.sql on the throwaway server, `CREATE DATABASE approval_restored OWNER gxg_owner`, approval_backup.restore
(decrypt, pg_restore, RET-01 purge), then per table (intents, decisions, executions, audit_events) the row count and an
md5 over the sorted ids of the rows up to the backup time, on both sides. Exit 1 on any difference.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approval_backup as ab

DB = "approval_restored"
TABLES = (  # table, id column, time column of the row
    ("intents", "intent_id", "created_at"),
    ("decisions", "decision_id", "decided_at"),
    ("executions", "execution_id", "reserved_at"),
    ("audit_events", "id", "at"),
)


def fingerprint(conn: psycopg.Connection, upto: datetime) -> dict[str, tuple[int, str]]:
    out = {}
    for table, idc, tc in TABLES:
        row = conn.execute(
            f"SELECT count(*), coalesce(md5(string_agg({idc}::text, ',' ORDER BY {idc}::text)), 'empty') "  # noqa: S608
            f"FROM {table} WHERE {tc} <= %s",
            (upto,),
        ).fetchone()
        if row is None:
            raise ab.BackupError(f"no result for {table}")
        out[table] = (int(row[0]), str(row[1]))
    return out


def main() -> int:
    directory = Path(os.environ.get("GXG_BACKUP_DIR", "/var/backups/gxg-approval"))
    prod = {k: os.environ[k] for k in ("PGHOST", "PGUSER", "PGDATABASE", "PGPASSWORD")}
    drill = {
        "PGHOST": os.environ["DRILL_PGHOST"],
        "PGUSER": os.environ["DRILL_PGUSER"],
        "PGPASSWORD": os.environ["DRILL_PGPASSWORD"],
    }
    if drill["PGHOST"] == prod["PGHOST"]:
        print("drill: REFUSED, the throwaway server is the production server", flush=True)
        return 2
    files = ab.backups(directory)
    if not files:
        print("drill: no backup in the directory", flush=True)
        return 1
    stamp, newest = max(files)
    print(f"drill: backup {newest.name}, backup time {stamp.isoformat()}", flush=True)
    key = ab.key_from_env()

    os.environ.update(drill)
    os.environ.pop("PGDATABASE", None)
    roles_sql = Path(os.environ.get("GXG_ROLES_SQL", "/opt/gxg/roles.sql")).read_text("utf-8")
    t0 = datetime.now(UTC)
    with psycopg.connect(dbname="postgres", autocommit=True) as conn:
        conn.execute(roles_sql.encode())
        conn.execute(f"DROP DATABASE IF EXISTS {DB}")  # the throwaway server only (guarded above)
        conn.execute(f"CREATE DATABASE {DB} OWNER gxg_owner")
    summary = ab.restore(newest, DB, key, int(os.environ.get("GXG_RETENTION_DAYS", "30")))
    restore_s = (datetime.now(UTC) - t0).total_seconds()
    print(
        f"drill: restored in {restore_s:.1f} s: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())),
        flush=True,
    )
    with psycopg.connect(dbname=DB, autocommit=True) as conn:
        restored = fingerprint(conn, stamp)

    os.environ.update(prod)
    with psycopg.connect(autocommit=True, options="-c default_transaction_read_only=on") as conn:
        production = fingerprint(conn, stamp)
    bad = 0
    for table, _, _ in TABLES:
        same = restored[table] == production[table]
        bad += not same
        print(
            f"drill: {table}: production rows={production[table][0]} md5={production[table][1][:12]}, "
            f"restored rows={restored[table][0]} md5={restored[table][1][:12]} -> {'match' if same else 'MISMATCH'}",
            flush=True,
        )
    print("drill: " + ("OK, all tables match" if not bad else f"FAILED, {bad} table(s) differ"), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
