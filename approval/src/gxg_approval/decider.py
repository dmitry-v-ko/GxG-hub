"""`decider` (AS-30): the only process that records a human decision, and only for a verified passkey assertion.

The ui forwards the raw WebAuthn JSON; it is never trusted. For a decision the decider
1. issues the challenge through `gxg_decision_challenge` (SHA-256(intent_id ‖ args_hash ‖ version ‖ decision) ‖
   16 random bytes, a row valid 5 minutes and once);
2. verifies the assertion with py_webauthn against the credential stored at enrollment (origin and RP id of the
   ui, user verification, signature counter);
3. calls `gxg_decide`, which re-checks the challenge row (known, fresh, unused, this human's), that the passkey was
   enrolled through the decider, the counter, and then the T2/T3/T4 conditions, all in one transaction.
Enrollment works the same way with a one-time operator invite (`gxg_enroll_challenge`, `gxg_enroll`).

The human the ui names must be the owner of the credential: a compromised ui cannot decide for someone whose passkey
did not sign, and cannot register a passkey without an invite issued to that human outside the ui.
Internal network only (AS-29); no inbound from the tailnet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers import parse_authentication_credential_json as parse_assertion
from webauthn.helpers import parse_registration_credential_json as parse_registration
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

RP_NAME = "GxG Approval"
UV = UserVerificationRequirement.REQUIRED  # AS-16: user verification (biometrics or PIN), not presence only
SELECTION = AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UV)
HTTP = {"GX400": 400, "GX403": 403, "GX404": 404, "GX409": 409, "GX423": 423}  # _gxg_fail SQLSTATEs
REFUSED = (403, "assertion refused")  # one answer for every verification failure: no oracle for the caller

_SQL_ENROLLED = "SELECT credential_id FROM webauthn_credentials WHERE human_principal = %s AND enrolled_by IS NOT NULL"
_SQL_ALL = "SELECT credential_id FROM webauthn_credentials WHERE human_principal = %s"
_SQL_CREDENTIAL = (
    "SELECT public_key, sign_count FROM webauthn_credentials "
    "WHERE credential_id = %s AND human_principal = %s AND enrolled_by IS NOT NULL"
)
_SQL_CHALLENGE = (
    "SELECT human_principal, intent_id, invite_hash FROM decider_challenges "
    "WHERE challenge = %s AND purpose = %s AND used_at IS NULL"
)
_SQL_DECISION_CHALLENGE = "SELECT gxg_decision_challenge(%s, %s, %s, %s, %s)"
_SQL_DECIDE = "SELECT gxg_decide(%s, %s, %s, %s, %s)"
_SQL_ENROLL_CHALLENGE = "SELECT gxg_enroll_challenge(%s, %s)"
_SQL_ENROLL = "SELECT gxg_enroll(%s, %s, %s, %s)"


class DecisionOptions(BaseModel):
    human: str = Field(max_length=512)
    intent_id: str = Field(max_length=64)
    decision: str = Field(max_length=16)
    version: int = Field(gt=0, lt=2**31)
    args_hash: str = Field(max_length=64)


class Decision(BaseModel):
    human: str = Field(max_length=512)
    intent_id: str = Field(max_length=64)
    assertion: str = Field(max_length=16384)  # the browser's PublicKeyCredential.toJSON()
    comment: str | None = Field(default=None, max_length=500)


class EnrollOptions(BaseModel):
    human: str = Field(max_length=512)
    invite: str = Field(min_length=16, max_length=128)


class Enrollment(EnrollOptions):
    credential: str = Field(max_length=16384)


@dataclass(frozen=True)
class Deps:
    pool: ConnectionPool[Any]
    rp_id: str  # host of the ui's public URL
    origin: str  # scheme://host[:port] of the ui's public URL
    ready: Callable[[], bool] = lambda: True


class _Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message


def invite_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _challenge_of(credential_json: str, parse: Callable[[str], Any]) -> tuple[Any, bytes]:
    """The parsed credential and the challenge its clientDataJSON names; malformed input is a refusal."""
    try:
        credential = parse(credential_json)
        client = json.loads(credential.response.client_data_json)
        return credential, base64url_to_bytes(client["challenge"])
    except Exception:  # any malformed JSON, base64 or structure
        raise _Refused(*REFUSED) from None


def _call(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> Any:
    try:
        with conn.transaction():
            return (conn.execute(sql, params).fetchone() or (None,))[0]
    except psycopg.Error as exc:
        status = HTTP.get(str(exc.sqlstate))
        if status is None:
            raise
        raise _Refused(status, str(exc.diag.message_primary)) from None


def create_app(deps: Deps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(_Refused)
    def refused(_: Any, exc: _Refused) -> Response:
        return JSONResponse({"message": exc.message}, status_code=exc.status)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Response:
        try:
            with deps.pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
            ok = deps.ready()
        except Exception:  # any failure means not ready
            ok = False
        return JSONResponse({"ready": ok}, status_code=200 if ok else 503)

    @app.post("/v1/decisions/options")
    def decision_options(body: DecisionOptions) -> Response:
        with deps.pool.connection() as conn:
            challenge = _call(
                conn, _SQL_DECISION_CHALLENGE, (body.human, body.intent_id, body.decision, body.version, body.args_hash)
            )
            allow = [PublicKeyCredentialDescriptor(id=bytes(r[0])) for r in conn.execute(_SQL_ENROLLED, (body.human,))]
        if not allow:
            raise _Refused(403, "no passkey enrolled through the decider (AS-17)")
        options = generate_authentication_options(
            rp_id=deps.rp_id, challenge=bytes(challenge), allow_credentials=allow, user_verification=UV
        )
        return Response(options_to_json(options), media_type="application/json")

    @app.post("/v1/decisions")
    def decide(body: Decision) -> Response:
        credential, challenge = _challenge_of(body.assertion, parse_assertion)
        with deps.pool.connection() as conn:
            row = conn.execute(_SQL_CHALLENGE, (challenge, "decide")).fetchone()
            if row is None or row[0] != body.human or str(row[1]) != body.intent_id:
                raise _Refused(*REFUSED)  # unknown or used challenge, another human, another intent
            stored = conn.execute(_SQL_CREDENTIAL, (credential.raw_id, body.human)).fetchone()
            if stored is None:
                raise _Refused(*REFUSED)  # not this human's passkey, or not enrolled through the decider
            try:
                verified = verify_authentication_response(
                    credential=credential,
                    expected_challenge=challenge,
                    expected_rp_id=deps.rp_id,
                    expected_origin=deps.origin,
                    credential_public_key=bytes(stored[0]),
                    credential_current_sign_count=int(stored[1]),
                    require_user_verification=True,
                )
            except Exception:  # wrong signature, origin, RP, flags or counter
                raise _Refused(*REFUSED) from None
            r = credential.response
            kept = {
                "credential_id": bytes_to_base64url(credential.raw_id),
                "authenticator_data": bytes_to_base64url(r.authenticator_data),
                "client_data_json": bytes_to_base64url(r.client_data_json),
                "signature": bytes_to_base64url(r.signature),
            }  # AS-30: kept in decisions, verifiable later
            params = (challenge, credential.raw_id, verified.new_sign_count, body.comment, json.dumps(kept))
            _call(conn, _SQL_DECIDE, params)
        return JSONResponse({"decided": True})

    @app.post("/v1/passkeys/options")
    def enroll_options(body: EnrollOptions) -> Response:
        with deps.pool.connection() as conn:
            challenge = _call(conn, _SQL_ENROLL_CHALLENGE, (body.human, invite_hash(body.invite)))
            exclude = [PublicKeyCredentialDescriptor(id=bytes(r[0])) for r in conn.execute(_SQL_ALL, (body.human,))]
        options = generate_registration_options(
            rp_id=deps.rp_id,
            rp_name=RP_NAME,
            user_id=hashlib.sha256(body.human.encode()).digest(),
            user_name=body.human,
            challenge=bytes(challenge),
            exclude_credentials=exclude,
            authenticator_selection=SELECTION,
        )
        return Response(options_to_json(options), media_type="application/json")

    @app.post("/v1/passkeys")
    def enroll(body: Enrollment) -> Response:
        credential, challenge = _challenge_of(body.credential, parse_registration)
        with deps.pool.connection() as conn:
            row = conn.execute(_SQL_CHALLENGE, (challenge, "enroll")).fetchone()
            if row is None or row[0] != body.human or row[2] != invite_hash(body.invite):
                raise _Refused(*REFUSED)
            try:
                verified = verify_registration_response(
                    credential=credential,
                    expected_challenge=challenge,
                    expected_rp_id=deps.rp_id,
                    expected_origin=deps.origin,
                    require_user_verification=True,
                )
            except Exception:  # any malformed or failed attestation
                raise _Refused(*REFUSED) from None
            _call(
                conn,
                _SQL_ENROLL,
                (challenge, verified.credential_id, verified.credential_public_key, verified.sign_count),
            )
        return JSONResponse({"registered": True})

    return app
