"""`TelegramService` over the `telegram_archive` database of the existing Telegram service (ADR-0004).

Read-only role `gxg_telegram_reader` (archive/reader_role.sql): column-level SELECT, read-only transactions.
The SQL follows `PostgresTelegramArchiveRepository` of gxg_agents_v2 @ f1c4d6d (archive-repository.ts):
Russian and English `websearch_to_tsquery`, `ts_rank`, newest first, `limit + 1` for `result_limit`, chat
progress for completeness. Scope: the archive holds only the Connection's scope chats; the service's trigger
`purge_out_of_scope` deletes a chat in the same transaction that takes it out of scope (CON-TG-06), so a chat
missing here is `chat_forbidden`. Peer IDs are only join keys: no query returns one.
shortcut: one connection per call, no pool; add psycopg_pool when the call rate needs it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

from .service import Collected, Message, Page, Reason, ServiceError

T = TypeVar("T")
APP = "gxg-telegram"  # application_name of tool queries; readiness uses APP + "-ready" (counted apart on the stand)
# SQL fragments are constants; values are always bound parameters (S608 below)
_COLUMNS = """c.chat_ref, m.message_id, m.sent_at, m.edited_at, m.text, m.caption, m.reply_to, m.media_type"""
_FROM = """telegram_archive.messages m
  JOIN telegram_archive.chats c ON c.connection_id = m.connection_id AND c.peer_id = m.peer_id"""


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _message(row: dict[str, Any]) -> Message:
    return Message(
        row["chat_ref"],
        int(row["message_id"]),
        str(_iso(row["sent_at"])),
        _iso(row["edited_at"]),
        row["text"],
        row["caption"],
        row["reply_to"],
        row["media_type"],
    )


class ArchiveService:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def _run(self, work: Callable[[psycopg.AsyncCursor[dict[str, Any]]], Awaitable[T]], app: str = APP) -> T:
        try:
            async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True, application_name=app) as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    return await work(cur)
        except psycopg.Error as exc:  # the text may quote values (SEC-18): the class name only
            raise ServiceError("unavailable", detail=type(exc).__name__) from None

    @staticmethod
    async def _state(
        cur: psycopg.AsyncCursor[dict[str, Any]], connection: str, refs: list[str]
    ) -> tuple[list[Reason], float | None]:
        """Completeness of `refs` (each must be in the archive) and the remaining Telegram flood wait, if any."""
        await cur.execute(
            """SELECT chat_ref, state, gap FROM telegram_archive.chats
            WHERE connection_id = %s AND chat_ref = ANY(%s)""",
            (connection, refs),
        )
        rows = await cur.fetchall()
        if len(rows) != len(set(refs)):
            raise ServiceError("chat_forbidden")
        reasons: set[Reason] = set()
        for r in rows:
            if r["state"] != "synced":
                reasons.add("not_synced")
            if r["gap"]:
                reasons.add("sync_gap")
        await cur.execute(
            """SELECT extract(epoch FROM retry_after - clock_timestamp())::float8 AS wait
            FROM telegram_archive.connection_state WHERE connection_id = %s AND retry_after > clock_timestamp()""",
            (connection,),
        )
        deferred = await cur.fetchone()
        if deferred is not None:
            reasons.add("flood_wait")
        return sorted(reasons), None if deferred is None else float(deferred["wait"])

    async def scope_chats(self, connection: str) -> list[str]:
        async def work(cur: psycopg.AsyncCursor[dict[str, Any]]) -> list[str]:
            await cur.execute(
                "SELECT chat_ref FROM telegram_archive.chats WHERE connection_id = %s ORDER BY chat_ref", (connection,)
            )
            return [r["chat_ref"] for r in await cur.fetchall()]

        return await self._run(work)

    async def search(self, connection: str, query: str, chat_refs: list[str], limit: int) -> Page:
        async def work(cur: psycopg.AsyncCursor[dict[str, Any]]) -> Page:
            reasons, _ = await self._state(cur, connection, chat_refs)
            await cur.execute(
                f"""WITH q AS (SELECT websearch_to_tsquery('pg_catalog.russian'::regconfig, %(q)s)
                    || websearch_to_tsquery('pg_catalog.english'::regconfig, %(q)s) AS query)
                SELECT {_COLUMNS} FROM q, {_FROM}
                WHERE m.connection_id = %(c)s AND c.chat_ref = ANY(%(refs)s) AND m.tsv @@ q.query
                ORDER BY ts_rank(m.tsv, q.query) DESC, m.sent_at DESC, m.message_id DESC
                LIMIT %(n)s""",  # noqa: S608 - constant fragments; every value is a bound parameter
                {"q": query, "c": connection, "refs": chat_refs, "n": limit + 1},
            )
            rows = await cur.fetchall()
            more = len(rows) > limit
            limited: list[Reason] = ["result_limit"] if more else []
            return Page([_message(r) for r in rows[:limit]], more, sorted({*reasons, *limited}))

        return await self._run(work)

    async def read_thread(self, connection: str, chat_ref: str, before_message_id: int | None, limit: int) -> Page:
        async def work(cur: psycopg.AsyncCursor[dict[str, Any]]) -> Page:
            reasons, _ = await self._state(cur, connection, [chat_ref])
            await cur.execute(
                f"""SELECT {_COLUMNS} FROM {_FROM}
                WHERE m.connection_id = %(c)s AND c.chat_ref = %(r)s AND (%(b)s::bigint IS NULL OR m.message_id < %(b)s)
                ORDER BY m.message_id DESC LIMIT %(n)s""",  # noqa: S608
                {"c": connection, "r": chat_ref, "b": before_message_id, "n": limit + 1},
            )
            rows = await cur.fetchall()
            return Page([_message(r) for r in rows[:limit]], len(rows) > limit, reasons)

        return await self._run(work)

    async def collect(self, connection: str, chat_ref: str, after: tuple[int, int] | None, limit: int) -> Collected:
        async def work(cur: psycopg.AsyncCursor[dict[str, Any]]) -> Collected:
            reasons, wait = await self._state(cur, connection, [chat_ref])
            if wait is not None:  # the archive is not advancing; Signals retries after Telegram's delay
                raise ServiceError("flood_wait", wait)
            await cur.execute(
                f"""SELECT {_COLUMNS}, (extract(epoch FROM m.archived_at) * 1000000)::bigint AS archived_us
                FROM {_FROM} WHERE m.connection_id = %(c)s AND c.chat_ref = %(r)s
                  AND (%(a)s::bigint IS NULL
                    OR ((extract(epoch FROM m.archived_at) * 1000000)::bigint, m.message_id) > (%(a)s, %(i)s))
                ORDER BY archived_us, m.message_id LIMIT %(n)s""",  # noqa: S608
                {"c": connection, "r": chat_ref, "a": after and after[0], "i": after and after[1], "n": limit + 1},
            )
            rows = await cur.fetchall()
            page = rows[:limit]
            return Collected(
                [_message(r) for r in page],
                [(int(r["archived_us"]), int(r["message_id"])) for r in page],
                len(rows) > limit,
                reasons,
            )

        return await self._run(work)

    async def ready(self) -> bool:
        async def work(cur: psycopg.AsyncCursor[dict[str, Any]]) -> bool:
            await cur.execute("SELECT 1 FROM telegram_archive.chats LIMIT 1")
            return True

        try:
            return await self._run(work, APP + "-ready")
        except ServiceError:
            return False
