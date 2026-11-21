"""The port the tools read Telegram through, and its result types (ADR-0004).

The one implementation is `archive.ArchiveService`: the `telegram_archive` database that the existing
service's archive-sync fills (gxg_agents_v2). The connector never talks to Telegram and never opens a session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Kind = Literal["chat_forbidden", "flood_wait", "unavailable"]
# Archive completeness (archive-repository.ts) plus `flood_wait`: sync of the Connection deferred by Telegram
Reason = Literal["result_limit", "sync_gap", "not_synced", "flood_wait"]
SEARCH_LIMIT_MAX = 100  # telegramArchiveSearchInputSchema.limit
CHAT_REFS_MAX = 128  # telegramArchiveSearchInputSchema.chat_refs


class ServiceError(Exception):
    """`chat_forbidden`: not in the Connection's archive (scope); `flood_wait`: Telegram's delay in seconds;
    `unavailable`: the archive cannot be read. `detail` is an exception class name, never its text."""

    def __init__(self, kind: Kind, retry_after_seconds: float | None = None, detail: str | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds
        self.detail = detail


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
    """`items` in the archive's order; `more` = items exist past the page."""

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
        """chat_refs of the Connection's archive, which holds only its scope chats (CON-TG-06)."""

    async def search(self, connection: str, query: str, chat_refs: list[str], limit: int) -> Page: ...

    async def read_thread(self, connection: str, chat_ref: str, before_message_id: int | None, limit: int) -> Page:
        """Newest first, messages older than `before_message_id`."""

    async def collect(self, connection: str, chat_ref: str, after: tuple[int, int] | None, limit: int) -> Collected:
        """Raises `flood_wait` while Telegram defers the Connection's sync: the archive is not advancing."""

    async def ready(self) -> bool: ...
