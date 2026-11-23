"""Fake Approval Service for the effect checks: the three endpoints a connector calls (approval-service-v1).

`/v1/intents` (service JWT of the connector, AS-06; EFF-08 idempotency by key and hash), `/v1/executions/{id}/dispatch`
(one CAS per execution), and the executor JWKS. It records what the connector sent; it is not the Approval Service.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2

from gxg_connector_kit import TokenError, svc_jwt
from gxg_connector_kit.effects import JWKS_PATH
from gxg_connector_kit.svc_jwt import Service


@dataclass
class FakeApproval:
    registry: Callable[[], Mapping[str, Service]]
    jwks: bytes
    connector_id: str
    intents: dict[str, dict[str, Any]] = field(default_factory=dict)  # intent_id -> create body
    dispatched: list[str] = field(default_factory=list)  # execution ids dispatched, in order
    dispatch_requests: list[str] = field(default_factory=list)  # every dispatch request, also refused ones
    down: bool = False  # every request fails to connect (EX-16)
    dispatch_status: int | None = None  # forced answer of dispatch, e.g. 423 (kill switch)

    def __post_init__(self) -> None:
        self._callers = svc_jwt.SvcJwtVerifier("approval-api", self.registry)
        self._keys: dict[str, tuple[str, str]] = {}  # idempotency key -> (intent_id, args hash)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            raise httpx2.ConnectError("approval service is down", request=request)
        path = request.url.path
        if path == JWKS_PATH:
            return httpx2.Response(200, content=self.jwks)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        try:
            if scheme.lower() != "bearer":
                raise TokenError("missing")
            self._callers.verify(token, allowed_issuers=[self.connector_id])
        except TokenError as exc:
            return httpx2.Response(401, json=exc.to_dict())
        body = json.loads(request.content)
        if path == "/v1/intents":
            return self._create(body)
        if path.startswith("/v1/executions/") and path.endswith("/dispatch"):
            if not isinstance(body.get("execution_token"), str):  # the token being dispatched (AS-05)
                return httpx2.Response(400, json={"code": "invalid_input", "message": "no execution_token"})
            return self._dispatch(path.split("/")[3])
        return httpx2.Response(404, json={"code": "not_found", "message": "not found"})

    def _dispatch(self, execution: str) -> httpx2.Response:
        self.dispatch_requests.append(execution)
        if self.dispatch_status is not None:
            return httpx2.Response(self.dispatch_status, json={"code": "effect_disabled", "message": "forced"})
        if execution in self.dispatched:
            return httpx2.Response(409, json={"code": "conflict", "message": "already dispatched"})
        self.dispatched.append(execution)
        return httpx2.Response(200, json={"status": "dispatched"})

    def _create(self, body: dict[str, Any]) -> httpx2.Response:
        key, digest = body["idempotency_key"], body["canonical_args_hash"]
        if key in self._keys:
            intent, seen = self._keys[key]
            if seen != digest:
                return httpx2.Response(409, json={"code": "conflict", "message": "idempotency key reused"})
            return httpx2.Response(200, json={"intent_id": intent, "status": "pending_approval"})
        intent = str(uuid.uuid4())
        self._keys[key] = (intent, digest)
        self.intents[intent] = body
        return httpx2.Response(201, json={"intent_id": intent, "status": "pending_approval"})
