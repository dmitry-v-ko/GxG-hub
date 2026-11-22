"""WebAuthn (py_webauthn): passkey registration and the step-up assertion of a decision (AS-16, AS-17, SEC-08).

The step-up challenge contains the intent id and its args hash and lives in the signed session for 5 minutes. It is
popped on the first verification attempt — but the session is a client-side cookie, so an old copy of it still holds
the challenge, and passkeys with a zero signature counter do not show a replay. What stops a replay from counting is
the database: one vote per human per intent (decisions UNIQUE (intent_id, decided_by)) and the hash/intent binding of
the challenge. Server-side single use comes with the decider (AS-30, M10).
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

import psycopg
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

STEP_UP_MAX_AGE_S = 300  # AS-16
REGISTRATION_MAX_AGE_S = 300

_SQL_CREDENTIALS = "SELECT credential_id FROM webauthn_credentials WHERE human_principal = %s"
_SQL_CREDENTIAL = (
    "SELECT public_key, sign_count FROM webauthn_credentials WHERE credential_id = %s AND human_principal = %s"
)
_SQL_COUNT = "UPDATE webauthn_credentials SET sign_count = %s WHERE credential_id = %s"
_SQL_ADD = (
    "INSERT INTO webauthn_credentials (credential_id, human_principal, public_key, sign_count, created_at) "
    "VALUES (%s, %s, %s, %s, now())"
)
_SQL_AUDIT = "INSERT INTO audit_events (at, actor, action, details) VALUES (now(), %s, 'passkey_registered', '{}')"


RP_NAME = "GxG Approval"
CHALLENGE_BYTES = 32
# AS-16: a passkey with user verification (biometrics or PIN), not presence only
SELECTION = AuthenticatorSelectionCriteria(
    resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.REQUIRED
)
UV = UserVerificationRequirement.REQUIRED


@dataclass(frozen=True)
class RelyingParty:
    rp_id: str  # host of the public URL
    origin: str  # scheme://host[:port] of the public URL


def has_passkey(conn: psycopg.Connection[Any], human: str) -> bool:
    return conn.execute(_SQL_CREDENTIALS, (human,)).fetchone() is not None


def registration_options(
    rp: RelyingParty, conn: psycopg.Connection[Any], session: MutableMapping[str, Any], *, human: str, now: float
) -> str:
    challenge = secrets.token_bytes(CHALLENGE_BYTES)
    session["reg"] = {"challenge": bytes_to_base64url(challenge), "at": now}
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=RP_NAME,
        user_id=hashlib.sha256(human.encode()).digest(),
        user_name=human,
        challenge=challenge,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(r[0])) for r in conn.execute(_SQL_CREDENTIALS, (human,))
        ],
        authenticator_selection=SELECTION,
    )
    return options_to_json(options)


def register(
    rp: RelyingParty,
    conn: psycopg.Connection[Any],
    session: MutableMapping[str, Any],
    *,
    human: str,
    credential_json: str,
    now: float,
) -> bool:
    pending = session.pop("reg", None)
    if not pending or now - pending["at"] > REGISTRATION_MAX_AGE_S:
        return False
    try:
        verified = verify_registration_response(
            credential=parse_registration_credential_json(credential_json),
            expected_challenge=base64url_to_bytes(pending["challenge"]),
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            require_user_verification=True,
        )
    except Exception:  # any malformed or failed attestation is a refusal
        return False
    with conn.transaction():
        conn.execute(_SQL_ADD, (verified.credential_id, human, verified.credential_public_key, verified.sign_count))
        conn.execute(_SQL_AUDIT, (human,))
    return True


def challenge_for(intent_id: str, args_hash: str) -> bytes:
    """AS-16: the challenge names the intent and its args hash, plus a nonce."""
    return f"gxg-step-up|{intent_id}|{args_hash}|{secrets.token_hex(CHALLENGE_BYTES // 2)}".encode()


def authentication_options(
    rp: RelyingParty,
    conn: psycopg.Connection[Any],
    session: MutableMapping[str, Any],
    *,
    human: str,
    intent_id: str,
    args_hash: str,
    now: float,
) -> str:
    challenge = challenge_for(intent_id, args_hash)
    session["stepup"] = {"intent": intent_id, "hash": args_hash, "challenge": bytes_to_base64url(challenge), "at": now}
    options = generate_authentication_options(
        rp_id=rp.rp_id,
        challenge=challenge,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(r[0])) for r in conn.execute(_SQL_CREDENTIALS, (human,))
        ],
        user_verification=UV,
    )
    return options_to_json(options)


def verify(
    rp: RelyingParty,
    conn: psycopg.Connection[Any],
    session: MutableMapping[str, Any],
    *,
    human: str,
    intent_id: str,
    args_hash: str,
    assertion_json: str,
    now: float,
) -> bool:
    """A fresh assertion of this human's passkey over the challenge issued for this intent and args hash."""
    pending = session.pop("stepup", None)
    if (
        not pending
        or pending["intent"] != intent_id
        or pending["hash"] != args_hash
        or now - pending["at"] > STEP_UP_MAX_AGE_S
    ):
        return False
    try:
        credential = parse_authentication_credential_json(assertion_json)
        row = conn.execute(_SQL_CREDENTIAL, (credential.raw_id, human)).fetchone()
        if row is None:
            return False
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(pending["challenge"]),
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            credential_public_key=bytes(row[0]),
            credential_current_sign_count=int(row[1]),
            require_user_verification=True,
        )
    except Exception:  # malformed JSON, wrong signature, replayed counter: a refusal
        return False
    conn.execute(_SQL_COUNT, (verified.new_sign_count, credential.raw_id))
    return True
