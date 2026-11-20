"""AC-M1-04: service JWT (AS-06, AS-25), keys from the platform registry of the bundle."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kit_testing import T0, BundleKit, Clock, hs256, jws

from gxg_connector_kit import _jwt, svc_jwt
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.svc_jwt import REQUIRED_CLAIMS, Service, SvcJwtVerifier, parse_registry

RECEIVER = "approval-api"
CALLERS = frozenset({"demo"})  # endpoint allowlist, e.g. POST /v1/intents


@pytest.fixture
def registry(bundle_kit: BundleKit) -> dict[str, Service]:
    return parse_registry(bundle_kit.platform)


@pytest.fixture
def demo_key(bundle_kit: BundleKit) -> Ed25519PrivateKey:
    return bundle_kit.service_keys["demo"]


@pytest.fixture
def verifier(registry: dict[str, Service], clock: Clock) -> SvcJwtVerifier:
    return SvcJwtVerifier(RECEIVER, lambda: registry, now=clock)


def header(key: Ed25519PrivateKey, **over: Any) -> dict[str, Any]:
    return {"alg": "EdDSA", "typ": "gxg-svc+jwt", "kid": _jwt.thumbprint(key.public_key()), **over}


def claims(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"iss": "demo", "aud": RECEIVER, "iat": T0 - 10, "exp": T0 + 50, "jti": "j-1"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def reason(verifier: SvcJwtVerifier, token: str, allowed: frozenset[str] = CALLERS) -> str:
    with pytest.raises(_jwt.TokenError) as exc:
        verifier.verify(token, allowed_issuers=allowed)
    assert exc.value.code == "unauthenticated"
    return exc.value.reason


def test_sign_and_verify(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier, clock: Clock) -> None:
    token = svc_jwt.sign(demo_key, iss="demo", aud=RECEIVER, now=clock)
    caller = verifier.verify(token, allowed_issuers=CALLERS)
    assert caller.id == "demo"
    assert caller.kind == "connector"
    assert caller.serves("t-alpha")
    assert not caller.serves("t-gamma")  # AS-25: connector not registered for this tenant


def test_executor_serves_every_tenant(registry: dict[str, Service]) -> None:
    assert registry["executor"].serves("t-anything")


@pytest.mark.parametrize(
    "over",
    [
        {"iat": T0 - 64, "exp": T0 - 4},
        {"iat": T0 + 4, "exp": T0 + 64},
        {"iat": T0 - 65, "exp": T0 - 5},  # AS-11 boundary: exactly 5 s
        {"iat": T0 + 5, "exp": T0 + 65},
    ],
    ids=["exp_4s_past", "iat_4s_future", "exp_5s_past", "iat_5s_future"],
)
def test_clock_skew_up_to_5s_is_accepted(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier, over: Any) -> None:
    assert verifier.verify(jws(demo_key, header(demo_key), claims(**over)), allowed_issuers=CALLERS).id == "demo"


def test_foreign_signature(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    assert reason(verifier, jws(Ed25519PrivateKey.generate(), header(demo_key), claims())) == "signature"


def test_unknown_kid(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    other = Ed25519PrivateKey.generate()
    assert reason(verifier, jws(demo_key, header(other), claims())) == "kid"


def test_issuer_not_allowed_for_endpoint(bundle_kit: BundleKit, verifier: SvcJwtVerifier) -> None:
    key = bundle_kit.service_keys["approval-mcp"]
    assert reason(verifier, jws(key, header(key), claims(iss="approval-mcp"))) == "iss"


def test_issuer_absent_from_registry(verifier: SvcJwtVerifier) -> None:
    key = Ed25519PrivateKey.generate()
    assert reason(verifier, jws(key, header(key), claims(iss="ghost")), frozenset({"ghost"})) == "iss_unregistered"


def test_wrong_audience(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    assert reason(verifier, jws(demo_key, header(demo_key), claims(aud="executor"))) == "aud"


def test_expired_more_than_5s(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    assert reason(verifier, jws(demo_key, header(demo_key), claims(iat=T0 - 66, exp=T0 - 6))) == "exp"


def test_issued_in_future_more_than_5s(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    assert reason(verifier, jws(demo_key, header(demo_key), claims(iat=T0 + 6, exp=T0 + 66))) == "iat"


@pytest.mark.parametrize("name", REQUIRED_CLAIMS)
def test_missing_required_claim(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier, name: str) -> None:
    c = claims()
    del c[name]
    # Without `iss` there is no registry entry to take the key from.
    assert reason(verifier, jws(demo_key, header(demo_key), c)) == (
        "iss_unregistered" if name == "iss" else f"claim:{name}"
    )


def test_alg_none(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    assert reason(verifier, jws(None, header(demo_key, alg="none"), claims())) == "alg"


def test_alg_hs256(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    assert reason(verifier, hs256(header(demo_key), claims())) == "alg"


def test_replayed_jti(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    token = jws(demo_key, header(demo_key), claims())
    verifier.verify(token, allowed_issuers=CALLERS)
    assert reason(verifier, token) == "replay"


@pytest.mark.parametrize("later", [30, 55], ids=["T0+30", "exp+5"])
def test_replayed_jti_later_while_token_is_valid(
    demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier, clock: Clock, later: int
) -> None:
    # exp = T0 + 50: at exp + 5 the token still passes every other check, only the cache rejects it.
    token = jws(demo_key, header(demo_key), claims())
    verifier.verify(token, allowed_issuers=CALLERS)
    clock.t = T0 + later
    assert reason(verifier, token) == "replay"


def test_same_jti_from_two_issuers_is_not_a_replay(bundle_kit: BundleKit, verifier: SvcJwtVerifier) -> None:
    # jti is unique per issuer; another service's token must not burn it.
    other = bundle_kit.service_keys["approval-mcp"]
    callers = frozenset({"demo", "approval-mcp"})
    demo = bundle_kit.service_keys["demo"]
    assert verifier.verify(jws(demo, header(demo), claims()), allowed_issuers=callers).id == "demo"
    assert (
        verifier.verify(jws(other, header(other), claims(iss="approval-mcp")), allowed_issuers=callers).id
        == "approval-mcp"
    )


def test_wrong_typ(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    assert reason(verifier, jws(demo_key, header(demo_key, typ="gxg-exec+jwt"), claims())) == "typ"


def test_lifetime_over_60s(demo_key: Ed25519PrivateKey, verifier: SvcJwtVerifier) -> None:
    assert reason(verifier, jws(demo_key, header(demo_key), claims(exp=T0 + 51))) == "ttl"


def test_sign_rejects_long_ttl(demo_key: Ed25519PrivateKey) -> None:
    for ttl in (0, 61):
        with pytest.raises(ValueError, match="ttl_s"):
            svc_jwt.sign(demo_key, iss="demo", aud=RECEIVER, ttl_s=ttl)


def test_registry_comes_from_the_current_bundle(bundle_kit: BundleKit, clock: Clock, demo_key: Any) -> None:
    store = BundleStore(bundle_kit.keys, now=clock)
    store.load(*bundle_kit.signed())
    v = SvcJwtVerifier(RECEIVER, store.registry, now=clock)
    assert v.verify(svc_jwt.sign(demo_key, iss="demo", aud=RECEIVER, now=clock), allowed_issuers=CALLERS).id == "demo"


def test_parse_registry_rejects_bad_documents(bundle_kit: BundleKit) -> None:
    doc = bundle_kit.platform
    with pytest.raises(ValueError, match="schema_version"):
        parse_registry({**doc, "schema_version": 2})
    with pytest.raises(ValueError, match="duplicate"):
        parse_registry({**doc, "services": doc["services"] + doc["services"][:1]})
    bad_key = [{**doc["services"][0], "public_key": "ed25519:shortk"}]
    with pytest.raises(ValueError, match="32 bytes"):
        parse_registry({**doc, "services": bad_key})
    with pytest.raises(ValueError, match="ed25519:"):
        parse_registry({**doc, "services": [{**doc["services"][0], "public_key": "rsa:x"}]})
