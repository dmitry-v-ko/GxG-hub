"""The tools of `demo` (three reads; `demo.message.propose` when effects are configured, effects.py): the domain part
on top of the kit's call pipeline (gxg_connector_kit.mcp).

The kit runs, for every call: identity from the trusted channel (SEC-03, MCC-02) -> input schema -> valid bundle ->
resource hook (allowed() before any backend call; `demo.note.get` resolves the board by the backend and check()s it;
a named resource without a grant is `not_found`: GR-17) -> the handler below with its `Access` -> output schema and
size -> envelope and one redacted audit line (MCC-05, SEC-18). Handlers narrow the backend query by `Access` and
check() every item again (GR-16).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gxg_connector_kit import mcp
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.grants import GrantResolver
from gxg_connector_kit.identity import IdentityVerifier, VerifiedIdentity
from gxg_connector_kit.mcp import NOT_FOUND, Access, Handler, Result

from .backend import Backend
from .data import Note

if TYPE_CHECKING:
    from .effects import Effects

ROOT = Path(__file__).resolve().parents[2]  # connectors/demo
MANIFEST = ROOT / "gxg-manifest.yaml"  # the one source of tool names, schemas and timeouts
DOMAIN, RTYPE = "demo", "board"
EXCERPT_MAX = 500
SEARCH_BUDGET_S = 15.0  # scan stops here with `source_timeout`, before the tool timeout (20 s, manifest)
logs = mcp.Logs.of("demo_connector")  # main() sends .errors (stack frames) to stderr, the rest to stdout


def rid(connection: str, board: str) -> str:
    return f"{DOMAIN}:{connection}/{RTYPE}/{board}"


class Connector:
    """`gxg_connector_kit.mcp.Domain` of `demo`."""

    connector_id, rtype = "demo", RTYPE

    def __init__(self, backend: Backend, grants: GrantResolver, *, search_budget_s: float = SEARCH_BUDGET_S) -> None:
        self.backend = backend
        self.grants = grants
        self.search_budget_s = search_budget_s
        self._cursor = mcp.Cursor()
        self.effects: Effects | None = None  # stage C, set by server.build when configured; else no propose tool

    def tools(self, verifier: IdentityVerifier, refresh: Callable[[], None]) -> list[mcp.GxgTool]:
        # `demo.message.propose` keeps the M8 order: effects.py resolves the board and checks `propose` itself
        # (GR-17, its own messages); moving it onto gxg_connector_kit.effects is in docs/backlog.md (M1.1).
        return [
            t.model_copy(update={"hook": self._propose_access}) if t.name == "demo.message.propose" else t
            for t in mcp.tools(self, MANIFEST, self.grants, verifier, refresh, logs, ROOT)
        ]

    async def _propose_access(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Access:
        return Access(ident, "propose", self.connection(ident.tenant_id))

    def handlers(self) -> Mapping[str, Handler]:
        out: dict[str, Handler] = {
            "demo.boards.list": self.boards_list,
            "demo.notes.search": self.notes_search,
            "demo.note.get": self.note_get,
        }
        if self.effects is not None:
            out["demo.message.propose"] = self.message_propose
        return out

    def connection(self, tenant: str) -> str | None:
        return self.backend.connections.get(tenant)

    async def resolve(self, tool: str, access: Access, args: dict[str, Any]) -> tuple[str, Any] | None:
        """`demo.note.get`: the board from the backend, not from an argument (GR-01, GR-03)."""
        note = self.backend.get_note(access.ident.tenant_id, args["note_id"])
        return None if note is None or access.connection is None else (rid(access.connection, note.board_id), note)

    def _readable(self, access: Access) -> dict[str, tuple[str, str]]:
        """{board id: (RID, grant id)} the caller may read. Nothing granted: no backend call (GR-16)."""
        if access.nothing or access.connection is None:
            return {}
        out = {}
        for board in self.backend.list_boards(access.ident.tenant_id):
            decision = access.check(rid(access.connection, board.id))
            if decision.allowed:
                out[board.id] = (rid(access.connection, board.id), str(decision.grant_id))
        return out

    async def boards_list(self, access: Access, args: dict[str, Any]) -> Result:
        readable = self._readable(access)
        titles = {b.id: b.title for b in self.backend.boards.get(access.ident.tenant_id, {}).values()}
        boards = [{"id": b, "title": titles[b]} for b in sorted(readable)]
        return {"boards": boards}, {"grant_id": sorted({g for _, g in readable.values()})}

    async def notes_search(self, access: Access, args: dict[str, Any]) -> Result:
        ident = access.ident
        query: str = args["query"]
        requested: list[str] | None = args.get("board_ids")
        limit: int = args.get("limit", 20)
        readable = self._readable(access)
        if requested is not None and any(b not in readable for b in requested):
            raise GxgError("not_found", NOT_FOUND)  # GR-17: a named board without a grant
        boards = sorted(set(requested) if requested is not None else readable)
        binding = json.dumps([ident.tenant_id, ident.principal, query, boards, limit]).encode()
        offset = self._cursor.offset(binding, args.get("cursor"))
        # GR-16, MCC-06: nothing readable, no backend call (an empty filter may mean "all")
        deadline = time.monotonic() + self.search_budget_s
        notes, method, timed_out = (
            await self.backend.search(ident.tenant_id, boards, query, deadline=deadline)
            if boards
            else ([], None, False)
        )
        # GR-16: every item is checked again, whatever the backend returned.
        grant_ids: set[str] = set()
        visible: list[Note] = []
        for n in notes:
            if n.board_id in boards:
                decision = access.check(readable[n.board_id][0])
                if decision.allowed:
                    visible.append(n)
                    grant_ids.add(str(decision.grant_id))
        page = visible[offset : offset + limit]
        more = offset + limit < len(visible)
        items = [
            {"id": n.id, "title": n.title, "excerpt": n.body[:EXCERPT_MAX], "source_ref": readable[n.board_id][0]}
            for n in page
        ]
        partial = ["source_timeout"] if timed_out else []
        content = {
            "items": items,
            "next_cursor": self._cursor.make(binding, offset + limit) if more else None,
            "complete": not partial,
            "partial_reasons": partial,
            "source_refs": sorted({i["source_ref"] for i in items}),
        }
        return content, {"grant_id": sorted(grant_ids), "method": method}

    async def note_get(self, access: Access, args: dict[str, Any]) -> Result:
        note: Note = access.target  # found by resolve(), allowed by the kit's check() of its board
        content = {
            "id": note.id,
            "board_id": note.board_id,
            "title": note.title,
            "body": note.body,
            "updated_at": note.updated_at,
        }
        return content, {}

    async def message_propose(self, access: Access, args: dict[str, Any]) -> Result:
        """EFF-04…EFF-08 by demo's own effects.py, which also resolves the board and checks `propose` (GR-17)."""
        if self.effects is None:
            raise RuntimeError("effects are not enabled")
        return await self.effects.propose(access.ident, args)
