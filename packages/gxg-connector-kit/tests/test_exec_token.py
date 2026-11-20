"""AC-M1-03: execution token verification (EFF-18 п.1, AS-09…AS-11)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kit_testing import REPO, T0, Clock, hs256, jws

from gxg_connector_kit import _jwt
from gxg_connector_kit.exec_token import REQUIRED_CLAIMS, ExecTokenVerifier, ExecutorJwks, parse_jwks

CONNECTOR = "demo"
KID = "exec-1"
SCHEMA = json.loads((REPO / "schemas/execution-token.schema.json").read_text(encoding="utf-8"))


def jwk(kid: str, key: Ed25519PrivateKey) -> dict[str, str]:
    return {"kty": "OKP", "crv": "Ed25519", "kid": kid, "x": _jwt.b64url_encode(_jwt.raw_public_key(key.public_key()))}


class Executor:
    """Fake executor: its key, a mutable JWKS and a fetch counter."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.jwks: list[dict[str, str]] = [jwk(KID, self.key)]
        self.fetches = 0
        self.fail = False

    def fetch(self) -> bytes:
        self.fetches += 1
        if self.fail:
            raise OSError("executor unreachable")
        return json.dumps({"keys": self.jwks}).encode()


def claims(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iss": "gxg-executor",
        "aud": CONNECTOR,
        "sub": "int_01",
        "jti": "exe_01",
        "ten": "t-alpha",
        "req": "agent:t-alpha:signals-actions",
        "tool": "demo.message.execute@1.0.0",
        "con": "demo-main",
        "res": "demo:demo-main/board/a-public",
        "ahs": "a" * 64,
        "idk": "cand-1:v1",
        "iat": T0 - 10,
        "exp": T0 + 50,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


HEADER = {"alg": "EdDSA", "typ": "gxg-exec+jwt", "kid": KID}


@pytest.fixture
def executor() -> Executor:
    return Executor()


@pytest.fixture
def verifier(executor: Executor, clock: Clock) -> ExecTokenVerifier:
    return ExecTokenVerifier(CONNECTOR, ExecutorJwks(executor.fetch, now=clock), now=clock)


def reason(verifier: ExecTokenVerifier, token: str) -> str:
    with pytest.raises(_jwt.TokenError) as exc:
        verifier.verify(token)
    assert exc.value.code == "unauthenticated"
    assert exc.value.to_dict() == {"code": "unauthenticated", "message": "token rejected", "retryable": False}
    return exc.value.reason


def test_required_claims_are_the_schema_required() -> None:
    assert tuple(SCHEMA["required"]) == REQUIRED_CLAIMS


def test_valid_token_is_accepted(executor: Executor, verifier: ExecTokenVerifier) -> None:
    out = verifier.verify(jws(executor.key, HEADER, claims()))
    assert out["sub"] == "int_01"
    assert out["ahs"] == "a" * 64


@pytest.mark.parametrize(
    ("over", "t"),
    [({"iat": T0 - 64, "exp": T0 - 4}, T0), ({"iat": T0 + 4, "exp": T0 + 64}, T0)],
    ids=["exp_4s_past", "iat_4s_future"],
)
def test_clock_skew_of_4s_is_accepted(
    executor: Executor, verifier: ExecTokenVerifier, clock: Clock, over: dict[str, int], t: int
) -> None:
    clock.t = t
    assert verifier.verify(jws(executor.key, HEADER, claims(**over)))["jti"] == "exe_01"


# The 9 cases of AC-M1-03, plus typ and TTL.


def test_foreign_signature(verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(Ed25519PrivateKey.generate(), HEADER, claims())) == "signature"


def test_tampered_payload(executor: Executor, verifier: ExecTokenVerifier) -> None:
    h, _, s = jws(executor.key, HEADER, claims()).split(".")
    p = _jwt.b64url_encode(json.dumps(claims(ahs="b" * 64)).encode())
    assert reason(verifier, f"{h}.{p}.{s}") == "signature"


def test_unknown_kid(executor: Executor, verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(executor.key, {**HEADER, "kid": "exec-x"}, claims())) == "kid"


@pytest.mark.parametrize(("kid", "why"), [(None, "kid"), ("", "kid"), (7, "malformed")])  # PyJWT rejects non-str kid
def test_missing_or_bad_kid(executor: Executor, verifier: ExecTokenVerifier, kid: Any, why: str) -> None:
    header = {k: v for k, v in {**HEADER, "kid": kid}.items() if v is not None}
    assert reason(verifier, jws(executor.key, header, claims())) == why


def test_wrong_issuer(executor: Executor, verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(executor.key, HEADER, claims(iss="gxg-approval"))) == "iss"


def test_wrong_audience(executor: Executor, verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(executor.key, HEADER, claims(aud="telegram"))) == "aud"


def test_audience_list_is_not_accepted(executor: Executor, verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(executor.key, HEADER, claims(aud=[CONNECTOR]))) == "claim:aud"


def test_expired_more_than_5s(executor: Executor, verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(executor.key, HEADER, claims(iat=T0 - 66, exp=T0 - 6))) == "exp"


def test_issued_in_future_more_than_5s(executor: Executor, verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(executor.key, HEADER, claims(iat=T0 + 6, exp=T0 + 66))) == "iat"


@pytest.mark.parametrize("name", REQUIRED_CLAIMS)
def test_missing_required_claim(executor: Executor, verifier: ExecTokenVerifier, name: str) -> None:
    c = claims()
    del c[name]
    assert reason(verifier, jws(executor.key, HEADER, c)) == f"claim:{name}"


@pytest.mark.parametrize(("name", "value"), [("iat", "1"), ("exp", True), ("sub", 5), ("ahs", "")])
def test_claim_of_wrong_type(executor: Executor, verifier: ExecTokenVerifier, name: str, value: Any) -> None:
    assert reason(verifier, jws(executor.key, HEADER, claims(**{name: value}))) == f"claim:{name}"


def test_alg_none(verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(None, {**HEADER, "alg": "none"}, claims())) == "alg"


def test_alg_hs256(verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, hs256(HEADER, claims())) == "alg"


def test_alg_header_eddsa_but_hmac_signature(verifier: ExecTokenVerifier) -> None:
    h, p, s = hs256(HEADER, claims()).split(".")
    h = _jwt.b64url_encode(json.dumps(HEADER).encode())
    assert reason(verifier, f"{h}.{p}.{s}") == "signature"


def test_replayed_jti(executor: Executor, verifier: ExecTokenVerifier) -> None:
    token = jws(executor.key, HEADER, claims())
    verifier.verify(token)
    assert reason(verifier, token) == "replay"
    # A new token with the same jti is a replay too.
    assert reason(verifier, jws(executor.key, HEADER, claims(iat=T0 - 9))) == "replay"


def test_rejected_token_does_not_burn_its_jti(executor: Executor, verifier: ExecTokenVerifier) -> None:
    reason(verifier, jws(executor.key, HEADER, claims(aud="other")))
    assert verifier.verify(jws(executor.key, HEADER, claims()))["jti"] == "exe_01"


def test_wrong_typ(executor: Executor, verifier: ExecTokenVerifier) -> None:
    assert reason(verifier, jws(executor.key, {**HEADER, "typ": "gxg-svc+jwt"}, claims())) == "typ"


@pytest.mark.parametrize(("iat", "exp"), [(T0 - 10, T0 + 51), (T0, T0), (T0, T0 - 1)])
def test_lifetime_over_60s_or_not_positive(executor: Executor, verifier: ExecTokenVerifier, iat: int, exp: int) -> None:
    assert reason(verifier, jws(executor.key, HEADER, claims(iat=iat, exp=exp))) == "ttl"


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "x" * 9000])
def test_malformed(verifier: ExecTokenVerifier, token: str) -> None:
    assert reason(verifier, token) == "malformed"


# --- JWKS cache (AS-09, AS-10) ------------------------------------------------------------


def test_jwks_is_cached_for_10_minutes(executor: Executor, verifier: ExecTokenVerifier, clock: Clock) -> None:
    verifier.verify(jws(executor.key, HEADER, claims(jti="e1")))
    clock.t = T0 + 599
    verifier.verify(jws(executor.key, HEADER, claims(jti="e2", iat=T0 + 590, exp=T0 + 640)))
    assert executor.fetches == 1
    clock.t = T0 + 600
    verifier.verify(jws(executor.key, HEADER, claims(jti="e3", iat=T0 + 590, exp=T0 + 640)))
    assert executor.fetches == 2


def test_compromised_kid_removed_from_jwks_is_rejected_after_refresh(
    executor: Executor, verifier: ExecTokenVerifier, clock: Clock
) -> None:
    verifier.verify(jws(executor.key, HEADER, claims(jti="e1")))
    executor.jwks = []
    clock.t = T0 + 600
    assert reason(verifier, jws(executor.key, HEADER, claims(jti="e2", iat=T0 + 590, exp=T0 + 640))) == "kid"


def test_rotation_new_kid_is_accepted_and_old_one_while_published(
    executor: Executor, verifier: ExecTokenVerifier, clock: Clock
) -> None:
    new = Ed25519PrivateKey.generate()
    executor.jwks.append(jwk("exec-2", new))
    verifier.verify(jws(new, {**HEADER, "kid": "exec-2"}, claims(jti="e1")))
    verifier.verify(jws(executor.key, HEADER, claims(jti="e2")))


def test_unknown_kid_does_not_trigger_refetch(executor: Executor, verifier: ExecTokenVerifier) -> None:
    for i in range(3):
        reason(verifier, jws(executor.key, {**HEADER, "kid": "nope"}, claims(jti=f"e{i}")))
    assert executor.fetches == 1


def test_failed_refresh_trusts_no_key(executor: Executor, verifier: ExecTokenVerifier, clock: Clock) -> None:
    verifier.verify(jws(executor.key, HEADER, claims(jti="e1")))
    executor.fail = True
    clock.t = T0 + 600
    assert reason(verifier, jws(executor.key, HEADER, claims(jti="e2", iat=T0 + 590, exp=T0 + 640))) == "kid"
    executor.fail = False
    verifier.verify(jws(executor.key, HEADER, claims(jti="e3", iat=T0 + 590, exp=T0 + 640)))


def test_parse_jwks_ignores_other_key_types_and_rejects_malformed_ed25519() -> None:
    key = Ed25519PrivateKey.generate()
    doc = {"keys": [{"kty": "RSA", "kid": "r", "n": "x", "e": "AQAB"}, jwk("ok", key)]}
    assert set(parse_jwks(json.dumps(doc).encode())) == {"ok"}
    for bad in ({**jwk("b", key), "x": "AAAA"}, {**jwk("b", key), "kid": ""}, {**jwk("b", key), "x": "a+b/"}):
        with pytest.raises(ValueError):
            parse_jwks(json.dumps({"keys": [bad]}).encode())


def test_malformed_jwks_means_no_keys(executor: Executor, clock: Clock) -> None:
    v = ExecTokenVerifier(CONNECTOR, ExecutorJwks(lambda: b"not json", now=clock), now=clock)
    assert reason(v, jws(executor.key, HEADER, claims())) == "kid"


def test_jti_cache_forgets_expired_entries() -> None:
    cache = _jwt.JtiCache()
    assert cache.add("a", 100, 50)
    assert not cache.add("a", 100, 99)
    assert cache.add("a", 200, 101)
