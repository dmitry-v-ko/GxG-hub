"""`sweeper` process: deadlines T5, T8 and leases T10, T14 (one `gxg_sweep()` call per batch).

Several sweepers can run at once: candidates are taken with FOR UPDATE SKIP LOCKED. With the default
interval an expired intent leaves `pending_approval` within ~5 s (AC-M7-12 asks for ≤ 60 s).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import psycopg

log = logging.getLogger("gxg_approval.sweeper")
BATCH = 100
MAX_INTERVAL_S = 30.0  # keeps AC-M7-12 (≤ 60 s) with room for one slow pass


def sweep_once(conn: psycopg.Connection[Any]) -> int:
    """Run batches until a batch is not full. Returns the number of transitions made."""
    total = 0
    while True:
        row = conn.execute("SELECT gxg_sweep(%s)", (BATCH,)).fetchone()
        moved = int(row[0]) if row else 0
        total += moved
        if moved < BATCH:
            return total


def run(dsn: str, *, interval_s: float = 5.0, stop: threading.Event | None = None) -> None:
    if not 0 < interval_s <= MAX_INTERVAL_S:
        raise ValueError(f"interval_s must be in (0, {MAX_INTERVAL_S}]")
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                while not stop.is_set():
                    if moved := sweep_once(conn):
                        log.info("sweeper: %d transitions", moved)
                    stop.wait(interval_s)
        except psycopg.Error:  # database restart or failover: reconnect on the next tick
            log.exception("sweeper: database error")
            stop.wait(interval_s)
