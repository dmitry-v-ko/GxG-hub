"""ADR-0001 / SEC-03: IdentityVerifier interface and the test-profile stub issuer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kit_testing import T0, Clock, jws

from gxg_connector_kit import _jwt
from gxg_connector_kit.identity import (
    REQUIRED_CLAIMS,
    STUB_HEADER,
    IdentityVerifier,
    StubIssuer,
    StubVerifier,
    VerifiedIdentity,
)

CONNECTOR = "demo"
pytestmark = pytest.mark.usefixtures("test_profile")


@pytest.fixture
def issuer() -> StubIssuer:
    return StubIssuer()


@pytest.fixture
def verifier(issuer: StubIssuer, clock: Clock) -> StubVerifier:
    return StubVerifier(CONNECTOR, issuer.public_key, now=clock)


def headers(issuer: StubIssuer, clock: Clock, **over: Any) -> dict[str, str]:
    kw: dict[str, Any] = {"tenant_id": "t-alpha", "principal": "agent:t-alpha:hermes", "audience": CONNECTOR}
    kw.update(over)
    return issuer.headers(now=clock, **kw)


def reason(verifier: StubVerifier, hdrs: dict[str, str]) -> str:
    with pytest.raises(_jwt.TokenError) as exc:
        verifier.verify(hdrs)
    assert exc.value.code == "unauthenticated"
    return exc.value.reason


def test_valid_identity(issuer: StubIssuer, verifier: StubVerifier, clock: Clock) -> None:
    v: IdentityVerifier = verifier
    hdrs = headers(issuer, clock)
    ident = v.verify(hdrs)
    assert ident == VerifiedIdentity(
        tenant_id="t-alpha",
        principal="agent:t-alpha:hermes",
        issued_at=datetime.fromtimestamp(T0, UTC),
        expires_at=datetime.fromtimestamp(T0 + 60, UTC),
        request_id=ident.request_id,
        assertion=hdrs[STUB_HEADER],
        mode="signed_assertion",
    )


def test_header_name_is_case_insensitive(issuer: StubIssuer, verifier: StubVerifier, clock: Clock) -> None:
    token = headers(issuer, clock)[STUB_HEADER]
    assert verifier.verify({STUB_HEADER.upper(): token}).tenant_id == "t-alpha"


def test_unsigned_identity_headers_give_nothing(verifier: StubVerifier) -> None:
    """ADR criterion 1 / SEC-04: tenant and agent in plain headers are not identity."""
    assert reason(verifier, {"x-tenant-id": "t-alpha", "x-agent-id": "agent:t-alpha:hermes"}) == "missing"


def test_other_headers_cannot_override_the_assertion(issuer: StubIssuer, verifier: StubVerifier, clock: Clock) -> None:
    hdrs = {**headers(issuer, clock), "x-tenant-id": "t-beta", "x-gxg-tenant": "t-beta"}
    assert verifier.verify(hdrs).tenant_id == "t-alpha"


def test_forged_signature(issuer: StubIssuer, verifier: StubVerifier, clock: Clock) -> None:
    forger = Ed25519PrivateKey.generate()
    claims = jwt.decode(headers(issuer, clock)[STUB_HEADER], options={"verify_signature": False})
    claims["tenant_id"], claims["sub"] = "t-beta", "agent:t-beta:spy"
    header = {"alg": "EdDSA", "typ": "gxg-test-identity+jwt", "kid": _jwt.thumbprint(issuer.public_key)}
    assert reason(verifier, {STUB_HEADER: jws(forger, header, claims)}) == "signature"


def test_foreign_issuer_key(verifier: StubVerifier, clock: Clock) -> None:
    other = StubIssuer()
    assert reason(verifier, headers(other, clock)) == "kid"


def test_wrong_audience(issuer: StubIssuer, verifier: StubVerifier, clock: Clock) -> None:
    assert reason(verifier, headers(issuer, clock, audience="telegram")) == "aud"


def test_replay(issuer: StubIssuer, verifier: StubVerifier, clock: Clock) -> None:
    hdrs = headers(issuer, clock)
    verifier.verify(hdrs)
    assert reason(verifier, hdrs) == "replay"


def test_replay_after_60s_is_rejected(issuer: StubIssuer, verifier: StubVerifier, clock: Clock) -> None:
    """ADR criterion 3: an intercepted identity replayed after > 60 s is rejected."""
    hdrs = headers(issuer, clock)
    clock.t = T0 + 66
    assert reason(verifier, hdrs) == "exp"


def test_principal_of_another_tenant(issuer: StubIssuer, verifier: StubVerifier, clock: Clock) -> None:
    assert reason(verifier, headers(issuer, clock, principal="agent:t-beta:spy")) == "principal"


@pytest.mark.parametrize("name", REQUIRED_CLAIMS)
def test_missing_claim(issuer: StubIssuer, verifier: StubVerifier, clock: Clock, name: str) -> None:
    claims = jwt.decode(headers(issuer, clock)[STUB_HEADER], options={"verify_signature": False})
    del claims[name]
    header = {"alg": "EdDSA", "typ": "gxg-test-identity+jwt", "kid": _jwt.thumbprint(issuer.public_key)}
    token = jws(issuer._key, header, claims)
    assert reason(verifier, {STUB_HEADER: token}) == f"claim:{name}"


def test_stub_refuses_outside_test_profile(monkeypatch: pytest.MonkeyPatch, issuer: StubIssuer) -> None:
    for value in (None, "prod", "TEST", ""):
        if value is None:
            monkeypatch.delenv("GXG_PROFILE", raising=False)
        else:
            monkeypatch.setenv("GXG_PROFILE", value)
        with pytest.raises(RuntimeError, match="GXG_PROFILE=test"):
            StubIssuer()
        with pytest.raises(RuntimeError, match="GXG_PROFILE=test"):
            StubVerifier(CONNECTOR, issuer.public_key)
