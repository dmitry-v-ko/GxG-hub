"""The three read tools of `demo` and the checks every call goes through.

Order for every call: identity from the trusted channel (SEC-03, MCC-02) -> input schema ->
grants (allowed() narrows the backend query, check() on every item: GR-16; a named resource
without a grant is `not_found`: GR-17) -> backend. Errors are `GxgError.to_dict()` as a tool
error (MCC-05). One JSON audit line per call without query, body or tokens (SEC-18).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import jsonschema
import yaml
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.base import Tool, ToolResult
from pydantic import Field
from pydantic.json_schema import SkipJsonSchema

from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.grants import GrantResolver
from gxg_connector_kit.identity import IdentityVerifier, VerifiedIdentity
from gxg_connector_kit.redact import redact

from .backend import Backend, Note

ROOT = Path(__file__).resolve().parents[2]  # connectors/demo
MANIFEST = ROOT / "gxg-manifest.yaml"  # the one source of tool names, schemas and timeouts
DOMAIN, RTYPE = "demo", "board"
EXCERPT_MAX = 500
SEARCH_BUDGET_S = 15.0  # scan stops here with `source_timeout`, before the tool timeout (20 s, manifest)
audit = logging.getLogger("demo_connector.audit")
errors = logging.getLogger("demo_connector.errors")  # stack frames of internal errors; main() sends them to stderr

Result = tuple[dict[str, Any], dict[str, Any]]  # (structuredContent, audit fields)
Handler = Callable[[VerifiedIdentity, dict[str, Any]], Awaitable[Result]]

_CURSOR = re.compile(r"([0-9]{1,9})\.[0-9a-f]{64}")
NOT_FOUND = "not found"  # one message for "no grant" and "does not exist" (GR-17, SC-05)


def load_schema(rel: str) -> dict[str, Any]:
    """A schema file referenced by gxg-manifest.yaml, relative to connectors/demo."""
    # shortcut: a `../..` path is looked up by its tail in the nearest parent (repo checkout, /app in the image,
    # mutmut's copy under connectors/demo/mutants/); upgrade to package data with the template.
    tail = Path(*(part for part in Path(rel).parts if part != ".."))
    doc: dict[str, Any] = json.loads(next(d / tail for d in (ROOT, *ROOT.parents) if (d / tail).is_file()).read_bytes())
    return doc


def rid(connection: str, board: str) -> str:
    return f"{DOMAIN}:{connection}/{RTYPE}/{board}"


class DemoTool(Tool):
    """A FastMCP tool whose schemas are the files in connectors/demo/schemas/ (AC-M3-02)."""

    handler: Annotated[SkipJsonSchema[Handler], Field(exclude=True)]
    verify: Annotated[SkipJsonSchema[Callable[[Mapping[str, str]], VerifiedIdentity]], Field(exclude=True)]
    refresh: Annotated[SkipJsonSchema[Callable[[], None]], Field(exclude=True)]

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        entry: dict[str, Any] = {"request_id": str(uuid.uuid4()), "tool": self.name}
        try:
            # Headers only: arguments and _meta never reach the verifier (MCC-02).
            ident = self.verify(get_http_request().headers)
            entry |= {"request_id": ident.request_id, "tenant": ident.tenant_id, "principal": ident.principal}
            error = next(jsonschema.Draft202012Validator(self.parameters).iter_errors(arguments), None)
            if error is not None:
                where = "/" + "/".join(map(str, error.absolute_path))
                raise GxgError("invalid_input", "arguments do not match the input schema", details={"path": where})
            self.refresh()  # also refuses with temporarily_unavailable without a valid bundle (GR-12)
            content, fields = await self.handler(ident, arguments)
            entry |= fields | {"outcome": "ok"}
            return ToolResult(structured_content=content)
        except GxgError as exc:
            entry["outcome"] = exc.code
            return ToolResult(structured_content=exc.to_dict(), is_error=True)
        except Exception as exc:  # MCC-05 envelope; the text may hold provider secrets (SEC-18): class and frames only
            entry |= {"outcome": "internal_error", "error": type(exc).__name__}
            frames = "\n".join(f"{f.filename}:{f.lineno} in {f.name}" for f in traceback.extract_tb(exc.__traceback__))
            errors.error("internal_error %s %s\n%s", entry["request_id"], entry["error"], frames)
            return ToolResult(structured_content=GxgError("internal_error", "internal error").to_dict(), is_error=True)
        finally:
            entry["duration_ms"] = round((time.monotonic() - started) * 1000)
            audit.info(json.dumps(redact(entry)))


class Connector:
    def __init__(self, backend: Backend, grants: GrantResolver, *, search_budget_s: float = SEARCH_BUDGET_S) -> None:
        self.backend = backend
        self.grants = grants
        self.search_budget_s = search_budget_s
        self._cursor_key = secrets.token_bytes()

    def tools(self, verifier: IdentityVerifier, refresh: Callable[[], None]) -> list[DemoTool]:
        handlers: dict[str, Handler] = {
            "demo.boards.list": self.boards_list,
            "demo.notes.search": self.notes_search,
            "demo.note.get": self.note_get,
        }

        def ready() -> None:
            refresh()
            _ = self.grants.version  # GR-12: no valid bundle -> temporarily_unavailable before any handler

        manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        return [
            DemoTool(
                name=t["name"],
                description=load_schema(t["input_schema"])["description"],
                parameters=load_schema(t["input_schema"]),
                output_schema=load_schema(t["output_schema"]),
                timeout=float(t["limits"]["timeout_s"]),
                handler=handlers[t["name"]],
                verify=verifier.verify,
                refresh=ready,
            )
            for t in manifest["tools"]
        ]

    def _readable(self, ident: VerifiedIdentity) -> dict[str, tuple[str, str]]:
        """{board id: (RID, grant id)} the caller may read. allowed() first: nothing granted, no backend call."""
        tenant, principal = ident.tenant_id, ident.principal
        connection = self.backend.connections.get(tenant)
        if connection is None:
            return {}
        allowed = self.grants.allowed(tenant, principal, "read", connection, RTYPE)
        if not allowed.all_of_type and not allowed.rids:
            return {}
        out = {}
        for board in self.backend.list_boards(tenant):
            decision = self.grants.check(tenant, principal, "read", rid(connection, board.id))
            if decision.allowed:
                out[board.id] = (rid(connection, board.id), str(decision.grant_id))
        return out

    async def boards_list(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        readable = self._readable(ident)
        titles = {b.id: b.title for b in self.backend.boards.get(ident.tenant_id, {}).values()}
        boards = [{"id": b, "title": titles[b]} for b in sorted(readable)]
        return {"boards": boards}, {"grant_id": sorted({g for _, g in readable.values()})}

    async def notes_search(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        query: str = args["query"]
        requested: list[str] | None = args.get("board_ids")
        limit: int = args.get("limit", 20)
        readable = self._readable(ident)
        if requested is not None and any(b not in readable for b in requested):
            raise GxgError("not_found", NOT_FOUND)  # GR-17: a named board without a grant
        boards = sorted(set(requested) if requested is not None else readable)
        binding = json.dumps([ident.tenant_id, ident.principal, query, boards, limit]).encode()
        cursor = args.get("cursor")
        offset = 0 if cursor is None else self._offset(binding, cursor)
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
                decision = self.grants.check(ident.tenant_id, ident.principal, "read", readable[n.board_id][0])
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
            "next_cursor": self._cursor(binding, offset + limit) if more else None,
            "complete": not partial,
            "partial_reasons": partial,
            "source_refs": sorted({i["source_ref"] for i in items}),
        }
        return content, {"grant_id": sorted(grant_ids), "method": method}

    async def note_get(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        note = self.backend.get_note(ident.tenant_id, args["note_id"])  # board from the backend (GR-01, GR-03)
        if note is None:
            raise GxgError("not_found", NOT_FOUND)
        connection = self.backend.connections[ident.tenant_id]
        decision = self.grants.check(ident.tenant_id, ident.principal, "read", rid(connection, note.board_id))
        if not decision.allowed:
            raise GxgError("not_found", NOT_FOUND)
        content = {
            "id": note.id,
            "board_id": note.board_id,
            "title": note.title,
            "body": note.body,
            "updated_at": note.updated_at,
        }
        return content, {"grant_id": [decision.grant_id]}

    # Cursor: offset + HMAC-SHA256 over (tenant, principal, query, boards, limit) with a key of this process.
    def _cursor(self, binding: bytes, offset: int) -> str:
        return f"{offset}.{hmac.new(self._cursor_key, binding + str(offset).encode(), hashlib.sha256).hexdigest()}"

    def _offset(self, binding: bytes, cursor: str) -> int:
        m = _CURSOR.fullmatch(cursor)
        if m is None or not hmac.compare_digest(cursor, self._cursor(binding, int(m[1]))):
            raise GxgError("invalid_input", "cursor is not valid for this caller and query")
        return int(m[1])
