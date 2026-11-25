"""AC-M14-01/02 (OPS-03, OPS-04): backup → restore onto a clean database → intents as at the backup, the executor
makes no second provider call, and payload past its RET-01 term is not brought back."""

from __future__ import annotations

import base64
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from test_executor import Stack, stack  # noqa: F401  (`stack`: the fixture of test_executor)

from scripts.ops import approval_backup as ab

KEY = bytes(range(32))
ADMIN = "test"  # superuser of the test server (conftest)


@dataclass(frozen=True)
class Pg:
    socket: str
    dbname: str

    def dsn(self, role: str) -> str:
        return f"host={self.socket} dbname={self.dbname} user={role}"

    def connect(self, role: str) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn(role), autocommit=True)


def _admin(socket: str, sql: str) -> None:
    with psycopg.connect(f"host={socket} dbname=postgres user={ADMIN}", autocommit=True) as conn:
        conn.execute(sql)  # static SQL


def snapshot(conn: psycopg.Connection[Any]) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "intents": conn.execute(
            "SELECT intent_id, status, attempts, version FROM intents ORDER BY intent_id"
        ).fetchall(),
        "executions": conn.execute(
            "SELECT intent_id, attempt, state FROM executions ORDER BY intent_id, attempt"
        ).fetchall(),
    }


@pytest.fixture
def clean_stand(pg: Any) -> Iterator[Pg]:
    """An empty database on the same server: what a freshly provisioned stand looks like before the restore."""
    name = f"gxg_restore_{uuid.uuid4().hex[:10]}"
    _admin(pg.socket, f"CREATE DATABASE {name} OWNER gxg_owner")
    yield Pg(pg.socket, name)
    _admin(pg.socket, f"DROP DATABASE {name} WITH (FORCE)")


def test_restore_keeps_intent_states_and_the_executor_calls_the_provider_no_second_time(
    stack: Stack,  # noqa: F811
    pg: Any,
    clean_stand: Pg,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ok = stack.propose(idempotency_key="backup-key-1")
    stack.drain(stack.executor(), lambda: stack.status(ok["intent_id"]) == "succeeded")
    stack.demo.mode("commit_then_drop", count=1)  # the provider executed, the answer was lost → reconciled
    lost = stack.propose(idempotency_key="backup-key-2", text="second")
    stack.drain(stack.executor("ex-2"), lambda: stack.status(lost["intent_id"]) == "reconciled_succeeded")
    old = stack.propose(idempotency_key="backup-key-3", text="third")
    stack.drain(stack.executor("ex-3"), lambda: stack.status(old["intent_id"]) == "succeeded")
    stack.owner.execute("ALTER TABLE intents DISABLE TRIGGER USER")  # a test row: terminal for 40 days (RET-01: 30)
    stack.owner.execute(
        "UPDATE intents SET terminal_at = now() - interval '40 days' WHERE intent_id = %s", (old["intent_id"],)
    )
    stack.owner.execute("ALTER TABLE intents ENABLE TRIGGER USER")
    assert len(stack.demo.calls()) == 3
    before = snapshot(stack.owner)

    monkeypatch.setenv("PGHOST", pg.socket)
    monkeypatch.setenv("PGUSER", ADMIN)
    monkeypatch.setenv("PGDATABASE", pg.dbname)
    key = base64.urlsafe_b64encode(KEY).decode()
    monkeypatch.setenv("GXG_BACKUP_KEY", key)
    dump = ab.backup(tmp_path, KEY, datetime.now(UTC))
    assert b"PGDMP" not in dump.read_bytes()  # encrypted: not even the archive header shows

    summary = ab.restore(dump, clean_stand.dbname, KEY, 30)
    assert summary["purged"] == 1  # only the 40-day-old payload
    with clean_stand.connect("gxg_owner") as restored:
        assert snapshot(restored) == before  # OPS-03: the states of the moment of the backup
        row = restored.execute(
            "SELECT args_ciphertext IS NULL, payload_purged_at IS NOT NULL FROM intents WHERE intent_id = %s",
            (old["intent_id"],),
        ).fetchone()
        assert row == (True, True)  # OPS-04: past its term, so not back from the old backup
        live = restored.execute("SELECT count(*) FROM intents WHERE args_ciphertext IS NOT NULL").fetchone()
        assert live == (2,)  # the others keep their payload until their own term

        dir2 = Path(tempfile.mkdtemp(prefix="m14", dir="/tmp"))
        with ConnectionPool(clean_stand.dsn("gxg_api"), min_size=1, max_size=5, kwargs={"autocommit": True}) as pool:
            second = Stack(clean_stand, restored, stack.keys, dir2, pool)  # the same keys, an EMPTY provider journal
            try:
                ex = second.executor("ex-restored")
                for _ in range(15):
                    ex.run_once()
                assert second.demo.calls() == []  # no provider call at all after the executor started
                assert snapshot(restored) == before  # and no state moved
            finally:
                second.close()
                shutil.rmtree(dir2, ignore_errors=True)


def test_restore_refuses_a_database_that_is_not_clean(pg: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PGHOST", pg.socket)
    monkeypatch.setenv("PGUSER", ADMIN)
    monkeypatch.setenv("PGDATABASE", pg.dbname)
    dump = ab.backup(tmp_path, KEY, datetime.now(UTC))
    with pytest.raises(ab.BackupError, match="not empty"):
        ab.restore(dump, pg.dbname, KEY, 30)
