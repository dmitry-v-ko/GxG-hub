"""`sweeper` process: deadlines T5, T8 and leases T10, T14 (one `gxg_sweep()` call per batch); once an hour the
payload purge of RET-01 (`gxg_purge`: payload of intents terminal for `retention_days`, delivered outbox rows).

Several sweepers can run at once: candidates are taken with FOR UPDATE SKIP LOCKED. With the default
interval an expired intent leaves `pending_approval` within ~5 s (AC-M7-12 asks for ≤ 60 s).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg

log = logging.getLogger("gxg_approval.sweeper")
BATCH = 100
MAX_INTERVAL_S = 30.0  # keeps AC-M7-12 (≤ 60 s) with room for one slow pass
PURGE_EVERY_S = 3600.0  # RET-01 counts days; an hourly pass keeps the scan of terminal intents rare


def sweep_once(conn: psycopg.Connection[Any]) -> int:
    """Run batches until a batch is not full. Returns the number of transitions made."""
    total = 0
    while True:
        row = conn.execute("SELECT gxg_sweep(%s)", (BATCH,)).fetchone()
        moved = int(row[0]) if row else 0
        total += moved
        if moved < BATCH:
            return total


def purge_once(conn: psycopg.Connection[Any], retention_days: int) -> int:
    """RET-01 in batches. Returns the number of intents whose payload was purged."""
    total = 0
    while True:
        row = conn.execute("SELECT gxg_purge(%s, %s)", (retention_days, BATCH)).fetchone()
        purged = int(row[0]) if row else 0
        total += purged
        if purged < BATCH:
            return total


def run(
    dsn: str,
    *,
    interval_s: float = 5.0,
    retention_days: int = 30,
    stop: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    if not 0 < interval_s <= MAX_INTERVAL_S:
        raise ValueError(f"interval_s must be in (0, {MAX_INTERVAL_S}]")
    stop = stop or threading.Event()
    purged_at: float | None = None
    while not stop.is_set():
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                while not stop.is_set():
                    if moved := sweep_once(conn):
                        log.info("sweeper: %d transitions", moved)
                    if purged_at is None or clock() - purged_at >= PURGE_EVERY_S:
                        log.info("sweeper: payload purged for %d intents", purge_once(conn, retention_days))
                        purged_at = clock()
                    stop.wait(interval_s)
        except psycopg.Error:  # database restart or failover: reconnect on the next tick
            log.exception("sweeper: database error")
            stop.wait(interval_s)
