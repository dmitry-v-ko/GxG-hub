"""ContextForgeVerifier (ADR-0001 variant C, SEC-03): every claim rule, each rejection with its reason."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kit_testing import T0, Clock

from gxg_connector_kit import TokenError
from gxg_connector_kit.cf_identity import HEADER, ISSUER, TYP, ContextForgeVerifier, args_hash
from gxg_connector_kit.identity import VerifiedIdentity

KID = "adapter-1"
ARGS = {"query": "привет", "limit": 5, "board_ids": ["a-public"]}


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier(key: Ed25519PrivateKey, clock: Clock) -> ContextForgeVerifier:
    return ContextForgeVerifier("demo", {KID: key.public_key()}, now=clock)


def claims(**kw: Any) -> dict[str, Any]:
    return {
        "iss": ISSUER,
        "aud": "demo",
        "sub": "agent:t-alpha:reader",
        "tenant_id": "t-alpha",
        "team_id": "team-uuid",
        "tool": "demo.notes.search",
        "iat": T0,
        "exp": T0 + 60,
        "jti": uuid.uuid4().hex,
        "request_id": "req-1",
        "ahs": args_hash(ARGS),
    } | kw


def token(key: Ed25519PrivateKey, *, kid: str = KID, typ: str = TYP, drop: str | None = None, **kw: Any) -> str:
    c = claims(**kw)
    if drop:
        del c[drop]
    return jwt.encode(c, key, algorithm="EdDSA", headers={"typ": typ, "kid": kid})


def verify(v: ContextForgeVerifier, tok: str, tool: str = "demo.notes.search", args: Any = ARGS) -> VerifiedIdentity:
    return v.verify({"X-GxG-Identity": tok}, tool=tool, arguments=args)


def reason(v: ContextForgeVerifier, tok: str | None, **kw: Any) -> str:
    with pytest.raises(TokenError) as exc:
        if tok is None:
            v.verify({}, tool="demo.notes.search", arguments=ARGS)
        else:
            verify(v, tok, **kw)
    assert exc.value.code == "unauthenticated"
    return exc.value.reason


def test_valid_identity(verifier: ContextForgeVerifier, key: Ed25519PrivateKey) -> None:
    tok = token(key)
    ident = verify(verifier, tok)
    assert ident == VerifiedIdentity(
        tenant_id="t-alpha",
        principal="agent:t-alpha:reader",
        issued_at=datetime.fromtimestamp(T0, UTC),
        expires_at=datetime.fromtimestamp(T0 + 60, UTC),
        request_id="req-1",
        assertion=tok,
        mode="signed_assertion",
    )


def test_request_id_falls_back_to_jti(verifier: ContextForgeVerifier, key: Ed25519PrivateKey) -> None:
    tok = token(key, drop="request_id", jti="j-1")
    assert verify(verifier, tok).request_id == "j-1"


def test_header_is_case_insensitive(verifier: ContextForgeVerifier, key: Ed25519PrivateKey) -> None:
    assert verifier.verify({HEADER.upper(): token(key)}, tool="demo.notes.search", arguments=ARGS)


def test_args_hash_is_sec03_canonical_json() -> None:
    # sorted keys, no spaces, non-ASCII as is; key order of the call does not matter
    assert args_hash({"b": 1, "a": "ж"}) == args_hash({"a": "ж", "b": 1})
    assert args_hash({"b": 1, "a": "ж"}) == hashlib.sha256('{"a":"ж","b":1}'.encode()).hexdigest()


@pytest.mark.parametrize(
    ("make", "want"),
    [
        (lambda k: token(Ed25519PrivateKey.generate()), "signature"),
        (lambda k: token(k, kid="other"), "kid"),
        (lambda k: token(k, typ="JWT"), "typ"),
        (lambda k: token(k, typ="gxg-exec+jwt"), "typ"),
        (lambda k: token(k, iss="mcpgateway"), "iss"),
        (lambda k: token(k, aud="kb"), "aud"),
        (lambda k: token(k, tool="demo.note.get"), "tool"),
        (lambda k: token(k, ahs=args_hash({"query": "other"})), "ahs"),
        (lambda k: token(k, sub="agent:t-beta:reader"), "principal"),
        (lambda k: token(k, sub="t-alpha:reader"), "principal"),
        (lambda k: token(k, exp=T0 + 61), "ttl"),
        (lambda k: token(k, iat=T0 - 120, exp=T0 - 60), "exp"),
        (lambda k: token(k, iat=T0 + 30, exp=T0 + 90), "iat"),
        (lambda k: "W10.e30.AA", "malformed"),
    ],
)
def test_rejections(verifier: ContextForgeVerifier, key: Ed25519PrivateKey, make: Any, want: str) -> None:
    assert reason(verifier, make(key)) == want


@pytest.mark.parametrize("name", ["iss", "aud", "sub", "tenant_id", "iat", "exp", "jti", "tool", "ahs"])
def test_missing_claim(verifier: ContextForgeVerifier, key: Ed25519PrivateKey, name: str) -> None:
    assert reason(verifier, token(key, drop=name)) == f"claim:{name}"


def test_missing_header(verifier: ContextForgeVerifier) -> None:
    assert reason(verifier, None) == "missing"


def test_other_tool_or_arguments_of_the_call(verifier: ContextForgeVerifier, key: Ed25519PrivateKey) -> None:
    assert reason(verifier, token(key), tool="demo.note.get") == "tool"
    assert reason(verifier, token(key), args=ARGS | {"limit": 6}) == "ahs"
    assert verify(verifier, token(key, ahs=args_hash({})), args={})  # no arguments hash as {}


def test_call_binding_is_required(verifier: ContextForgeVerifier, key: Ed25519PrivateKey) -> None:
    for kw in ({}, {"tool": "demo.notes.search"}, {"arguments": ARGS}):
        with pytest.raises(TokenError) as exc:
            verifier.verify({HEADER: token(key)}, **kw)
        assert exc.value.reason == "call binding missing"


def test_replay(verifier: ContextForgeVerifier, key: Ed25519PrivateKey, clock: Clock) -> None:
    tok = token(key)
    assert verify(verifier, tok)
    assert reason(verifier, tok) == "replay"
    clock.t = T0 + 66  # after exp + skew the assertion is expired, not forgotten
    assert reason(verifier, tok) == "exp"


def test_skew_bounds(verifier: ContextForgeVerifier, key: Ed25519PrivateKey, clock: Clock) -> None:
    clock.t = T0 + 65
    assert verify(verifier, token(key))
    clock.t = T0 + 66
    assert reason(verifier, token(key)) == "exp"
    clock.t = T0 - 5
    assert verify(verifier, token(key))
    clock.t = T0 - 6
    assert reason(verifier, token(key)) == "iat"


def test_key_rotation_by_kid(key: Ed25519PrivateKey, clock: Clock) -> None:
    new = Ed25519PrivateKey.generate()
    both = ContextForgeVerifier("demo", {KID: key.public_key(), "adapter-2": new.public_key()}, now=clock)
    assert verify(both, token(key)) and verify(both, token(new, kid="adapter-2"))
    only_new = ContextForgeVerifier("demo", {"adapter-2": new.public_key()}, now=clock)
    assert reason(only_new, token(key)) == "kid"


def test_no_keys_does_not_start() -> None:
    with pytest.raises(ValueError, match=r"^no identity adapter keys$"):
        ContextForgeVerifier("demo", {})
