"""`admin` API (AS-24): kill switch (EX-13) and passkey invites (AS-30), for operators with `platform_admin`.

Admin network only (`tag:approval-admin`, SEC-26). Every request carries the operator's OIDC ID token
(`Authorization: Bearer`), verified here against the IdP's JWKS: signature, `iss`, `aud`, `exp`. The operator is
`oidc:<iss>:<sub>`; `platform_admin` is the list `GXG_ADMIN_OPERATORS` (the bundle has no operator role:
pending_owner_decision). `switch clear` lifts a block, so it also needs a fresh login: `auth_time` at most 5 minutes
old (step-up of «Администрирование»). Every action is audited in the same transaction as the change
(gxg_switch, gxg_passkey_invite) and counted in `gxg_admin_actions_total` for the operator's alert.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from . import admin_web
from .decider import invite_hash

STEP_UP_MAX_AGE_S = 300
INVITE_TTL_S = 24 * 3600
ALGORITHMS = ["RS256", "ES256", "EdDSA"]
HTTP = {"GX400": 400, "GX403": 403, "GX404": 404, "GX409": 409}


class SwitchChange(BaseModel):
    scope: str = Field(max_length=256)
    blocked: bool
    reason: str = Field(max_length=500)


class Invite(BaseModel):
    human: str = Field(max_length=512)


@dataclass(frozen=True)
class Deps:
    pool: ConnectionPool[Any]
    issuer: str
    audience: str
    operators: frozenset[str]
    signing_key: Callable[[str], Any]  # token → verification key (PyJWKClient.get_signing_key_from_jwt in production)
    clock: Callable[[], float] = time.time
    web: Any = None  # admin_web.Web: the operator's browser sign-in and the invite page (optional)


class _Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message


def create_app(deps: Deps) -> FastAPI:  # noqa: PLR0915
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(_Refused)
    def refused(_: Any, exc: _Refused) -> Response:
        return JSONResponse({"message": exc.message}, status_code=exc.status)

    def operator(request: Request, *, fresh: bool = False) -> str:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise _Refused(401, "operator token required")
        token = header.removeprefix("Bearer ")
        try:
            key = deps.signing_key(token)
            claims = jwt.decode(
                token, getattr(key, "key", key), algorithms=ALGORITHMS, audience=deps.audience, issuer=deps.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]}, leeway=5,
            )  # fmt: skip
        except Exception:  # bad signature, unknown kid, wrong iss/aud, expired, malformed
            raise _Refused(401, "operator token refused") from None
        who = f"oidc:{claims['iss']}:{claims['sub']}"
        if who not in deps.operators:
            raise _Refused(403, "platform_admin required")
        auth_time = claims.get("auth_time")
        if fresh and (not isinstance(auth_time, int | float) or deps.clock() - auth_time > STEP_UP_MAX_AGE_S):
            raise _Refused(403, "a fresh login (auth_time ≤ 5 min) is required to clear a switch")
        return who

    def call(sql: str, params: tuple[Any, ...]) -> None:
        try:
            with deps.pool.connection() as conn, conn.transaction():
                conn.execute(sql, params)
        except psycopg.Error as exc:
            status = HTTP.get(str(exc.sqlstate))
            if status is None:
                raise
            raise _Refused(status, str(exc.diag.message_primary)) from None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/switches")
    def switches(request: Request) -> Response:
        operator(request)
        out = [
            {"scope": s, "blocked": b, "reason": r, "changed_by": c, "changed_at": t}
            for s, b, r, c, t in list_switches()
        ]
        return JSONResponse(out)

    @app.post("/v1/switches")
    def switch(request: Request, body: SwitchChange) -> Response:
        who = operator(request, fresh=not body.blocked)
        call("SELECT gxg_switch(%s, %s, %s, %s)", (body.scope, body.blocked, body.reason, who))
        return JSONResponse({"scope": body.scope, "blocked": body.blocked})

    def issue_invite(who: str, human: str) -> str:
        """The one place an invite is created (API and the operator page): audited in the same transaction."""
        token = secrets.token_urlsafe(24)
        call("SELECT gxg_passkey_invite(%s, %s, %s, %s)", (human, invite_hash(token), who, INVITE_TTL_S))
        return token

    @app.post("/v1/passkey-invites")
    def invite(request: Request, body: Invite) -> Response:
        who = operator(request)
        token = issue_invite(who, body.human)
        return JSONResponse({"human": body.human, "invite": token, "expires_in_s": INVITE_TTL_S})

    def list_switches() -> list[tuple[str, bool, str, str, str]]:
        with deps.pool.connection() as conn:
            rows = conn.execute("SELECT scope, blocked, reason, changed_by, changed_at FROM switches ORDER BY scope")
            return [(sc, b, r, c, t.isoformat()) for sc, b, r, c, t in rows]

    def set_switch(who: str, scope: str, blocked: bool, reason: str) -> str:
        """The operator page's kill switch: the same audited `gxg_switch` as the API; "" or the refusal message."""
        try:
            call("SELECT gxg_switch(%s, %s, %s, %s)", (scope, blocked, reason, who))
        except _Refused as exc:
            return exc.message
        return ""

    if deps.web is not None:
        admin_web.register(
            app, web=deps.web, deps=deps, issue_invite=issue_invite, list_switches=list_switches, set_switch=set_switch
        )
    return app
