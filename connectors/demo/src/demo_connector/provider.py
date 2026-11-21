"""Fake provider of `demo` (demo-connector-v1 «Fake provider»): a journal of sends in SQLite, idempotent by key.

The provider is remote: it records a call when it receives it, `slow:<ms>` delays only the answer.
Journal row per call: `(tenant, intent_id, execution_id, idempotency_key, recipient, text_hash, attempt, outcome, at)`;
the row count is the call counter, and it survives restarts (a file in the volume). A call has an effect when its
outcome is `ok` or `commit_then_drop`. Dedupe is per `(tenant, idempotency_key)`, like a provider account per tenant
connection; a repeated key returns the first receipt and is logged as `duplicate` (no second effect).
Modes are set only through the test controls (`testctl.py`, test profile). The text itself is never stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Mode = Literal["ok", "reject", "rate_limited", "timeout_before_start", "commit_then_drop"]
MODES: tuple[Mode, ...] = ("ok", "reject", "rate_limited", "timeout_before_start", "commit_then_drop")
EFFECT = ("ok", "commit_then_drop")
RETRY_AFTER_S = 3
_SLOW = re.compile(r"slow:([0-9]{1,6})")
_SCHEMA = """CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, intent_id TEXT NOT NULL, execution_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, recipient TEXT NOT NULL, text_hash TEXT NOT NULL, attempt INTEGER NOT NULL,
  outcome TEXT NOT NULL, operation_id TEXT, at TEXT NOT NULL)"""
_COLUMNS = ("tenant", "intent_id", "execution_id", "idempotency_key", "recipient", "text_hash", "attempt", "outcome")


class Dropped(Exception):
    """commit_then_drop: the provider executed the request and the answer was lost."""


@dataclass(frozen=True)
class Sent:
    outcome: str  # journal outcome: ok, duplicate, reject, rate_limited, timeout_before_start
    operation_id: str | None


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Provider:
    def __init__(self, path: Path, credential: str = "") -> None:
        self._credential = credential  # the per-tenant provider token: used, never stored, logged or returned
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._lock = threading.Lock()
        self.mode: Mode = "ok"
        self.remaining: int | None = None  # calls left in `mode`, then `ok`; None: until changed
        self.dedupe = True
        self.delay_s = 0.0

    def set_mode(self, spec: str, count: int | None = None) -> None:
        """`<mode>[,no_dedupe][,slow:<ms>]`; a bad spec raises ValueError and changes nothing."""
        mode: Mode = "ok"
        dedupe, delay = True, 0.0
        for part in spec.split(","):
            slow = _SLOW.fullmatch(part)
            if part == "no_dedupe":
                dedupe = False
            elif slow:
                delay = int(slow[1]) / 1000
            elif part in MODES:
                mode = part
            else:
                raise ValueError(f"unknown provider mode {part!r}")
        if count is not None and count < 1:
            raise ValueError("count must be positive")
        self.mode, self.remaining, self.dedupe, self.delay_s = mode, count, dedupe, delay

    def _take_mode(self) -> Mode:
        mode = self.mode
        if self.remaining is not None:
            self.remaining -= 1
            if self.remaining == 0:
                self.mode, self.remaining = "ok", None
        return mode

    async def send(
        self, *, tenant: str, intent_id: str, execution_id: str, idempotency_key: str, recipient: str, text: str
    ) -> Sent:
        """One call, journalled at once; `slow` delays the answer. Raises Dropped for commit_then_drop."""
        with self._lock:
            mode = self._take_mode()
            first = None
            if self.dedupe and mode == "ok":
                first = self._db.execute(
                    "SELECT operation_id FROM calls WHERE tenant = ? AND idempotency_key = ? AND outcome IN (?, ?)"
                    " ORDER BY id LIMIT 1",
                    (tenant, idempotency_key, *EFFECT),
                ).fetchone()
            outcome = "duplicate" if first else mode
            operation = first[0] if first else (f"demo-op-{uuid.uuid4()}" if mode in EFFECT else None)
            attempt = self._db.execute(
                "SELECT count(*) FROM calls WHERE tenant = ? AND idempotency_key = ?", (tenant, idempotency_key)
            ).fetchone()[0]
            self._db.execute(
                "INSERT INTO calls (tenant, intent_id, execution_id, idempotency_key, recipient, text_hash, attempt,"
                " outcome, operation_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant, intent_id, execution_id, idempotency_key, recipient, text_hash(text), attempt + 1, outcome,
                 operation, datetime.now(UTC).isoformat()),
            )  # fmt: skip
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if outcome == "commit_then_drop":
            raise Dropped
        return Sent(outcome, operation)

    def lookup(self, intent_id: str) -> tuple[bool, str | None]:
        """Reconcile: (executed, operation id) of an intent, by its client reference."""
        with self._lock:
            row = self._db.execute(
                "SELECT operation_id FROM calls WHERE intent_id = ? AND outcome IN (?, ?) ORDER BY id LIMIT 1",
                (intent_id, *EFFECT),
            ).fetchone()
        return (row is not None, row[0] if row else None)

    def calls(self, idempotency_key: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(_COLUMNS)}, at FROM calls"  # noqa: S608 (constant column list)
                + (" WHERE idempotency_key = ?" if idempotency_key is not None else "")
                + " ORDER BY id",
                () if idempotency_key is None else (idempotency_key,),
            ).fetchall()
        return [dict(zip((*_COLUMNS, "at"), r, strict=True)) for r in rows]
