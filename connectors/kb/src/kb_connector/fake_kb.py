"""fake-KB: an MCP server with the client contract of the KB Access Layer, for tests and the dev stand only.

The contract is the one of docs/audits/kb-gap-audit.md «Контракт для адаптера KbBackend» (gxg_agents_v2@f1c4d6d:
the clients, the Mastra in-process read tools, the kb.entities columns). The KB server itself (`kb_access`) is not
in that repository, so this fake reproduces the documented behaviour, including what the wrapper must defend against:
`Authorization: Bearer <key>` with a key per role (read < write < admin) bound to a workspace (Q2); search hides only
archived rows, so drafts are visible (G8); `kb_get` does not check the workspace (G6, worst case); results have no
outputSchema and come back as JSON text (G10); write tools are hidden from a read key (Q4).
Not shipped in the production image (connectors/kb/Dockerfile removes this file unless KEEP_FAKE_KB=1).

  python -m kb_connector.fake_kb        FAKE_KB_KEYS_FILE={"<key>": {"workspace_id": "...", "role": "read"}}, port 8080
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from mcp import types as mt
from starlette.types import ASGIApp, Receive, Scope, Send

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "kb.json"
WRITE_TOOLS = frozenset({"kb_create"})  # the fake has one write tool; the real KB has more (audit table)
SEARCH_MAX = 50  # the in-process adapter cuts `limit` to 1…50 (audit: kb_search)
Role = Literal["read", "write", "admin"]


@dataclass(frozen=True)
class Key:
    workspace_id: str
    role: Role


def _json(value: Any) -> ToolResult:
    """The KB answers JSON text without structuredContent (no outputSchema, G10)."""
    return ToolResult(content=[mt.TextContent(type="text", text=json.dumps(value))])


class FakeKb:
    def __init__(self, keys: dict[str, Key], path: Path = FIXTURES) -> None:
        doc = json.loads(path.read_bytes())
        self.keys = keys
        self.entity_types: list[dict[str, Any]] = doc["entity_types"]
        self.entities: list[dict[str, Any]] = doc["entities"]
        self.calls: Counter[str] = Counter()  # tools/call per KB tool (tests)
        self.http_status: int | None = None  # tests: every HTTP request answers this status (KB down, 429, 401)
        self.answer: Any = None  # tests: when set, every tools/call returns this JSON (contract drift)
        self.delay_s = 0.0  # tests: every HTTP request waits this long (a slow KB)
        self.retry_after: str | None = "7"  # tests: the Retry-After of a 429
        self.active = self.max_active = 0  # tests: concurrent HTTP requests now and at most
        self.text: str | None = None  # tests: every tools/call answers this raw text (not JSON)
        self.tool_error = False  # tests: every tools/call is a tool error with an internal message
        self.requests: list[tuple[str, str, dict[str, Any]]] = []  # tests: (tool, key's workspace, arguments)
        self.mcp = FastMCP("gxg-kb")
        self.mcp.add_middleware(_RoleTools(self))
        for fn in (self.kb_list_entity_types, self.kb_search, self.kb_get, self.kb_create):
            self.mcp.add_tool(Tool.from_function(fn))
        self.http = self.mcp.http_app(stateless_http=True, json_response=True)

    def key(self) -> Key:
        auth = get_http_request().headers.get("authorization", "")
        return self.keys[auth.removeprefix("Bearer ")]  # the ASGI guard has refused unknown keys already

    def _reply(self, name: str, value: Any, args: dict[str, Any] | None = None) -> ToolResult:
        self.calls[name] += 1
        self.requests.append((name, self.key().workspace_id, args or {}))
        if self.tool_error:
            raise ToolError("KB internal failure: kb-secret-text")
        if self.text is not None:
            return ToolResult(content=[mt.TextContent(type="text", text=self.text)])
        return _json(self.answer if self.answer is not None else value)

    def kb_list_entity_types(self) -> ToolResult:
        """Active entity types: id, key, label, is_active (global, no workspace)."""
        return self._reply("kb_list_entity_types", [t for t in self.entity_types if t["is_active"]])

    def kb_search(
        self, query: str, limit: int = 10, entity_types: list[str] | None = None, include_body: bool = False
    ) -> ToolResult:
        """kb.entities of the key's workspace, archived excluded (drafts are not), by entity type keys."""
        ws, q = self.key().workspace_id, query.casefold()
        type_ids = {t["id"] for t in self.entity_types if entity_types is None or t["key"] in entity_types}
        rows = [
            {k: v for k, v in e.items() if include_body or k != "body_md"}
            for e in self.entities
            if e["workspace_id"] == ws
            and e["entity_type_id"] in type_ids
            and e["status"] != "archived"
            and q in f"{e['title']}\n{e['summary']}\n{e['body_md']}".casefold()
        ]
        args = {"query": query, "limit": limit, "entity_types": entity_types, "include_body": include_body}
        return self._reply("kb_search", rows[: max(1, min(limit, SEARCH_MAX))], args)

    def kb_get(self, id: int) -> ToolResult:
        """One kb.entities row by id, or null. No workspace check (worst case of the audit, G6)."""
        return self._reply("kb_get", next((e for e in self.entities if e["id"] == id), None), {"id": id})

    def kb_create(self, title: str, summary: str, body_md: str, entity_type_id: int) -> ToolResult:
        """Write tool: a new draft in the key's workspace. Hidden from and refused to a read key (Q4)."""
        row = {
            "id": max(e["id"] for e in self.entities) + 1,
            "workspace_id": self.key().workspace_id,
            "entity_type_id": entity_type_id,
            "title": title,
            "summary": summary,
            "body_md": body_md,
            "status": "draft",
            "archived_at": None,
            "updated_at": "2026-11-22T00:00:00Z",
        }
        self.entities.append(row)
        return self._reply("kb_create", {"id": row["id"]})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI: refuse an unknown key with 401 before MCP, or answer the test status."""
        if scope["type"] != "http":
            await self.http(scope, receive, send)
            return
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self._serve(scope, receive, send)
        finally:
            self.active -= 1

    async def _serve(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        key = dict(scope["headers"]).get(b"authorization", b"").decode().removeprefix("Bearer ")
        status = self.http_status or (None if key in self.keys else 401)
        if status is not None:
            extra = [(b"retry-after", self.retry_after.encode())] if status == 429 and self.retry_after else []
            await send({"type": "http.response.start", "status": status, "headers": extra})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.http(scope, receive, send)


class _RoleTools(Middleware):
    """Write tools exist only for write and admin keys: not listed and not callable with a read key (Q4)."""

    def __init__(self, kb: FakeKb) -> None:
        self.kb = kb

    async def on_list_tools(
        self, context: MiddlewareContext[mt.ListToolsRequest], call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]]
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [t for t in tools if t.name not in WRITE_TOOLS or self.kb.key().role != "read"]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: Callable[[MiddlewareContext[mt.CallToolRequestParams]], Awaitable[ToolResult]],
    ) -> ToolResult:
        if context.message.name in WRITE_TOOLS and self.kb.key().role == "read":
            raise ToolError("forbidden: this key may not write")
        return await call_next(context)


def main() -> None:  # pragma: no cover - the stand runs it; tests use FakeKb in process
    raw = json.loads(Path(os.environ["FAKE_KB_KEYS_FILE"]).read_text())
    kb = FakeKb({k: Key(v["workspace_id"], v["role"]) for k, v in raw.items()})
    app: ASGIApp = kb
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning", lifespan="on")  # noqa: S104


if __name__ == "__main__":
    main()
