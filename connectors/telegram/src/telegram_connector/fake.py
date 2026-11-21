"""Fake of the existing Telegram service (test profile only): fixtures/service.json behind the `TelegramService` port.

It plays the service side: it owns one "session" per Connection (it never opens another one), enforces its
Connection scope (CON-TG-02: `chat_forbidden`), and knows peer IDs and session strings that must never leave
it. Counters: requests per port method (AC-M11-03) and open sessions (AC-M11-02). Failure modes (MC-03,
AC-M11-04): every request raises a `ServiceError` kind, `flood_wait:<s>`, or waits `slow:<ms>`.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .service import SEARCH_LIMIT_MAX, Collected, Kind, Message, Page, Reason, ServiceError

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "service.json"
_WORDS = ("roadmap", "budget", "status", "release", "invoice", "review", "deadline", "встреча", "договор", "риск")
_EPOCH = datetime(2026, 9, 1, tzinfo=UTC)
_MEDIA = ("photo", "document", None)


@dataclass
class Chat:
    name: str
    chat_ref: str
    peer_id: str
    state: str
    gap: bool
    messages: list[Message]  # message_id ascending
    archived_us: list[int]  # archive position of messages[i]


class FakeTelegramService:
    def __init__(self, path: Path = FIXTURES) -> None:
        doc = json.loads(path.read_bytes())
        rng = random.Random(doc["seed"])  # noqa: S311 - deterministic synthetic data, not security
        self.sessions: dict[str, str] = {}  # connection -> session string: the service's secret (AC-M11-05)
        self.chats: dict[str, dict[str, Chat]] = {}  # connection -> chat_ref -> chat
        for conn, c in doc["connections"].items():
            self.sessions[conn] = c["session"]
            self.chats[conn] = {}
            for ch in c["chats"]:
                texts = []
                for i in range(ch["messages"]):
                    words = " ".join(rng.sample(_WORDS, 3))
                    texts.append(f"{ch['name']} message {i + 1}: {words}. Status: {rng.choice(('open', 'done'))}.")
                self.chats[conn][ch["chat_ref"]] = Chat(
                    ch["name"], ch["chat_ref"], ch["peer_id"], ch["state"], ch["gap"], [], []
                )
                for text in texts:
                    self._append(conn, ch["chat_ref"], text, rng)
        for s in doc["special_messages"]:
            ref = next(r for r, ch in self.chats[s["connection"]].items() if ch.name == s["chat"])
            self._append(s["connection"], ref, s["text"], rng)
        self.calls: Counter[str] = Counter()  # requests per port method
        self.failure: Kind | None = None
        self.retry_after_s = 0.0  # with failure == "flood_wait"
        self.delay_s = 0.0

    def _append(self, conn: str, ref: str, text: str, rng: random.Random) -> None:
        chat = self.chats[conn][ref]
        mid = len(chat.messages) + 1
        sent = _EPOCH + timedelta(minutes=10 * mid + rng.randrange(5))
        media = _MEDIA[mid % 3]
        caption = text if media == "document" else None  # a document carries its text as the caption
        chat.messages.append(
            Message(
                ref,
                mid,
                sent.strftime("%Y-%m-%dT%H:%M:%SZ"),
                None,
                None if caption else text,
                caption,
                mid - 1 if mid % 4 == 0 else None,
                media,
            )
        )
        chat.archived_us.append(int(sent.timestamp() * 1_000_000) + mid)

    @property
    def open_sessions(self) -> int:
        """The service's own sessions: one per Connection. Nothing on the port opens another one (AC-M11-02)."""
        return len(self.sessions)

    def texts(self) -> list[str]:
        return [
            m.text or m.caption or "" for chats in self.chats.values() for ch in chats.values() for m in ch.messages
        ]

    async def _enter(self, method: str, conn: str, refs: list[str]) -> list[Chat]:
        """Count the request, apply the failure mode, then the Connection scope (the service's own check)."""
        self.calls[method] += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.failure is not None:
            raise ServiceError(self.failure, self.retry_after_s if self.failure == "flood_wait" else None)
        chats = self.chats.get(conn, {})
        if any(r not in chats for r in refs):
            raise ServiceError("chat_forbidden")
        return [chats[r] for r in refs]

    @staticmethod
    def _reasons(chats: list[Chat]) -> list[Reason]:
        out: set[Reason] = set()
        for ch in chats:
            if ch.state != "synced":
                out.add("not_synced")
            if ch.gap:
                out.add("sync_gap")
        return sorted(out)

    async def scope_chats(self, connection: str) -> list[str]:
        await self._enter("scope_chats", connection, [])
        return sorted(self.chats.get(connection, {}))

    async def search(self, connection: str, query: str, chat_refs: list[str], limit: int) -> Page:
        if not 1 <= limit <= SEARCH_LIMIT_MAX or not chat_refs or not query.strip():
            raise ValueError("search input outside telegramArchiveSearchInputSchema")
        chats = await self._enter("search", connection, chat_refs)
        terms = query.casefold().split()
        hits = [
            m for ch in chats for m in ch.messages if all(t in (m.text or m.caption or "").casefold() for t in terms)
        ]
        hits.sort(key=lambda m: (m.sent_at, m.message_id), reverse=True)  # the service ranks, then newest first
        reasons = self._reasons(chats)
        if len(hits) > limit:
            reasons = sorted({*reasons, "result_limit"})
        return Page(hits[:limit], len(hits) > limit, reasons)

    async def read_thread(self, connection: str, chat_ref: str, before_message_id: int | None, limit: int) -> Page:
        (chat,) = await self._enter("read_thread", connection, [chat_ref])
        older = [m for m in reversed(chat.messages) if before_message_id is None or m.message_id < before_message_id]
        return Page(older[:limit], len(older) > limit, self._reasons([chat]))

    async def collect(self, connection: str, chat_ref: str, after: tuple[int, int] | None, limit: int) -> Collected:
        (chat,) = await self._enter("collect", connection, [chat_ref])
        rows = [
            (p, m)
            for p, m in zip(
                zip(chat.archived_us, (m.message_id for m in chat.messages), strict=True), chat.messages, strict=True
            )
            if after is None or p > after
        ]
        page = rows[:limit]
        return Collected([m for _, m in page], [p for p, _ in page], len(rows) > limit, self._reasons([chat]))

    def ready(self) -> bool:
        return self.failure is None
