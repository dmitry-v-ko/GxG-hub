"""AC-M10-03 (RET-01, OPS-04 partly), backlog M9 F-2: the payload purge of the sweeper."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from gxg_approval import sweeper

COLS = "args_ciphertext IS NULL, preview_ciphertext IS NULL, payload_purged_at IS NOT NULL"


def make(propose: Callable[..., Any], owner: psycopg.Connection[Any], status: str, days: int, n: int) -> str:
    """An intent moved to `status` `days` ago (terminal_at), as the transitions leave it."""
    intent = str(propose(args={"text": f"t{n}", "n": n}).json()["intent_id"])
    owner.execute("ALTER TABLE intents DISABLE TRIGGER USER")  # a test row, not a transition: no webhook event
    owner.execute(
        "UPDATE intents SET status = %s, terminal_at = CASE WHEN %s <> 'outcome_unknown' "
        "THEN now() - make_interval(days => %s) END WHERE intent_id = %s",
        (status, status, days, intent),
    )
    owner.execute("ALTER TABLE intents ENABLE TRIGGER USER")
    return intent


def state(owner: psycopg.Connection[Any], intent: str) -> tuple[Any, ...]:
    row = owner.execute(f"SELECT {COLS} FROM intents WHERE intent_id = %s", (intent,)).fetchone()
    assert row is not None
    return tuple(row)


def test_terminal_31_days_ago_is_purged_metadata_stays(
    propose: Callable[..., Any], owner: psycopg.Connection[Any], pg: Any
) -> None:
    old = make(propose, owner, "succeeded", 31, 1)
    young = make(propose, owner, "rejected", 29, 2)
    unknown = make(propose, owner, "outcome_unknown", 0, 3)
    owner.execute("UPDATE intents SET created_at = now() - interval '60 days' WHERE intent_id = %s", (unknown,))
    before = owner.execute(
        "SELECT tenant_id, requester_principal, tool_key, canonical_args_hash, status, version, terminal_at "
        "FROM intents WHERE intent_id = %s",
        (old,),
    ).fetchone()
    with pg.connect("gxg_sweeper") as conn:
        assert sweeper.purge_once(conn, 30) == 1
        assert sweeper.purge_once(conn, 30) == 0  # once
    assert state(owner, old) == (True, True, True)
    after = owner.execute(
        "SELECT tenant_id, requester_principal, tool_key, canonical_args_hash, status, version, terminal_at "
        "FROM intents WHERE intent_id = %s",
        (old,),
    ).fetchone()
    assert after == before  # metadata in place (AS-02: UPDATE, never DELETE)
    assert state(owner, young) == (False, False, False)
    assert state(owner, unknown) == (False, False, False)  # outcome_unknown: never until resolved
    audit = owner.execute("SELECT actor, intent_id::text, details FROM audit_events WHERE action = 'payload_purged'")
    assert audit.fetchall() == [("sweeper", old, {"retention_days": 30})]


def test_shorter_retention_and_its_bounds(propose: Callable[..., Any], owner: psycopg.Connection[Any], pg: Any) -> None:
    """RET-01: 1 to 90 days."""
    intent = make(propose, owner, "expired", 2, 1)
    with pg.connect("gxg_sweeper") as conn:
        for bad in (0, 91):
            with pytest.raises(psycopg.Error) as err:
                conn.execute("SELECT gxg_purge(%s, 10)", (bad,))
            assert err.value.sqlstate == "GX400"
        assert sweeper.purge_once(conn, 3) == 0
        assert sweeper.purge_once(conn, 1) == 1
    assert state(owner, intent) == (True, True, True)


def test_batches_until_done(
    propose: Callable[..., Any], owner: psycopg.Connection[Any], pg: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(sweeper, "BATCH", 2)
    for n in range(5):
        make(propose, owner, "cancelled", 40, n)
    with pg.connect("gxg_sweeper") as conn:
        assert sweeper.purge_once(conn, 30) == 5


def test_delivered_outbox_and_old_rows_are_deleted(
    propose: Callable[..., Any], owner: psycopg.Connection[Any], pg: Any
) -> None:
    """M9-final F-2: a delivered `notify` row (title, requester) is gone a day later; undelivered rows stay."""
    propose()
    owner.execute("UPDATE outbox SET delivered_at = now() - interval '25 hours' WHERE topic = 'notify'")
    owner.execute("INSERT INTO outbox (topic, payload, created_at, available_at, delivered_at) "
                  "VALUES ('notify', '{}', now(), now(), now() - interval '1 hour')")  # fmt: skip
    owner.execute(
        "INSERT INTO webhook_deliveries (outbox_id, subscriber_id, tenant_id, event, created_at, next_at, "
        "delivered_at, dead_at) VALUES (1, 'a', 't', '{}', now(), now(), now() - interval '8 days', null), "
        "(2, 'a', 't', '{}', now(), now(), null, now() - interval '8 days'), "
        "(3, 'a', 't', '{}', now(), now(), now() - interval '6 days', null), "
        "(4, 'a', 't', '{}', now(), now(), null, null)"
    )
    owner.execute(
        "INSERT INTO decider_challenges (challenge, purpose, human_principal, created_at) VALUES "
        "('\\x01', 'decide', 'oidc:x', now() - interval '2 days'), ('\\x02', 'decide', 'oidc:x', now())"
    )
    owner.execute(
        "INSERT INTO passkey_invites VALUES (repeat('a', 64), 'oidc:x', 'oidc:op', now(), now() - interval '2 days', "
        "null), (repeat('b', 64), 'oidc:x', 'oidc:op', now(), now() + interval '1 day', null)"
    )
    with pg.connect("gxg_sweeper") as conn:
        sweeper.purge_once(conn, 30)
    left = owner.execute("SELECT topic, delivered_at IS NULL FROM outbox ORDER BY id").fetchall()
    assert left.count(("notify", False)) == 1  # the row delivered an hour ago stays; the one of 25 h is gone
    assert owner.execute("SELECT count(*) FROM outbox WHERE delivered_at < now() - interval '1 day'").fetchone() == (0,)
    assert owner.execute("SELECT count(*) > 0 FROM outbox WHERE delivered_at IS NULL").fetchone() == (True,)  # event
    assert owner.execute("SELECT outbox_id FROM webhook_deliveries ORDER BY outbox_id").fetchall() == [(3,), (4,)]
    assert owner.execute("SELECT challenge FROM decider_challenges").fetchall() == [(b"\x02",)]
    assert owner.execute("SELECT token_hash FROM passkey_invites").fetchall() == [("b" * 64,)]


def test_sweeper_purges_hourly(pg: Any, propose: Callable[..., Any], owner: psycopg.Connection[Any]) -> None:
    """The sweeper loop runs the purge at start and then once per PURGE_EVERY_S."""
    old = make(propose, owner, "failed", 31, 1)
    stop = threading.Event()
    ticks = iter([0.0, 10.0, 3700.0, 3700.0, 3700.0])
    calls: list[int] = []
    real = sweeper.purge_once

    def counted(conn: Any, days: int) -> int:
        calls.append(days)
        if len(calls) == 2:
            stop.set()
        return real(conn, days)

    import unittest.mock

    with unittest.mock.patch.object(sweeper, "purge_once", counted):
        worker = threading.Thread(
            target=sweeper.run,
            args=(pg.dsn("gxg_sweeper"),),
            kwargs={"interval_s": 0.01, "retention_days": 30, "stop": stop, "clock": lambda: next(ticks, 3700.0)},
            daemon=True,
        )
        worker.start()
        worker.join(10)
    assert calls == [30, 30] and state(owner, old) == (True, True, True)
