"""`mcp` process (port 8091): tools `effects.status` and `effects.cancel` for agents, via ContextForge.

Identity comes only from the trusted channel (SEC-03, `IdentityVerifier` of the kit), never from tool
arguments. The process has no database access: it calls the `api` with its own service JWT (kind
`approval_mcp`) and `on_behalf_of` = the verified agent (AS-11a). Another tenant's or requester's intent
is `not_found`, byte for byte like a missing one.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolResult, TextContent

from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.identity import IdentityVerifier


class ErrorResult(ToolResult):
    """MCC-05: a tool error is `isError: true` with the error envelope as `structuredContent`."""

    def to_mcp_result(self) -> CallToolResult:
        return CallToolResult(content=self.content, structuredContent=self.structured_content, isError=True)


def _result(doc: dict[str, Any], *, error: bool) -> ToolResult:
    text = [TextContent(type="text", text=json.dumps(doc, separators=(",", ":"), sort_keys=True))]
    return ErrorResult(content=text, structured_content=doc) if error else ToolResult(text, doc)


def create_server(
    api: httpx.AsyncClient,
    token: Callable[[], str],
    identity: IdentityVerifier,
    headers: Callable[[], Mapping[str, str]] = lambda: get_http_headers(include_all=True),
) -> FastMCP:
    """`api`: client with base_url of the `api` process; `token()`: a fresh service JWT for it."""
    server = FastMCP("gxg-approval")

    async def call(method: str, path: str, **kw: Any) -> dict[str, Any]:
        r = await api.request(method, path, headers={"Authorization": f"Bearer {token()}"}, **kw)
        doc = r.json()
        if r.status_code != 200:
            raise GxgError(
                doc["code"], doc["message"], retryable=doc["retryable"], retry_after_s=doc.get("retry_after_s")
            )
        return dict(doc)

    async def run(intent_id: str, cancel: bool) -> ToolResult:
        try:
            who = identity.verify(headers())
            scope = {"tenant_id": who.tenant_id, "principal": who.principal}
            doc = await call("GET", f"/v1/intents/{intent_id}", params=scope)
            if cancel:
                body = {"on_behalf_of": scope, "expected_version": doc["version"]}
                doc = await call("POST", f"/v1/intents/{intent_id}/cancel", json=body)
        except GxgError as exc:
            return _result(exc.to_dict(), error=True)
        except httpx.HTTPError:
            return _result(GxgError("temporarily_unavailable", "approval service is unavailable").to_dict(), error=True)
        return _result(doc, error=False)

    @server.tool(
        name="effects.status",
        description="Status of your own intent (no args, preview or tokens).",
        annotations={"readOnlyHint": True},
    )
    async def status(intent_id: str) -> ToolResult:
        return await run(intent_id, cancel=False)

    @server.tool(
        name="effects.cancel",
        description="Cancel your own intent before it is reserved for execution.",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    async def cancel(intent_id: str) -> ToolResult:
        return await run(intent_id, cancel=True)

    return server
