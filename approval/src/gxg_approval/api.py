"""`api` process (port 8090, internal network): the internal API of schemas/approval-service.openapi.yaml."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from jsonschema import Draft202012Validator
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.exec_token import ExecTokenVerifier, ExecutorJwks
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.readiness import all_ready
from gxg_connector_kit.svc_jwt import Service, SvcJwtVerifier

from . import intents
from .crypto import Encryptor

log = logging.getLogger("gxg_approval.api")
STATUS = {
    "invalid_input": 400,
    "unauthenticated": 401,
    "permission_denied": 403,
    "not_found": 404,
    "conflict": 409,
    "effect_disabled": 423,
    "rate_limited": 429,
    "temporarily_unavailable": 503,
}
ID_PATTERN = r"^[0-9a-fA-F-]{36}$"
CANCEL_BODY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["on_behalf_of", "expected_version"],
    "properties": {
        "on_behalf_of": {
            "type": "object",
            "additionalProperties": False,
            "required": ["principal", "tenant_id"],
            "properties": {
                "principal": {"type": "string", "maxLength": 256},
                "tenant_id": {"type": "string", "maxLength": 64},
            },
        },
        "expected_version": {"type": "integer", "minimum": 1},
    },
}
DISPATCH_BODY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["execution_token"],
    "properties": {"execution_token": {"type": "string", "maxLength": 4096}},
}
DISPATCH_RESPONSE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["execution_id", "dispatch_deadline"],
    "properties": {
        "execution_id": {"type": "string", "format": "uuid"},
        "dispatch_deadline": {"type": "string", "format": "date-time"},
    },
}


def _json_body(schema: dict[str, Any]) -> dict[str, Any]:
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


def _responses(*codes: int, ok: dict[str, Any] | None = None) -> dict[int | str, dict[str, Any]]:
    out: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        schema = (
            ok
            if code < 300 and ok is not None
            else {"$ref": "intent-status.schema.json" if code < 300 else "error.schema.json"}
        )
        out[code] = {
            "description": "OK" if code < 300 else "Error",
            "content": {"application/json": {"schema": schema}},
        }
    return out


@dataclass
class Deps:
    service_id: str  # aud of service JWTs to this API
    pool: ConnectionPool[Any]
    store: BundleStore
    encryptor: Encryptor
    exec_jwks: dict[str, Any]  # AS-09: executor public keys (configuration, no secret)
    _exec_verifiers: dict[str, ExecTokenVerifier] = field(default_factory=dict)

    def exec_verifier(self, connector_id: str) -> ExecTokenVerifier:
        if connector_id not in self._exec_verifiers:
            jwks = json.dumps(self.exec_jwks).encode()
            self._exec_verifiers[connector_id] = ExecTokenVerifier(connector_id, ExecutorJwks(lambda: jwks))
        return self._exec_verifiers[connector_id]


def _error(exc: GxgError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after_s)} if exc.retry_after_s else None
    return JSONResponse(exc.to_dict(), status_code=STATUS.get(exc.code, 500), headers=headers)


def create_app(deps: Deps) -> FastAPI:  # noqa: PLR0915 (route table)
    app = FastAPI(title="GxG Approval Service internal API", version="1.0.0", docs_url=None, redoc_url=None)
    verifier = SvcJwtVerifier(deps.service_id, deps.store.registry)

    @app.exception_handler(GxgError)
    async def _gxg(_request: Request, exc: GxgError) -> JSONResponse:
        return _error(exc)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error(GxgError("invalid_input", "request does not match the API contract"))

    @app.exception_handler(Exception)
    async def _internal(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return _error(GxgError("internal_error", "internal error"))

    def caller(request: Request, kind: str) -> Service:
        """AS-06: service JWT of an issuer of `kind` (the endpoint's allowlist)."""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise GxgError("unauthenticated", "token rejected")
        issuers = [s.id for s in deps.store.registry().values() if s.kind == kind]
        return verifier.verify(token, allowed_issuers=issuers)

    async def body(request: Request, schema: Callable[[Any], bool]) -> Any:
        try:
            doc = json.loads(await request.body())
        except ValueError:
            raise GxgError("invalid_input", "body is not JSON") from None
        if not schema(doc):
            raise GxgError("invalid_input", "body does not match the API contract")
        return doc

    @app.post(
        "/v1/intents",
        operation_id="createIntent",
        openapi_extra=_json_body({"$ref": "intent-create.schema.json"}),
        responses=_responses(201, 200, 400, 401, 403, 409, 423, 429),
    )
    async def create_intent(request: Request) -> Response:
        connector = caller(request, "connector")
        doc = await body(request, lambda _d: True)  # validated against intent-create by intents.propose

        def run() -> intents.Created:
            with deps.pool.connection() as conn:
                return intents.propose(conn, deps.store.current, connector, doc, deps.encryptor)

        created = await run_in_threadpool(run)
        return JSONResponse(created.status, status_code=201 if created.new else 200)

    @app.get("/v1/intents/{intent_id}", operation_id="getIntent", responses=_responses(200, 401, 404))
    def get_intent(
        request: Request,
        intent_id: str,
        tenant_id: Annotated[str, Query(max_length=64)],
        principal: Annotated[str, Query(max_length=256)],
    ) -> Response:
        caller(request, "approval_mcp")
        with deps.pool.connection() as conn:
            return JSONResponse(intents.get(conn, intent_id, tenant_id, principal))

    cancel_valid = Draft202012Validator(CANCEL_BODY).is_valid

    @app.post(
        "/v1/intents/{intent_id}/cancel",
        operation_id="cancelIntent",
        openapi_extra=_json_body(CANCEL_BODY),
        responses=_responses(200, 404, 409),
    )
    async def cancel_intent(request: Request, intent_id: str) -> Response:
        caller(request, "approval_mcp")
        doc = await body(request, cancel_valid)
        who = doc["on_behalf_of"]  # AS-11a: mandatory for approval_mcp, the verified identity of the agent

        def run() -> dict[str, Any]:
            with deps.pool.connection() as conn:
                return intents.cancel(conn, intent_id, who["tenant_id"], who["principal"], doc["expected_version"])

        return JSONResponse(await run_in_threadpool(run))

    dispatch_valid = Draft202012Validator(DISPATCH_BODY).is_valid

    @app.post(
        "/v1/executions/{execution_id}/dispatch",
        operation_id="dispatchExecution",
        openapi_extra=_json_body(DISPATCH_BODY),
        responses=_responses(200, 401, 404, 409, 423, ok=DISPATCH_RESPONSE),
    )
    async def dispatch_execution(request: Request, execution_id: uuid.UUID) -> Response:
        connector = caller(request, "connector")
        doc = await body(request, dispatch_valid)
        claims = deps.exec_verifier(connector.id).verify(doc["execution_token"])  # aud = this connector
        if claims["jti"] != str(execution_id):
            raise GxgError("unauthenticated", "token rejected")

        def run() -> str:
            with deps.pool.connection() as conn:
                return intents.dispatch(conn, execution_id, uuid.UUID(claims["sub"]), connector)

        deadline = await run_in_threadpool(run)
        return JSONResponse({"execution_id": str(execution_id), "dispatch_deadline": deadline})

    @app.get("/.well-known/gxg-exec-jwks.json", operation_id="getExecutorJwks")
    def jwks() -> dict[str, Any]:
        return deps.exec_jwks

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        try:
            with deps.pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
            db_ready = True
        except Exception:  # any failure means not ready
            db_ready = False
        state = all_ready(deps.store.readiness())
        ready = db_ready and state.ready
        reasons = list(state.reasons) + ([] if db_ready else ["database is unreachable"])
        return JSONResponse({"ready": ready, "reasons": reasons}, status_code=200 if ready else 503)

    return app
