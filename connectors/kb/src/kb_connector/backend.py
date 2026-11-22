"""The KB port of the wrapper and its adapter to the KB Access Layer MCP endpoint.

Contract: docs/audits/kb-gap-audit.md «Контракт для адаптера KbBackend» (gxg_agents_v2@f1c4d6d). The wrapper calls
only three KB tools with the read key of the caller's connection: `kb_list_entity_types` (collection = entity type
key, Q1), `kb_search` (narrowed to the allowed collections, GR-16) and `kb_get`. One connection per tenant: URL,
read key, workspace (Q2); every row is checked against that workspace (G6). Readers get only published rows (Q3).
The port is async and bounded: the tool timeout (asyncio.wait_for in tools.py) cancels a hung call; a search
returns one page, whether more follows, and whether the KB's 50-row window cut it (G9). Errors become Connector
Contract codes; KB text never leaves this module (SEC-18).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from mcp import ClientSession
from mcp import types as mt
from mcp.client.streamable_http import streamable_http_client

from gxg_connector_kit.errors import GxgError

KB_TOOLS = frozenset({"kb_search", "kb_get", "kb_list_entity_types"})  # what the wrapper needs (G15)
# write and admin tools of the KB (audit table): a read key must not see any (G4, Q4)
WRITE_TOOLS = frozenset(
    {
        "kb_create",
        "kb_update",
        "kb_publish",
        "kb_relate",
        "kb_tag",
        "kb_untag",
        "kb_profile_update",
        "kb_file_sync",
        "kb_taxonomy_create_term",
        "kb_taxonomy_update_term",
        "kb_taxonomy_audit",
        "kb_profile_activate",
        "kb_profile_history",
    }
)
SEARCH_WINDOW = 50  # kb_search limit is at most 50 and has no offset (G9)
TYPES_TTL_S = 300.0  # entity types change only with KB migrations
CONNECT_TIMEOUT_S = 5.0
READY_TIMEOUT_S = 5.0  # one readiness probe per connection; it holds the connection's lock, so it must be bounded
MAX_RESPONSE_BYTES = 2_000_000  # G12: the SDK reads a JSON response whole
RETRY_AFTER_S = 5
_ID = re.compile(r"[1-9][0-9]{0,17}")  # kb.entities.id is a positive integer
_WORD = re.compile(r"\w{3}")


@dataclass(frozen=True)
class Document:
    id: str
    collection: str
    title: str
    content: str
    updated_at: str


@dataclass(frozen=True)
class Connection:
    connection_id: str
    url: str
    workspace_id: str
    key: str  # the read key of the wrapper for this connection; never logged


def load_connections(path: Path) -> dict[str, Connection]:
    """KB_CONNECTIONS_FILE: {tenant: {connection_id, url, workspace_id, key_file}}; the key comes from key_file."""
    doc = json.loads(path.read_text())
    return {
        tenant: Connection(c["connection_id"], c["url"], c["workspace_id"], Path(c["key_file"]).read_text().strip())
        for tenant, c in doc.items()
    }


HttpClient = Callable[[Connection], httpx2.AsyncClient]


def http_client(conn: Connection) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {conn.key}"},
        timeout=httpx2.Timeout(None, connect=CONNECT_TIMEOUT_S),  # the total is the tool timeout of the manifest
    )  # redirects are not followed (the httpx default): the KB URL is configuration only (SEC-15)


class ContractError(RuntimeError):
    """The KB answered in a form the contract does not have (G10): internal_error. No message: the audit logs
    the class name only, and KB text never leaves this module."""


class ToolFailed(ContractError):
    """The KB answered a tools/call with isError. Its text is unknown and never passed on."""


class KeyRefused(RuntimeError):
    """The KB refused the wrapper's own key (HTTP 401/403): internal_error, not the caller's unauthenticated."""


class Backend:
    """The port tools.py uses: connections (tenant -> connection id), calls, ready, list_collections,
    get_document, search."""

    def __init__(self, connections: dict[str, Connection], client: HttpClient = http_client) -> None:
        self._conns = connections
        self.connections = {t: c.connection_id for t, c in connections.items()}
        self._client = client
        self._locks = {t: asyncio.Semaphore(1) for t in connections}  # the KB is sequential only (G13)
        self._types: dict[str, tuple[float, dict[int, str]]] = {}
        self.calls: Counter[str] = Counter()  # KB tools/call per tool name (audit `kb_calls`)

    async def _session(self, tenant: str, use: Callable[[ClientSession], Any]) -> Any:
        conn, statuses = self._conns[tenant], []

        async def seen(response: httpx2.Response) -> None:
            statuses.append(response)
            if too_big(response.headers):
                raise ContractError

        async with self._locks[tenant]:
            try:
                async with self._client(conn) as http:
                    http.event_hooks["response"].append(seen)  # the MCP client hides HTTP statuses
                    async with streamable_http_client(conn.url, http_client=http) as (r, w), ClientSession(r, w) as s:
                        await s.initialize()
                        return await use(s)
            except Exception as exc:
                raise _classify(exc, statuses) from None

    async def _call(self, tenant: str, tool: str, args: dict[str, Any]) -> Any:
        """The JSON value of a KB tool result. A tool error -> ContractError (its text is not passed on)."""
        self.calls[tool] += 1

        async def use(s: ClientSession) -> Any:
            return await s.call_tool(tool, args)

        result = await self._session(tenant, use)
        if result.is_error:
            raise ToolFailed
        try:  # the KB has no outputSchema: one text item with JSON (G10)
            first = result.content[0]
            assert isinstance(first, mt.TextContent)  # noqa: S101 - caught below as contract drift
            return json.loads(first.text)
        except (IndexError, AssertionError, ValueError):
            raise ContractError from None

    async def ready(self) -> bool:
        """For /readyz (MCC-09): every connection lists the three KB tools and no write tool (G4, G15, Q4)."""
        if not self._conns:
            return False

        async def names(s: ClientSession) -> set[str]:
            return {t.name for t in (await s.list_tools()).tools}

        for tenant in self._conns:
            try:  # bounded: a KB that accepts and never answers must not hang /readyz or hold the lock (M1)
                listed = await asyncio.wait_for(self._session(tenant, names), READY_TIMEOUT_S)
            except Exception:
                return False
            if not KB_TOOLS <= listed or listed & WRITE_TOOLS:
                return False
        return True

    async def _type_keys(self, tenant: str) -> dict[int, str]:
        cached = self._types.get(tenant)
        if cached is not None and time.monotonic() - cached[0] < TYPES_TTL_S:
            return cached[1]
        rows = _rows(await self._call(tenant, "kb_list_entity_types", {}))
        try:  # the KB lists active types; an inactive row is skipped all the same
            keys = {int(t["id"]): str(t["key"]) for t in rows if t["is_active"] is True}
        except (KeyError, TypeError, ValueError):
            raise ContractError from None
        self._types[tenant] = (time.monotonic(), keys)
        return keys

    async def list_collections(self, tenant: str) -> list[str]:
        if tenant not in self._conns:
            return []
        return sorted(set((await self._type_keys(tenant)).values()))

    def _document(self, tenant: str, row: Any, keys: dict[int, str]) -> Document | None:
        """A row the caller's connection may see as a document, or None: another workspace (GR-02, G6), not
        published or archived (Q3), an unknown entity type."""
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            raise ContractError
        if row.get("workspace_id") != self._conns[tenant].workspace_id:
            return None
        if row.get("status") != "published" or row.get("archived_at") is not None:
            return None
        collection = keys.get(row.get("entity_type_id"))  # type: ignore[arg-type]
        if collection is None:
            return None
        return Document(
            id=str(row["id"]),
            collection=collection,
            title=str(row.get("title") or ""),
            content=str(row.get("body_md") or row.get("summary") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    async def get_document(self, tenant: str, document_id: str) -> Document | None:
        """The document with its real collection from the KB (GR-01, GR-03), or None."""
        if tenant not in self._conns or not _ID.fullmatch(document_id):
            return None  # no KB call for an id the KB cannot have
        keys = await self._type_keys(tenant)
        try:
            row = await self._call(tenant, "kb_get", {"id": int(document_id)})
        except (
            ToolFailed
        ):  # the KB's "not found" form is unknown (audit G1): every tool error looks like a miss (SC-05)
            return None
        if isinstance(row, dict) and "item" in row:
            row = row["item"]
        return None if row is None else self._document(tenant, row, keys)

    async def search(
        self, tenant: str, collections: list[str], query: str, *, offset: int, limit: int
    ) -> tuple[list[Document], bool, bool]:
        """(page, more after it, complete). Only `collections`; the KB ranks. The KB has no offset and at most 50
        rows (G9): the wrapper always asks for the whole window and pages over the rows it keeps (published, own
        workspace), so dropped rows do not shorten a page. complete is False when the window was full and the
        page could not be filled from it: the KB may have more."""
        if not collections or tenant not in self._conns or not _WORD.search(query):
            return [], False, True  # G16: without a word of 3+ characters kb_search returns arbitrary rows
        keys = await self._type_keys(tenant)
        args = {"query": query, "entity_types": collections, "limit": SEARCH_WINDOW, "include_body": True}
        rows = _rows(await self._call(tenant, "kb_search", args))
        docs = [d for d in (self._document(tenant, r, keys) for r in rows) if d is not None]
        more = len(docs) > offset + limit
        return docs[offset : offset + limit], more, more or len(rows) < SEARCH_WINDOW


def too_big(headers: httpx2.Headers) -> bool:
    """G12: an answer above MAX_RESPONSE_BYTES by its Content-Length.
    shortcut: a chunked answer without Content-Length is not bounded here."""
    return int(headers.get("content-length") or 0) > MAX_RESPONSE_BYTES


def _rows(data: Any) -> list[Any]:
    """A list result in any of the forms KB clients accept: a list, {items}, {entities} (G10)."""
    if isinstance(data, dict):
        data = data.get("items", data.get("entities"))
    if not isinstance(data, list):
        raise ContractError
    return data


def _classify(exc: Exception, statuses: list[httpx2.Response]) -> Exception:
    """An exception of a KB call as a Connector Contract error (audit: «Ошибки → Connector Contract»)."""
    for response in statuses:
        status = response.status_code
        if status == 429:
            retry = response.headers.get("retry-after", "")
            return GxgError(
                "rate_limited", "KB rate limit", retry_after_s=int(retry) if retry.isdigit() else RETRY_AFTER_S
            )
        if status >= 500:
            return GxgError("temporarily_unavailable", "KB is not available")
        if status in (401, 403):
            return KeyRefused()
    stack: list[BaseException] = [exc]  # exception groups and chains are trees: no cycles
    while stack:
        e = stack.pop()
        if isinstance(e, GxgError | ContractError):
            return e
        if isinstance(e, httpx2.TransportError):  # connect, read and other timeouts, refused connections
            return GxgError("temporarily_unavailable", "KB is not available")
        stack += list(getattr(e, "exceptions", ())) + [c for c in (e.__cause__, e.__context__) if c is not None]
    return exc
