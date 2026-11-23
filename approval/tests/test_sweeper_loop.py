"""The sweeper loop without a database (critic M10-2 N4): defaults, the hourly purge clock, logs, errors."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from gxg_approval import sweeper


class Stop:
    """threading.Event stand-in: records every wait and stops after `waits` of them."""

    def __init__(self, waits: int) -> None:
        self.waits, self.args = waits, list[Any]()

    def is_set(self) -> bool:
        return len(self.args) >= self.waits

    def wait(self, timeout: Any) -> bool:
        self.args.append(timeout)
        return self.is_set()


class Conn:
    def __init__(self, answers: dict[str, list[Any]]) -> None:
        self.answers, self.calls = answers, list[tuple[str, Any]]()

    def execute(self, sql: str, params: Any) -> Any:
        self.calls.append((sql, params))
        assert len(self.calls) < 100, "the loop does not stop"  # a broken stop check fails fast, not hangs
        queue = self.answers[sql.partition("(")[0].upper()]
        return Row(queue.pop(0) if len(queue) > 1 else queue[0])


class Row:
    def __init__(self, row: Any) -> None:
        self.row = row

    def fetchone(self) -> Any:
        return self.row


def connected(monkeypatch: pytest.MonkeyPatch, conn: Conn) -> None:
    connects = list[str]()

    @contextlib.contextmanager
    def connect(dsn: str, **_kw: Any) -> Iterator[Conn]:
        connects.append(dsn)
        assert len(connects) < 10, "the loop reconnects without working"
        yield conn

    monkeypatch.setattr(psycopg, "connect", connect)


def test_defaults_purge_clock_and_logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "gxg_approval.sweeper")
    conn = Conn({"SELECT GXG_SWEEP": [(3,), (0,)], "SELECT GXG_PURGE": [(2,), (0,)]})
    connected(monkeypatch, conn)
    stop = Stop(4)
    ticks = iter([0.0, 3599.0, 3600.0, 3600.0, 3601.0])  # purge at start, not before an hour, at an hour exactly
    sweeper.run("dsn", stop=stop, clock=lambda: next(ticks))  # type: ignore[arg-type]
    purges = [p for sql, p in conn.calls if "purge" in sql]
    assert purges == [(30, sweeper.BATCH), (30, sweeper.BATCH)]  # retention_days default 30
    assert stop.args == [5.0] * 4  # interval_s default 5 s
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["sweeper: 3 transitions", "sweeper: payload purged for 2 intents",
                        "sweeper: payload purged for 0 intents"]  # fmt: skip


def test_no_row_counts_as_zero() -> None:
    conn = Conn({"SELECT GXG_SWEEP": [None], "SELECT GXG_PURGE": [None]})
    assert sweeper.sweep_once(conn) == 0 and sweeper.purge_once(conn, 30) == 0  # type: ignore[arg-type]


def test_database_error_waits_one_interval(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    tries = list[str]()

    def refused(dsn: str, **_kw: Any) -> Any:
        tries.append(dsn)
        assert len(tries) < 10, "the loop does not stop"
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(psycopg, "connect", refused)
    stop = Stop(2)
    sweeper.run("dsn", interval_s=0.5, stop=stop)  # type: ignore[arg-type]
    assert stop.args == [0.5, 0.5]
    assert [r.getMessage() for r in caplog.records] == ["sweeper: database error"] * 2


def test_the_given_stop_ends_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = Conn({"SELECT GXG_SWEEP": [(0,)], "SELECT GXG_PURGE": [(0,)]})
    connected(monkeypatch, conn)
    stop = Stop(1)
    sweeper.run("dsn", interval_s=0.01, stop=stop)  # type: ignore[arg-type]
    assert stop.args == [0.01] and len(conn.calls) == 2  # one sweep, one purge, then the caller's stop
