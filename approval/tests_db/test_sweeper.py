"""Sweeper: T5, T8, T10, T14 as role gxg_sweeper. AC-M7-12."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from gxg_approval import sweeper

HUMAN = "oidc:https://idp.example.invalid:1"


def status(owner: psycopg.Connection[Any], intent: str) -> str:
    row = owner.execute("SELECT status FROM intents WHERE intent_id = %s", (intent,)).fetchone()
    assert row is not None
    return str(row[0])


def test_expired_intent_leaves_pending_within_60_s(
    pg: Any, owner: psycopg.Connection[Any], propose: Callable[..., Any]
) -> None:
    """AC-M7-12: the running sweeper process moves an intent with expires_at in the past to `expired`."""
    due, fresh = propose().json()["intent_id"], propose().json()["intent_id"]
    owner.execute("UPDATE intents SET expires_at = now() - interval '1 second' WHERE intent_id = %s", (due,))
    stop = threading.Event()
    worker = threading.Thread(
        target=sweeper.run, args=(pg.dsn("gxg_sweeper"),), kwargs={"interval_s": 0.2, "stop": stop}
    )
    started = time.monotonic()
    worker.start()
    try:
        while status(owner, due) != "expired" and time.monotonic() - started < 60:
            time.sleep(0.05)
        assert status(owner, due) == "expired"
        assert time.monotonic() - started < 60
    finally:
        stop.set()
        worker.join(5)
    assert status(owner, fresh) == "pending_approval"
    audit = owner.execute("SELECT actor, from_status, to_status FROM audit_events WHERE action = 'expire_approval'")
    assert audit.fetchall() == [("sweeper", "pending_approval", "expired")]
    assert owner.execute("SELECT topic FROM outbox WHERE intent_id = %s ORDER BY id", (due,)).fetchall() == [
        ("webhook",),  # M10: T1 event (trigger)
        ("notify",),
        ("webhook",),
    ]


def test_one_pass_handles_all_four_deadlines(
    pg: Any, owner: psycopg.Connection[Any], propose: Callable[..., Any]
) -> None:
    ids = [propose(tool="demo.note.propose").json()["intent_id"] for _ in range(4)]  # approved (not_required)
    pending = propose().json()["intent_id"]
    with pg.connect("gxg_executor") as ex, pg.connect("gxg_api") as api:
        for i in ids[1:]:
            ex.execute("SELECT gxg_reserve(%s, 'x')", (i,))
        execution = owner.execute("SELECT execution_id FROM executions WHERE intent_id = %s", (ids[3],)).fetchone()
        assert execution is not None
        api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (execution[0], ids[3]))
    owner.execute("UPDATE intents SET expires_at = now() WHERE intent_id = %s", (pending,))
    owner.execute("UPDATE intents SET execution_deadline = now() WHERE intent_id = %s", (ids[0],))
    owner.execute("UPDATE executions SET reserve_lease_until = now() WHERE intent_id = %s", (ids[1],))
    owner.execute("UPDATE executions SET dispatch_lease_until = now() WHERE intent_id = %s", (ids[3],))
    with pg.connect("gxg_sweeper") as conn:
        assert sweeper.sweep_once(conn) == 4
        assert sweeper.sweep_once(conn) == 0
    assert [status(owner, i) for i in (pending, *ids)] == [
        "expired",
        "expired",
        "approved",
        "executing",
        "outcome_unknown",
    ]


def test_batches_until_done(
    pg: Any, owner: psycopg.Connection[Any], propose: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, world: Any
) -> None:
    world.set_policy("t-alpha", [], quotas={"pending_per_requester_max": 100})
    for _ in range(5):
        propose()
    owner.execute("UPDATE intents SET expires_at = now()")
    monkeypatch.setattr(sweeper, "BATCH", 2)
    with pg.connect("gxg_sweeper") as conn:
        assert sweeper.sweep_once(conn) == 5
    assert owner.execute("SELECT count(*) FROM intents WHERE status = 'expired'").fetchone() == (5,)


def test_interval_is_bounded() -> None:
    stopped = threading.Event()
    stopped.set()  # a wrong check returns at once instead of looping
    for bad in (0, -1, 30.001, 31):
        with pytest.raises(ValueError, match="interval_s"):
            sweeper.run("host=/nonexistent", interval_s=bad, stop=stopped)
    sweeper.run("host=/nonexistent", interval_s=sweeper.MAX_INTERVAL_S, stop=stopped)  # the bound itself is allowed


def test_reconnects_after_a_database_error(
    pg: Any, owner: psycopg.Connection[Any], propose: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    stop = threading.Event()
    worker = threading.Thread(
        target=sweeper.run, args=("host=/nonexistent dbname=x",), kwargs={"interval_s": 0.05, "stop": stop}
    )
    worker.start()
    time.sleep(0.3)
    stop.set()
    worker.join(5)
    assert not worker.is_alive()
    assert "sweeper: database error" in [r.getMessage() for r in caplog.records]
