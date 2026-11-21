"""The three read tools of the Telegram connector and the checks every call goes through.

Order for every call (as `demo`): identity from the trusted channel (SEC-03, MCC-02) -> input schema ->
grants: a named chat without a grant is `not_found` before any request to the service (GR-17, AC-M11-03);
a search is narrowed to `allowed()` first and every item is checked again (GR-16) -> the service port.
The service keeps its own Connection scope; grants only narrow it. Errors are `GxgError.to_dict()` as a tool
error (MCC-05); a flood wait is `rate_limited` with Telegram's delay (AC-M11-04). One JSON audit line per call
without query, text, peer IDs or tokens (SEC-18, AC-M11-05).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
import traceback
import unicodedata
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

from gxg_connector_kit import TokenError
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.grants import GrantResolver
from gxg_connector_kit.identity import IdentityVerifier, VerifiedIdentity
from gxg_connector_kit.redact import redact

from .service import CHAT_REFS_MAX, SEARCH_LIMIT_MAX, Message, Reason, ServiceError, TelegramService

ROOT = Path(__file__).resolve().parents[2]  # connectors/telegram
MANIFEST = ROOT / "gxg-manifest.yaml"
DOMAIN, RTYPE = "telegram", "chat"
EXCERPT_MAX, TEXT_MAX = 500, 4000
audit = logging.getLogger("telegram_connector.audit")
errors = logging.getLogger("telegram_connector.errors")  # stack frames of internal errors; main() sends them to stderr

Result = tuple[dict[str, Any], dict[str, Any]]  # (structuredContent, audit fields)
Handler = Callable[[VerifiedIdentity, dict[str, Any]], Awaitable[Result]]

CHAT_REF = re.compile(r"tgc1_[A-Za-z0-9_-]{32}")
_CURSOR = re.compile(r"([0-9]{1,9})\.[0-9a-f]{64}")
NOT_FOUND = "not found"  # one message for "no grant", "not in scope" and "does not exist" (GR-17, SC-05)
_KEEP = frozenset("\n\t")


def load_schema(rel: str) -> dict[str, Any]:
    """A schema file referenced by gxg-manifest.yaml, relative to connectors/telegram."""
    # shortcut: a `../..` path is looked up by its tail in the nearest parent (as demo); package data with the template
    tail = Path(*(part for part in Path(rel).parts if part != ".."))
    doc: dict[str, Any] = json.loads(next(d / tail for d in (ROOT, *ROOT.parents) if (d / tail).is_file()).read_bytes())
    return doc


def rid(connection: str, chat_ref: str) -> str:
    return f"{DOMAIN}:{connection}/{RTYPE}/{chat_ref}"


def escape(text: str) -> str:
    """Control and format characters (bidi overrides, zero-width, BEL...) as visible `\\u{XXXX}` (CON-TG-08)."""
    return "".join(
        f"\\u{{{ord(c):04X}}}" if c not in _KEEP and unicodedata.category(c) in ("Cc", "Cf") else c for c in text
    )


def other(reasons: list[Reason]) -> list[str]:
    """Archive completeness: a chat not yet synced or with a sync gap. `semantic_pending` concerns semantic ranking,
    not this full-text search; `result_limit` becomes pagination."""
    return ["other"] if {"sync_gap", "not_synced"} & set(reasons) else []


def service_error(exc: ServiceError) -> GxgError:
    """Failure classes of the service (connectors/telegram.md "Failures") in the MCC-05 vocabulary."""
    if exc.kind == "flood_wait":
        delay = math.ceil(exc.retry_after_seconds or 0)  # Telegram's delay, never shortened (AC-M11-04)
        return GxgError("rate_limited", "Telegram flood wait", retry_after_s=min(max(delay, 1), 86400))
    if exc.kind in ("chat_forbidden", "peer_inaccessible"):
        return GxgError("not_found", NOT_FOUND)  # outside the service scope: the same as no grant (GR-17)
    return GxgError("temporarily_unavailable", "Telegram service is not available")  # busy, auth_required, provider


class TelegramTool(Tool):
    """A FastMCP tool whose schemas are the files in connectors/telegram/schemas/.

    shortcut: the call pipeline is a copy of demo's DemoTool.run; move it into gxg-connector-kit with the
    connector template (mcp-toolkit-v1) or at the third connector.
    """

    handler: Annotated[SkipJsonSchema[Handler], Field(exclude=True)]
    verify: Annotated[SkipJsonSchema[Callable[..., VerifiedIdentity]], Field(exclude=True)]  # IdentityVerifier.verify
    refresh: Annotated[SkipJsonSchema[Callable[[], None]], Field(exclude=True)]

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        entry: dict[str, Any] = {"request_id": str(uuid.uuid4()), "tool": self.name}
        try:
            headers = get_http_request().headers
            # backlog M5 (2): the agent's own credential must never reach a connector (SEC-04, no token passthrough)
            entry["authorization_header"] = "authorization" in headers
            ident = self.verify(headers, tool=self.name, arguments=arguments)
            entry |= {"request_id": ident.request_id, "tenant": ident.tenant_id, "principal": ident.principal}
            error = next(jsonschema.Draft202012Validator(self.parameters).iter_errors(arguments), None)
            if error is not None:
                where = "/" + "/".join(map(str, error.absolute_path))
                raise GxgError("invalid_input", "arguments do not match the input schema", details={"path": where})
            self.refresh()  # also refuses with temporarily_unavailable without a valid bundle (GR-12)
            try:  # gxg-manifest.yaml limits.timeout_s; FastMCP does not enforce Tool.timeout for this run()
                content, fields = await asyncio.wait_for(self.handler(ident, arguments), self.timeout)
            except TimeoutError:
                # sent to the service, no answer: a read is safe to repeat; never timeout_before_start (MCC-05)
                raise GxgError("temporarily_unavailable", "Telegram service did not answer in time") from None
            except ServiceError as exc:
                entry["service_error"] = exc.kind
                raise service_error(exc) from None
            entry |= fields | {"outcome": "ok"}
            return ToolResult(structured_content=content)
        except GxgError as exc:
            entry["outcome"] = exc.code
            if isinstance(exc, TokenError):  # why the identity was refused (SEC-03); never the token
                entry["reason"] = exc.reason
            return ToolResult(structured_content=exc.to_dict(), is_error=True)
        except Exception as exc:  # MCC-05 envelope; the text may hold message text or peers (SEC-18): class and frames
            entry |= {"outcome": "internal_error", "error": type(exc).__name__}
            frames = "\n".join(f"{f.filename}:{f.lineno} in {f.name}" for f in traceback.extract_tb(exc.__traceback__))
            errors.error("internal_error %s %s\n%s", entry["request_id"], entry["error"], frames)
            return ToolResult(structured_content=GxgError("internal_error", "internal error").to_dict(), is_error=True)
        finally:
            entry["duration_ms"] = round((time.monotonic() - started) * 1000)
            audit.info(json.dumps(redact(entry)))


def _text(m: Message) -> str:
    """The message text, or the caption of a media message; never instructions (SEC-11)."""
    return escape(m.text or m.caption or "")


def _message(m: Message) -> dict[str, Any]:
    text = _text(m)
    return {
        "id": f"{m.chat_ref}:{m.message_id}",
        "message_id": m.message_id,
        "sent_at": m.sent_at,
        "edited_at": m.edited_at,
        "reply_to": m.reply_to,
        "media_type": m.media_type,
        "text": text[:TEXT_MAX],  # the response budget of thread.read (connectors/telegram.md)
        "truncated": len(text) > TEXT_MAX,
    }


class Connector:
    def __init__(self, service: TelegramService, grants: GrantResolver, connections: Mapping[str, tuple[str, str]]):
        """`connections`: tenant -> (Hub connection id in the grants, the service's Connection id)."""
        self.service = service
        self.grants = grants
        self.connections = dict(connections)
        self._cursor_key = secrets.token_bytes()

    def tools(self, verifier: IdentityVerifier, refresh: Callable[[], None]) -> list[TelegramTool]:
        handlers: dict[str, Handler] = {
            "telegram.messages.search": self.search,
            "telegram.thread.read": self.thread_read,
            "telegram.messages.collect": self.collect,
        }

        def ready() -> None:
            refresh()
            _ = self.grants.version  # GR-12: no valid bundle -> temporarily_unavailable before any handler

        manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        return [
            TelegramTool(
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

    def _chat(self, ident: VerifiedIdentity, chat_ref: str, action: str) -> tuple[str, str, str]:
        """(service Connection, RID, grant id) of a named chat, or `not_found` with no request to the service."""
        conn = self.connections.get(ident.tenant_id)
        if conn is None:
            raise GxgError("not_found", NOT_FOUND)
        decision = self.grants.check(ident.tenant_id, ident.principal, action, rid(conn[0], chat_ref))  # type: ignore[arg-type]
        if not decision.allowed:
            raise GxgError("not_found", NOT_FOUND)
        return conn[1], rid(conn[0], chat_ref), str(decision.grant_id)

    async def search(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        query: str = args["query"]
        requested: list[str] | None = args.get("chat_refs")
        limit: int = args.get("limit", 20)
        conn = self.connections.get(ident.tenant_id)
        chats: list[str] = []
        if requested is not None:
            for c in requested:
                self._chat(ident, c, "read")  # GR-17: a named chat without a grant, before any request
            chats = sorted(requested)
        elif conn is not None:
            allowed = self.grants.allowed(ident.tenant_id, ident.principal, "read", conn[0], RTYPE)
            if allowed.all_of_type:  # a mask: every chat of the service's scope (CON-TG-02 stays the boundary)
                chats = await self.service.scope_chats(conn[1])
            else:
                prefix = rid(conn[0], "")
                chats = sorted(r[len(prefix) :] for r in allowed.rids if r.startswith(prefix))
        binding = json.dumps([ident.tenant_id, ident.principal, query, chats, limit]).encode()
        cursor = args.get("cursor")
        offset = 0 if cursor is None else self._offset(binding, cursor)
        if not chats or conn is None:  # GR-16, MCC-06: nothing readable, no request to the service
            return {"items": [], "next_cursor": None, "complete": True, "partial_reasons": [], "source_refs": []}, {}
        page = await self.service.search(conn[1], query, chats[:CHAT_REFS_MAX], offset + limit)
        grant_ids: set[str] = set()
        items = []
        for m in page.items[offset:]:  # GR-16: every item is checked again, whatever the service returned
            decision = self.grants.check(ident.tenant_id, ident.principal, "read", rid(conn[0], m.chat_ref))
            if m.chat_ref in chats and decision.allowed:
                grant_ids.add(str(decision.grant_id))
                items.append(
                    {
                        "id": f"{m.chat_ref}:{m.message_id}",
                        "excerpt": _text(m)[:EXCERPT_MAX],
                        "source_ref": rid(conn[0], m.chat_ref),
                    }
                )
        # the service takes at most 128 chats and returns at most 100 ranked results (archive-repository.ts)
        last = offset + 2 * limit > SEARCH_LIMIT_MAX
        next_cursor = self._cursor(binding, offset + limit) if page.more and not last else None
        partial = other(page.partial_reasons) + (
            ["truncated"] if len(chats) > CHAT_REFS_MAX or (page.more and last) else []
        )
        content = {
            "items": items,
            "next_cursor": next_cursor,
            "complete": not partial,
            "partial_reasons": partial,
            "source_refs": sorted({i["source_ref"] for i in items}),
        }
        return content, {"grant_id": sorted(grant_ids)}

    async def thread_read(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        conn, source, grant = self._chat(ident, args["chat_ref"], "read")
        limit: int = args.get("limit", 20)
        page = await self.service.read_thread(conn, args["chat_ref"], args.get("before_message_id"), limit)
        messages = [_message(m) for m in page.items if m.chat_ref == args["chat_ref"]][:limit]
        partial = other(page.partial_reasons)
        content = {
            "chat_ref": args["chat_ref"],
            "source_ref": source,
            "untrusted": True,
            "messages": messages,
            "next_cursor": messages[-1]["message_id"] if page.more and messages else None,
            "more": page.more,
            "complete": not partial,
            "partial_reasons": partial,
        }
        return content, {"grant_id": [grant]}

    async def collect(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        conn, source, grant = self._chat(ident, args["chat_ref"], "collect")
        cursor: str | None = args.get("cursor")  # `tgk1.<archived_us>.<message_id>`: the input schema checked it
        after = None if cursor is None else (int(cursor.split(".")[1]), int(cursor.split(".")[2]))
        limit: int = args.get("limit", 50)
        got = await self.service.collect(conn, args["chat_ref"], after, limit)
        rows = [(p, x) for p, x in zip(got.positions, got.items, strict=True) if x.chat_ref == args["chat_ref"]][:limit]
        partial = other(got.partial_reasons)
        content = {
            "chat_ref": args["chat_ref"],
            "source_ref": source,
            "untrusted": True,
            "messages": [_message(x) for _, x in rows],
            # the position of the last message, or the caller's own cursor: never a step back (Signals stores it)
            "next_cursor": f"tgk1.{rows[-1][0][0]}.{rows[-1][0][1]}" if rows else cursor,
            "more": got.more,
            "complete": not partial,
            "partial_reasons": partial,
        }
        return content, {"grant_id": [grant]}

    # Cursor: offset + HMAC-SHA256 over (tenant, principal, query, chats, limit) with a key of this process (as demo).
    def _cursor(self, binding: bytes, offset: int) -> str:
        return f"{offset}.{hmac.new(self._cursor_key, binding + str(offset).encode(), hashlib.sha256).hexdigest()}"

    def _offset(self, binding: bytes, cursor: str) -> int:
        m = _CURSOR.fullmatch(cursor)
        if m is None or not hmac.compare_digest(cursor, self._cursor(binding, int(m[1]))):
            raise GxgError("invalid_input", "cursor is not valid for this caller and query")
        return int(m[1])
