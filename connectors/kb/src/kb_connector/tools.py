"""The three read tools of the KB wrapper and the checks every call goes through.

Order for every call (as in `demo`): identity from the trusted channel (SEC-03, MCC-02) -> input schema ->
bundle -> grants -> KB. Grants: a named collection is checked before any KB call and without a grant is
`not_found` (GR-17); otherwise the KB query is narrowed to the allowed collections (GR-16) and every item is
checked again. A document of another collection or tenant is `not_found`, byte for byte as a missing one
(SC-05). Errors are `GxgError.to_dict()` as a tool error (MCC-05); one JSON audit line per call without query,
content or tokens (SEC-18).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import jsonschema
import yaml
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.base import Tool, ToolResult
from pydantic import Field
from pydantic.json_schema import SkipJsonSchema

from gxg_connector_kit import TokenError
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.grants import GrantResolver
from gxg_connector_kit.identity import IdentityVerifier, VerifiedIdentity
from gxg_connector_kit.redact import redact

from .backend import Backend, Document

ROOT = Path(__file__).resolve().parents[2]  # connectors/kb
MANIFEST = ROOT / "gxg-manifest.yaml"  # the one source of tool names, schemas and timeouts
DOMAIN, RTYPE = "kb", "collection"
EXCERPT_MAX = 500
TITLE_MAX, CONTENT_MAX = 300, 20000  # the output schemas' maxLength: KB data is cut to them (MCC-04)
audit = logging.getLogger("kb_connector.audit")
errors = logging.getLogger("kb_connector.errors")  # stack frames of internal errors; main() sends them to stderr

Result = tuple[dict[str, Any], dict[str, Any]]  # (structuredContent, audit fields)
Handler = Callable[[VerifiedIdentity, dict[str, Any]], Awaitable[Result]]

_CURSOR = re.compile(r"([0-9]{1,9})\.[0-9a-f]{64}")
NOT_FOUND = "not found"  # one message for "no grant" and "does not exist" (GR-17, SC-05)


def load_schema(rel: str) -> dict[str, Any]:
    """A schema file referenced by gxg-manifest.yaml, relative to connectors/kb."""
    # shortcut: a `../..` path is looked up by its tail in the nearest parent (as in demo); package data later.
    tail = Path(*(part for part in Path(rel).parts if part != ".."))
    doc: dict[str, Any] = json.loads(next(d / tail for d in (ROOT, *ROOT.parents) if (d / tail).is_file()).read_bytes())
    return doc


def rid(connection: str, collection: str) -> str:
    return f"{DOMAIN}:{connection}/{RTYPE}/{collection}"


# shortcut: a copy of demo_connector.tools.DemoTool.run (second connector); move it into gxg-connector-kit
# together with the toolkit template (mcp-toolkit-v1), when a third connector needs it.
class KbTool(Tool):
    """A FastMCP tool whose schemas are the files in connectors/kb/schemas/."""

    handler: Annotated[SkipJsonSchema[Handler], Field(exclude=True)]
    verify: Annotated[SkipJsonSchema[Callable[..., VerifiedIdentity]], Field(exclude=True)]  # IdentityVerifier.verify
    ready: Annotated[SkipJsonSchema[Callable[[], str]], Field(exclude=True)]  # the loaded bundle_version
    # KB calls so far (Backend.calls); the audit line records how many this call made (MCC-08, zero on refusals).
    # shortcut: a process-wide counter, exact for sequential calls only; per request with the real KB client.
    kb_calls: Annotated[SkipJsonSchema[Callable[[], int]], Field(exclude=True)]

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        started, calls = time.monotonic(), self.kb_calls()
        entry: dict[str, Any] = {"request_id": str(uuid.uuid4()), "tool": self.name}
        try:
            ident = self.verify(get_http_request().headers, tool=self.name, arguments=arguments)
            entry |= {"request_id": ident.request_id, "tenant": ident.tenant_id, "principal": ident.principal}
            error = next(jsonschema.Draft202012Validator(self.parameters).iter_errors(arguments), None)
            if error is not None:
                where = "/" + "/".join(map(str, error.absolute_path))
                raise GxgError("invalid_input", "arguments do not match the input schema", details={"path": where})
            entry["bundle_version"] = self.ready()  # GR-12: no valid bundle -> temporarily_unavailable; GR-14
            try:
                content, fields = await asyncio.wait_for(self.handler(ident, arguments), self.timeout)
            except TimeoutError:
                raise GxgError("temporarily_unavailable", "KB did not answer in time") from None
            entry |= fields | {"outcome": "ok"}
            return ToolResult(structured_content=content)
        except GxgError as exc:
            entry["outcome"] = exc.code
            if isinstance(exc, TokenError):
                entry["reason"] = exc.reason
            return ToolResult(structured_content=exc.to_dict(), is_error=True)
        except Exception as exc:  # MCC-05 envelope; the text may hold KB secrets (SEC-18): class and frames only
            entry |= {"outcome": "internal_error", "error": type(exc).__name__}
            frames = "\n".join(f"{f.filename}:{f.lineno} in {f.name}" for f in traceback.extract_tb(exc.__traceback__))
            errors.error("internal_error %s %s\n%s", entry["request_id"], entry["error"], frames)
            return ToolResult(structured_content=GxgError("internal_error", "internal error").to_dict(), is_error=True)
        finally:
            entry["duration_ms"] = round((time.monotonic() - started) * 1000)
            entry["kb_calls"] = self.kb_calls() - calls
            audit.info(json.dumps(redact(entry)))


class Connector:
    def __init__(self, backend: Backend, grants: GrantResolver) -> None:
        self.backend = backend
        self.grants = grants
        self._cursor_key = secrets.token_bytes()

    def tools(self, verifier: IdentityVerifier, refresh: Callable[[], None]) -> list[KbTool]:
        handlers: dict[str, Handler] = {
            "kb.documents.search": self.documents_search,
            "kb.documents.get": self.documents_get,
            "kb.context.get": self.context_get,
        }

        def ready() -> str:
            refresh()
            return self.grants.version  # GR-12: no valid bundle -> temporarily_unavailable before any handler

        manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        return [
            KbTool(
                name=t["name"],
                description=load_schema(t["input_schema"])["description"],
                parameters=load_schema(t["input_schema"]),
                output_schema=load_schema(t["output_schema"]),
                timeout=float(t["limits"]["timeout_s"]),
                handler=handlers[t["name"]],
                verify=verifier.verify,
                ready=ready,
                kb_calls=self.backend.calls.total,
            )
            for t in manifest["tools"]
        ]

    async def _readable(self, ident: VerifiedIdentity, requested: list[str] | None) -> tuple[str, set[str]]:
        """(connection, collections) to search. A named collection without a grant -> not_found before any
        KB call (GR-17); nothing granted -> empty and no KB call (GR-16)."""
        tenant, principal = ident.tenant_id, ident.principal
        connection = self.backend.connections.get(tenant)
        if connection is None:  # no KB for this tenant
            if requested:
                raise GxgError("not_found", NOT_FOUND)
            return "", set()
        if requested is not None:
            names = set(requested)
        else:
            allowed = self.grants.allowed(tenant, principal, "read", connection, RTYPE)
            prefix = rid(connection, "")
            names = {r.removeprefix(prefix) for r in allowed.rids if r.startswith(prefix)}
            if allowed.all_of_type:
                names = set(await self.backend.list_collections(tenant))
        readable = {n for n in names if self.grants.check(tenant, principal, "read", rid(connection, n)).allowed}
        if requested is not None and readable != names:
            raise GxgError("not_found", NOT_FOUND)
        return connection, readable

    async def _search(
        self, ident: VerifiedIdentity, args: dict[str, Any], offset: int, limit: int
    ) -> tuple[str, list[Document], bool, set[str]]:
        """(connection, visible page, more after it, grant ids). The KB returns one page of the allowed collections
        only (GR-16); every item is checked again, whatever the KB returned."""
        connection, readable = await self._readable(ident, args.get("collections"))
        hits, more = (
            await self.backend.search(ident.tenant_id, sorted(readable), args["query"], offset=offset, limit=limit)
            if readable
            else ([], False)
        )
        visible, grant_ids = [], set()
        for d in hits:
            decision = self.grants.check(ident.tenant_id, ident.principal, "read", rid(connection, d.collection))
            if d.collection in readable and decision.allowed:
                visible.append(d)
                grant_ids.add(str(decision.grant_id))
        return connection, visible, more, grant_ids

    async def documents_search(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        limit: int = args.get("limit", 20)
        binding = json.dumps(
            [ident.tenant_id, ident.principal, args["query"], sorted(set(args.get("collections") or [])), limit]
        ).encode()
        cursor = args.get("cursor")
        offset = 0 if cursor is None else self._offset(binding, cursor)
        connection, page, more, grant_ids = await self._search(ident, args, offset, limit)
        ref = rid(connection, "")
        items = [
            {
                "id": d.id,
                "title": d.title[:TITLE_MAX],
                "excerpt": d.content[:EXCERPT_MAX],
                "source_ref": ref + d.collection,
            }
            for d in page
        ]
        content = {
            "items": items,
            "next_cursor": self._cursor(binding, offset + limit) if more else None,
            "complete": True,
            "partial_reasons": [],
            "source_refs": sorted({i["source_ref"] for i in items}),
        }
        return content, {"grant_id": sorted(grant_ids)}

    async def documents_get(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        connection = self.backend.connections.get(ident.tenant_id)
        if connection is None:  # no KB for this tenant
            raise GxgError("not_found", NOT_FOUND)
        allowed = self.grants.allowed(ident.tenant_id, ident.principal, "read", connection, RTYPE)
        if not (allowed.all_of_type or allowed.rids):  # no collection granted at all: no KB call
            raise GxgError("not_found", NOT_FOUND)
        doc = await self.backend.get_document(ident.tenant_id, args["document_id"])  # collection from the KB (GR-01)
        if doc is None:
            raise GxgError("not_found", NOT_FOUND)
        decision = self.grants.check(ident.tenant_id, ident.principal, "read", rid(connection, doc.collection))
        if not decision.allowed:
            raise GxgError("not_found", NOT_FOUND)
        content = {
            "id": doc.id,
            "collection": doc.collection,
            "title": doc.title[:TITLE_MAX],
            "content": doc.content[:CONTENT_MAX],
            "truncated": len(doc.content) > CONTENT_MAX,
            "updated_at": doc.updated_at,
            "source_ref": rid(connection, doc.collection),
        }
        return content, {"grant_id": [decision.grant_id]}

    async def context_get(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        """The search above, then excerpts joined up to max_chars (a cut document marks the result truncated)."""
        max_docs: int = args.get("max_documents", 5)
        max_chars: int = args.get("max_chars", 4000)
        connection, visible, truncated, grant_ids = await self._search(ident, args, 0, max_docs)
        text, used = "", []
        for d in visible:
            sep = "\n\n" if text else ""
            room = max_chars - len(text) - len(sep)
            if room <= 0:
                truncated = True
                break
            part = f"## {d.title[:TITLE_MAX]}\n{d.content}"
            if len(part) > room:
                part, truncated = part[:room], True
            text += sep + part
            used.append(d)
        documents = [
            {"id": d.id, "title": d.title[:TITLE_MAX], "source_ref": rid(connection, d.collection)} for d in used
        ]
        content = {
            "context": text,
            "documents": documents,
            "complete": not truncated,
            "partial_reasons": ["truncated"] if truncated else [],
            "source_refs": sorted({d["source_ref"] for d in documents}),
        }
        return content, {"grant_id": sorted(grant_ids)}

    # shortcut: the cursor key lives in this process (as in demo): after a restart or on another replica a cursor
    # is invalid_input and the caller starts over; a shared key with the KB client when there are replicas.
    # Cursor: offset + HMAC-SHA256 over (tenant, principal, query, collections, limit) with a key of this process.
    def _cursor(self, binding: bytes, offset: int) -> str:
        return f"{offset}.{hmac.new(self._cursor_key, binding + str(offset).encode(), hashlib.sha256).hexdigest()}"

    def _offset(self, binding: bytes, cursor: str) -> int:
        m = _CURSOR.fullmatch(cursor)
        if m is None or not hmac.compare_digest(cursor, self._cursor(binding, int(m[1]))):
            raise GxgError("invalid_input", "cursor is not valid for this caller and query")
        return int(m[1])
