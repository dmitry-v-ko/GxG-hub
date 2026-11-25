"""Synthetic data of `demo` (fixtures/boards.json): deterministic notes from a fixed seed (demo-connector-v1)."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "boards.json"
_WORDS = ("roadmap", "budget", "status", "release", "invoice", "review", "deadline", "meeting", "draft", "risk")
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Note:
    id: str
    board_id: str
    title: str
    body: str
    updated_at: str
    indexed: str = field(init=False, repr=False)  # what `index` searches: precomputed at load time

    def __post_init__(self) -> None:
        object.__setattr__(self, "indexed", f"{self.title}\n{self.body}".casefold())


@dataclass
class Board:
    id: str
    title: str
    notes: list[Note]


def new_note(rng: random.Random, board: str, title: str, body: str) -> Note:
    """A note with a random, globally unique id (not derived from the board) and a time stamp."""
    stamp = (_EPOCH + timedelta(minutes=rng.randrange(400_000))).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Note(str(uuid.UUID(int=rng.getrandbits(128), version=4)), board, title, body, stamp)


def load(path: Path = FIXTURES) -> tuple[dict[str, str], dict[str, dict[str, Board]]]:
    """(tenant -> connection id, tenant -> board id -> board)."""
    doc = json.loads(path.read_bytes())
    rng = random.Random(doc["seed"])  # noqa: S311 - deterministic synthetic data, not security
    connections, boards = {}, {}
    for tenant, t in doc["tenants"].items():
        connections[tenant] = t["connection"]
        boards[tenant] = {b["id"]: Board(b["id"], b["title"], []) for b in t["boards"]}
        for b in t["boards"]:
            for i in range(b["notes"]):
                words = rng.sample(_WORDS, 3)
                body = f"{b['id']} note {i + 1}: {' '.join(words)}. Status: {rng.choice(('open', 'done', 'blocked'))}."
                boards[tenant][b["id"]].notes.append(new_note(rng, b["id"], f"{words[0].capitalize()} {i + 1}", body))
    for s in doc["special_notes"]:
        boards[s["tenant"]][s["board"]].notes.append(new_note(rng, s["board"], s["title"], s["body"]))
    return connections, boards
