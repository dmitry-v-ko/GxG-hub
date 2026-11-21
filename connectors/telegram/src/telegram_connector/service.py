"""The domain port of the existing Telegram service (gxg_agents_v2, docs/current/telegram-ingestion.md).

The service owns the user session, its lock, archive sync, Connection scope (CON-TG-02) and flood waits.
The connector only calls this port, after its own grant checks. Contract, from gxg_agents_v2 @ f1c4d6d:
- `search`: `PostgresTelegramArchiveRepository.search` (archive-repository.ts): connection_id, query
  1..1000, chat_refs 1..128, limit 1..100; ranked items; complete + partial_reasons.
- `read_thread`, `collect`: the target `telegram.thread.read` and the Chat folders archive position
  `(archived_us, message_id)` of connectors/telegram.md; not served by the service yet.
- errors: the failure classes of connectors/telegram.md (`ServiceError.kind`), never message text or peers.
shortcut: no network adapter, the service has no read endpoint yet (reports/M11-ESCALATION.md); add an HTTP
client implementing `TelegramService` when it has one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Kind = Literal["auth_required", "busy", "flood_wait", "chat_forbidden", "peer_inaccessible", "provider_error"]
Reason = Literal["result_limit", "semantic_pending", "sync_gap", "not_synced"]
SEARCH_LIMIT_MAX = 100  # telegramArchiveSearchInputSchema.limit
CHAT_REFS_MAX = 128


class ServiceError(Exception):
    """A failure class of the service; `retry_after_seconds` only with `flood_wait` (Telegram's own delay)."""

    def __init__(self, kind: Kind, retry_after_seconds: float | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Message:
    chat_ref: str
    message_id: int
    sent_at: str
    edited_at: str | None
    text: str | None
    caption: str | None
    reply_to: int | None
    media_type: str | None


@dataclass(frozen=True)
class Page:
    """`items` in the port's order; `more` = items exist past the page (search: `result_limit`)."""

    items: list[Message]
    more: bool
    partial_reasons: list[Reason]


@dataclass(frozen=True)
class Collected:
    """Oldest archived first; `positions[i]` is the archive position `(archived_us, message_id)` of `items[i]`."""

    items: list[Message]
    positions: list[tuple[int, int]]
    more: bool
    partial_reasons: list[Reason]


class TelegramService(Protocol):
    async def scope_chats(self, connection: str) -> list[str]:
        """chat_refs of the Connection's scope (the outer boundary, CON-TG-02)."""

    async def search(self, connection: str, query: str, chat_refs: list[str], limit: int) -> Page: ...

    async def read_thread(self, connection: str, chat_ref: str, before_message_id: int | None, limit: int) -> Page:
        """Newest first, messages older than `before_message_id`."""

    async def collect(self, connection: str, chat_ref: str, after: tuple[int, int] | None, limit: int) -> Collected: ...

    def ready(self) -> bool: ...
