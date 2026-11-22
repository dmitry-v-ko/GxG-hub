"""Synthetic backend of `demo`: boards and notes of two tenants, search by `index` or `scan`.

Stands in for an existing service: it knows nothing about grants. The connector narrows
every call to the boards a caller may read (GR-16) before it reaches this module.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from gxg_connector_kit.errors import GxgError

from . import data
from .data import Board, Note

SCAN_DELAY_S = 2.0  # demo-connector-v1: `scan` is slow on purpose
IndexFailure = Literal["temporarily_unavailable", "timeout_before_start"]
BackendFailure = Literal["rate_limited", "temporarily_unavailable", "timeout_before_start"]
RETRY_AFTER_S = 7
Method = Literal["index", "scan"]


class Backend:
    def __init__(self, path: Path = data.FIXTURES, *, scan_delay_s: float = SCAN_DELAY_S) -> None:
        self.connections, self.boards = data.load(path)  # tenant -> connection; tenant -> board id -> board
        self.scan_delay_s = scan_delay_s
        self.index_failure: IndexFailure | None = None  # set by POST /_test/index/fail
        self.failure: BackendFailure | None = None  # set by POST /_test/backend/fail: every call fails (MC-03)
        self.delay_s = 0.0  # set by POST /_test/backend/fail `slow:<ms>`: every call waits (MC-03)
        self.calls: Counter[str] = Counter()  # tests: backend calls per method (AC-M3-07, AC-M3-08)

    def _enter(self, kind: str) -> None:
        """Count the call, then apply the test failure mode (the service behind the connector is down)."""
        self.calls[kind] += 1
        if self.failure == "rate_limited":
            raise GxgError("rate_limited", "backend rate limit", retry_after_s=RETRY_AFTER_S)
        if self.failure is not None:
            raise GxgError(self.failure, "backend is not available")

    def list_boards(self, tenant: str) -> list[Board]:
        self._enter("boards")
        return sorted(self.boards[tenant].values(), key=lambda b: b.id)

    def get_note(self, tenant: str, note_id: str) -> Note | None:
        """The note with its real board (GR-03), or None. Only this tenant's data (GR-02)."""
        self._enter("get")
        for board in self.boards[tenant].values():  # a tenant without a connection never gets here (kit hook)
            for note in board.notes:
                if note.id == note_id:
                    return note
        return None

    async def search(
        self, tenant: str, board_ids: list[str], query: str, *, deadline: float
    ) -> tuple[list[Note], Method, bool]:
        """Matches in `board_ids` only, by (board, note id); the method used; True if `scan` ran out of time.

        Fallback index -> scan only when the index fails before running (MCC-06);
        a valid empty index result is final.
        """
        self._enter("search")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        q = query.casefold()
        boards = [self.boards[tenant][b] for b in sorted(board_ids)]
        try:
            return self._index(boards, q), "index", False
        except GxgError as exc:
            if exc.code not in ("temporarily_unavailable", "timeout_before_start"):
                raise
        notes, timed_out = await self._scan(boards, q, deadline)
        return notes, "scan", timed_out

    def _index(self, boards: list[Board], q: str) -> list[Note]:
        self.calls["index"] += 1
        if self.index_failure is not None:
            raise GxgError(self.index_failure, "index is not available")
        return [n for b in boards for n in sorted(b.notes, key=_by_id) if q in n.indexed]

    async def _scan(self, boards: list[Board], q: str, deadline: float) -> tuple[list[Note], bool]:
        self.calls["scan"] += 1
        hits: list[Note] = []
        for b in boards:
            if time.monotonic() >= deadline:
                return hits, True
            await asyncio.sleep(self.scan_delay_s / len(boards))
            hits += [n for n in sorted(b.notes, key=_by_id) if q in f"{n.title}\n{n.body}".casefold()]
        return hits, False


def _by_id(note: Note) -> str:
    return note.id
