"""Stage C of `demo`: `demo.message.propose` (EFF-04…EFF-08) and the internal `demo.message.execute` /
`reconcile` (EFF-18, EFF-19, EFF-25, AS-12) on port 8081, which only the executor may reach (SEC-14).

`execute` order (EFF-18): service JWT of an executor (AS-06) → execution token (kit: signature by a JWKS `kid`,
`iss`, `aud` = demo, `exp`, local `jti` cache) bound to the body → args hash = `ahs` → token's tool, connection and
board match this connector, the requester still has `propose` by the current bundle → `dispatch` (the CAS in the
Approval Service, with the kill switch, AS-05) → one provider call with the idempotency key. Before `dispatch` any
refusal is an HTTP error and the provider is not called; after it every failure is `outcome_unknown` (AS-12).
"""

from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any

import anyio
import httpx2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator, FormatChecker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gxg_connector_kit import TokenError, _schema, canonical, svc_jwt
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.exec_token import ExecTokenVerifier, ExecutionClaims, ExecutorJwks
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.identity import VerifiedIdentity
from gxg_connector_kit.redact import redact

from .backend import Backend
from .provider import Dropped, Provider

PROPOSE, EXECUTE = "demo.message.propose", "demo.message.execute"
DOMAIN, RTYPE = "demo", "board"
HTTP = {
    "invalid_input": 422,  # AS-12: args (hash or shape) do not match
    "unauthenticated": 401,
    "permission_denied": 403,
    "conflict": 409,
    "effect_disabled": 423,
    "temporarily_unavailable": 503,
}
NOT_FOUND = "not found"
# Reconcile body (approval-service-v1, connector contract for the executor)
RECONCILE_BODY = Draft202012Validator(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent_id", "execution_id", "idempotency_key"],
        "properties": {
            "intent_id": {"type": "string", "format": "uuid"},
            "execution_id": {"type": "string", "format": "uuid"},
            "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 256},
            "provider_operation_id": {"type": "string", "maxLength": 256},
        },
    },
    format_checker=FormatChecker(),
)
audit = logging.getLogger("demo_connector.audit")
errors = logging.getLogger("demo_connector.errors")

Result = tuple[dict[str, Any], dict[str, Any]]


class Approval:
    """Client of the Approval Service `api` (service JWT per call, AS-06)."""

    def __init__(self, http: httpx2.AsyncClient, key: Ed25519PrivateKey, *, connector_id: str, audience: str) -> None:
        self.http, self._key, self.connector_id, self.audience = http, key, connector_id, audience

    async def post(self, path: str, body: dict[str, Any]) -> httpx2.Response:
        token = svc_jwt.sign(self._key, iss=self.connector_id, aud=self.audience)
        try:
            return await self.http.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx2.HTTPError:
            raise GxgError("temporarily_unavailable", "approval service is not reachable") from None


def rid(connection: str, board: str) -> str:
    return f"{DOMAIN}:{connection}/{RTYPE}/{board}"


def args_hash(tool: str, version: str, tenant: str, connection: str, resource: str, args: Any) -> str:  # noqa: PLR0917
    """EFF-05: hash of {tool_key, tool_version, tenant_id, connection_id, resource_id, args}."""
    return canonical.hash({"tool_key": tool, "tool_version": version, "tenant_id": tenant, "connection_id": connection,
                           "resource_id": resource, "args": args})  # fmt: skip


class Effects:
    def __init__(
        self,
        backend: Backend,
        grants: BundleStore,
        approval: Approval,
        *,
        provider: Provider,
        jwks: ExecutorJwks,
        tool: dict[str, Any],  # the manifest entry of demo.message.propose
        refresh: Callable[[], None] = lambda: None,
    ) -> None:
        self.backend, self.grants, self.approval, self.provider, self.jwks = backend, grants, approval, provider, jwks
        self.tool, self.refresh = tool, refresh
        self.connector_id = approval.connector_id
        self.tokens = ExecTokenVerifier(self.connector_id, jwks)
        self._dispatched: dict[str, float] = {}  # jti → until: a replay of these is «already used» (409)
        self.callers = svc_jwt.SvcJwtVerifier(self.connector_id, grants.registry)

    # --- public MCP tool ----------------------------------------------------------------------------
    async def propose(self, ident: VerifiedIdentity, args: dict[str, Any]) -> Result:
        tenant, principal = ident.tenant_id, ident.principal
        connection = self.backend.connections.get(tenant)
        board = self.backend.boards[tenant].get(args["board_id"]) if connection else None
        if connection is None or board is None:
            raise GxgError("not_found", NOT_FOUND)
        resource = rid(connection, board.id)
        decision = self.grants.check(tenant, principal, "propose", resource)
        if not decision.allowed:  # GR-17: read without propose reveals nothing new; no right at all → not_found
            readable = self.grants.check(tenant, principal, "read", resource).allowed
            raise GxgError("resource_forbidden" if readable else "not_found", "no propose right on this board"
                           if readable else NOT_FOUND)  # fmt: skip
        canonical_args = canonical.normalize({"recipient": args["recipient"], "text": args["text"]})  # EFF-05, NFC
        version = self.tool["version"]
        labels = self.tool["preview"]["fields"]
        body = {
            "tool_key": PROPOSE,
            "tool_version": version,
            "connection_id": connection,
            "resource_id": resource,
            "canonical_args": canonical_args,
            "canonical_args_hash": args_hash(PROPOSE, version, tenant, connection, resource, canonical_args),
            # EFF-08: no key from the agent → our own; then repeats are not deduplicated
            "idempotency_key": args.get("idempotency_key") or f"demo:{uuid.uuid4()}",
            "preview": {  # EFF-06, EFF-30: from canonical args and the backend, labels from the manifest
                "schema_version": 1,
                "title": self.tool["preview"]["title"],
                "target": {"label": board.title, "rid": resource},
                "fields": [
                    {"label": labels["recipient"], "value": canonical_args["recipient"], "kind": "identifier"},
                    {"label": labels["text"], "value": canonical_args["text"], "kind": "multiline"},
                ],
            },
            "requester": {"principal": principal, "tenant_id": tenant},
        }
        resp = await self.approval.post("/v1/intents", body)
        doc = _json(resp)
        if resp.status_code in (200, 201) and isinstance(doc, dict) and {"intent_id", "status"} <= doc.keys():
            return {"intent_id": doc["intent_id"], "status": doc["status"]}, {
                "intent_id": doc["intent_id"],
                "grant_id": [decision.grant_id],
            }
        raise _refusal(doc)

    # --- internal HTTP (port 8081) ------------------------------------------------------------------
    def internal_app(self) -> Starlette:
        async def healthz(_: Request) -> JSONResponse:
            return JSONResponse({"status": "ok"})

        return Starlette(
            routes=[
                Route(f"/internal/v1/execute/{EXECUTE}", self._endpoint(self.execute), methods=["POST"]),
                Route(f"/internal/v1/reconcile/{EXECUTE}", self._endpoint(self.reconcile), methods=["POST"]),
                Route("/healthz", healthz),
            ]
        )

    def _endpoint(self, handler: Callable[[dict[str, Any], dict[str, Any]], Any]) -> Callable[..., Any]:
        async def run(request: Request) -> JSONResponse:
            started = time.monotonic()
            entry: dict[str, Any] = {"request_id": str(uuid.uuid4()), "tool": EXECUTE, "op": handler.__name__}
            try:
                scheme, _, token = request.headers.get("authorization", "").partition(" ")
                executors = [s.id for s in self.grants.registry().values() if s.kind == "executor"]
                if scheme.lower() != "bearer" or not token:
                    raise TokenError("missing")
                self.callers.verify(token, allowed_issuers=executors)  # AS-06: only an executor
                try:
                    body = json.loads(await request.body())
                except ValueError:
                    raise GxgError("invalid_input", "body is not JSON") from None
                if not isinstance(body, dict):
                    raise GxgError("invalid_input", "body is not an object")
                out = await handler(body, entry)
                entry["outcome"] = out.get("status") or out.get("finding")
                return JSONResponse(out)
            except GxgError as exc:
                entry["outcome"] = exc.code
                if isinstance(exc, TokenError):
                    entry["reason"] = exc.reason  # why (kid, exp, replay…); never the token
                return JSONResponse(exc.to_dict(), status_code=HTTP[exc.code])  # every code raised here is mapped
            except Exception as exc:  # before dispatch (after it, execute catches everything): no provider call
                entry |= {"outcome": "internal_error", "error": type(exc).__name__}
                tb = traceback.extract_tb(exc.__traceback__)
                frames = "\n".join(f"{f.filename}:{f.lineno} in {f.name}" for f in tb)
                errors.error("internal_error %s %s\n%s", entry["request_id"], entry["error"], frames)
                return JSONResponse(GxgError("internal_error", "internal error").to_dict(), status_code=500)
            finally:
                entry["duration_ms"] = round((time.monotonic() - started) * 1000)
                audit.info(json.dumps(redact(entry)))

        return run

    async def execute(self, body: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        claims, args = await self._checked(body, entry)  # args: the canonical form whose hash was approved
        token = body["execution_token"]
        resp = await self.approval.post(f"/v1/executions/{claims['jti']}/dispatch", {"execution_token": token})
        if resp.status_code == 423:
            raise GxgError("effect_disabled", "effects are disabled")
        if resp.status_code != 200:
            raise GxgError("conflict", "dispatch refused")
        now = time.time()
        self._dispatched = {j: t for j, t in self._dispatched.items() if t > now}
        self._dispatched[claims["jti"]] = claims["exp"] + 5
        # Dispatched (EFF-18 п.4): from here on the only answers are effect results (AS-12).
        try:
            sent = await self.provider.send(
                tenant=claims["ten"],
                provider_key=claims["sub"],  # intent_id (EFF-18 p.5), not the agent's per-requester key
                execution_id=claims["jti"],
                recipient=args["recipient"],
                text=args["text"],
            )
        except Dropped:
            return _result(claims["jti"], "outcome_unknown", None)
        except Exception as exc:  # unknown whether the provider got it
            entry["error"] = type(exc).__name__
            return _result(claims["jti"], "outcome_unknown", None)
        if sent.outcome in ("ok", "duplicate"):
            return _result(claims["jti"], "succeeded", sent.operation_id)
        return _result(claims["jti"], "failed_before_effect", None)  # reject, rate_limited, timeout_before_start

    async def _checked(self, body: dict[str, Any], entry: dict[str, Any]) -> tuple[ExecutionClaims, dict[str, Any]]:
        """EFF-18 п.1-3, all before `dispatch`: token, args hash, target, grant."""
        if not _schema.validator("execute-request").is_valid(body):
            raise GxgError("invalid_input", "body does not match execute-request")
        try:  # the JWKS fetch may block: off the event loop
            claims = await anyio.to_thread.run_sync(self.tokens.verify, body["execution_token"])
        except TokenError as exc:
            # AC-M8-04: a replay of a dispatched token is «already used» (409); any other replay stays 401 (AC-M8-03)
            if exc.reason == "replay" and body["execution_id"] in self._dispatched:
                raise GxgError("conflict", "execution token already used") from None
            raise
        if (claims["sub"], claims["jti"]) != (body["intent_id"], body["execution_id"]):
            raise TokenError("binding")
        tenant, principal, resource = claims["ten"], claims["req"], claims["res"]
        entry |= {"tenant": tenant, "principal": principal, "intent_id": claims["sub"], "execution_id": claims["jti"]}
        tool_key, _, version = claims["tool"].partition("@")  # tool names have no `@` (execution-token schema)
        try:
            args = canonical.normalize(body["args"])
            same = args_hash(tool_key, version, tenant, claims["con"], resource, args)
        except GxgError:  # a float or a lone surrogate: not what was approved
            same = None
        if same != claims["ahs"]:  # EFF-18 п.2: one changed character of text or recipient
            raise GxgError("invalid_input", "args do not match the approved hash")
        if (
            (tool_key, version) != (PROPOSE, self.tool["version"])
            or tenant not in self.backend.connections
            # board ids are exact keys: a resource of another domain or connection is never one of them
            or resource.removeprefix(rid(self.backend.connections[tenant], "")) not in self.backend.boards[tenant]
            or claims["con"] != self.backend.connections[tenant]
        ):  # EFF-18 п.3: the token's tool, tenant, connection and resource are this connector's
            raise GxgError("permission_denied", "execution target is not served by this connector")
        self.refresh()
        if not self.grants.check(tenant, principal, "propose", resource).allowed:  # EX-14: revoked since approval
            raise GxgError("permission_denied", "the requester no longer may propose on this board")
        return claims, args

    async def reconcile(self, body: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        if not RECONCILE_BODY.is_valid(body):
            raise GxgError("invalid_input", "body does not match the reconcile request")
        entry |= {"intent_id": body["intent_id"], "execution_id": body["execution_id"]}
        executed, operation = self.provider.lookup(body["intent_id"])  # provider key (EFF-25)
        return {
            "intent_id": body["intent_id"],
            "execution_id": body["execution_id"],
            "finding": "executed" if executed else "not_executed",
            "provider_operation_id": operation,
        }


def _result(execution_id: str, status: str, operation: str | None) -> dict[str, Any]:
    """effect-result.schema.json"""
    return {
        "operation_id": operation or execution_id,
        "status": status,
        "effect_ref": None,
        "reconciliation_required": status == "outcome_unknown",
        "provider_receipt_ref": operation,
    }


def _json(resp: httpx2.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


# Refusals of the Approval Service passed to the agent; anything else (including unauthenticated, which is about our
# service JWT, not the agent) is retryable unavailability.
PASSED_ON = ("invalid_input", "permission_denied", "not_found", "conflict", "effect_disabled", "rate_limited")


def _refusal(doc: Any) -> GxgError:
    if isinstance(doc, dict) and doc.get("code") in PASSED_ON:
        retry = doc.get("retry_after_s") if doc["code"] == "rate_limited" else None
        try:
            return GxgError(doc["code"], str(doc.get("message")), retry_after_s=retry)  # GxgError caps the length
        except ValueError:  # rate_limited without a valid retry_after_s
            pass
    return GxgError("temporarily_unavailable", "approval service did not answer")
