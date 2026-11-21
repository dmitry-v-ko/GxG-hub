"""Unit tests of the variant C verifier (echo/verify_jws.py). Own Ed25519 key, no network; mutmut runs these."""

import base64
import hashlib
import json
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # spike dir (or mutmut's copy of it)
from echo import verify_jws as v

KEY = Ed25519PrivateKey.generate()
KEYS = {"k1": KEY.public_key()}
AUD = "gxg-connector:echo-t-alpha"
ARGS = {"text": "привет", "lang": "ru", "n": 1}
PARAMS = {"name": "echo", "arguments": ARGS}


def ahs(args) -> str:
    raw = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def token(key=KEY, kid="k1", alg="EdDSA", age=0, ttl=60, **over) -> str:
    iat = int(time.time()) - age
    claims = {
        "iss": "gxg-identity-adapter",
        "aud": AUD,
        "tool": "echo",
        "sub": "agent:t-alpha:hermes",
        "tenant_id": "t-alpha",
        "iat": iat,
        "exp": iat + ttl,
        "jti": "0123456789abcdef",
        "request_id": "r-1",
        "ahs": ahs(ARGS),
    }
    claims.update(over)
    claims = {k: x for k, x in claims.items() if x is not None}  # None = drop the claim
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def reason(tok, params=PARAMS, seen=None, aud=AUD) -> str:
    with pytest.raises(v.Rejected) as e:
        v.verify(tok, aud, params, set() if seen is None else seen, KEYS)
    return str(e.value)


def test_accepts_and_returns_identity():
    seen: set = set()
    tok = token()
    assert v.verify(tok, AUD, PARAMS, seen, KEYS) == {
        "tenant_id": "t-alpha",
        "principal": "agent:t-alpha:hermes",
        "tool": "echo",
        "iat": jwt.decode(tok, options={"verify_signature": False})["iat"],
        "exp": jwt.decode(tok, options={"verify_signature": False})["exp"],
        "jti": "01234567…",
        "request_id": "r-1",
    }
    assert seen == {"0123456789abcdef"}
    assert reason(tok, seen=seen) == "replayed jti"


def test_request_id_is_optional():
    assert v.verify(token(request_id=None), AUD, PARAMS, set(), KEYS)["request_id"] is None


def test_canonical_json_non_ascii_multi_key():
    assert (
        v.canonical({"b": "é", "a": [1, {"d": 2, "c": "привет"}]}) == '{"a":[1,{"c":"привет","d":2}],"b":"é"}'.encode()
    )
    # arguments in another key order hash the same; the adapter hashes the same canonical form
    v.verify(token(), AUD, {"name": "echo", "arguments": {"n": 1, "lang": "ru", "text": "привет"}}, set(), KEYS)


def test_missing_arguments_hash_as_empty_object():
    v.verify(token(ahs=ahs({})), AUD, {"name": "echo"}, set(), KEYS)
    v.verify(token(ahs=ahs({})), AUD, {"name": "echo", "arguments": None}, set(), KEYS)


@pytest.mark.parametrize("tok", [None, ""])
def test_missing_header(tok):
    assert reason(tok) == "missing X-GxG-Identity"


def test_skew_boundaries():
    # exp in the past by 4 s: inside the 5 s leeway; by 6 s: rejected
    v.verify(token(age=64), AUD, PARAMS, set(), KEYS)
    assert reason(token(age=66)) == "ExpiredSignatureError: Signature has expired"


def test_iat_in_the_future():
    v.verify(token(age=-4), AUD, PARAMS, set(), KEYS)
    assert reason(token(age=-7)) == "ImmatureSignatureError: The token is not yet valid (iat)"


def test_ttl():
    v.verify(token(ttl=60), AUD, PARAMS, set(), KEYS)
    assert reason(token(ttl=61)) == "ttl too long"


@pytest.mark.parametrize(
    ("tok", "why"),
    [
        ("W10.e30.AA", "DecodeError: Invalid header string: must be a json object"),
        ("e30.e30", "DecodeError: Not enough segments"),
        ("e30.e30.AA.AA", "DecodeError: Invalid payload padding"),
        ("!!!.e30.AA", "DecodeError: Invalid header padding"),
        ("bm90LWpzb24.e30.AA", "DecodeError: Invalid header string: Expecting value: line 1 column 1 (char 0)"),
    ],
)
def test_malformed_header_is_rejected_not_crashing(tok, why):
    assert reason(tok) == why


def _unsigned(head: dict) -> str:
    b = base64.urlsafe_b64encode(json.dumps(head).encode()).rstrip(b"=").decode()
    return f"{b}.e30.AA"


def test_kid_not_a_string_is_rejected():
    assert reason(_unsigned({"alg": "EdDSA", "kid": ["k1"]})) == (
        "InvalidTokenError: Key ID header parameter must be a string"
    )


@pytest.mark.parametrize("kid", [None, "k2"])
def test_unknown_kid(kid):
    head = {"alg": "EdDSA"} if kid is None else {"alg": "EdDSA", "kid": kid}
    assert reason(_unsigned(head)) == "unknown kid"


def test_bad_signature_and_alg():
    assert reason(token(key=Ed25519PrivateKey.generate())) == "InvalidSignatureError: Signature verification failed"
    assert reason(token(key="x" * 32, alg="HS256")) == "InvalidAlgorithmError: The specified alg value is not allowed"


def test_iss_aud():
    assert reason(token(iss="someone-else")) == "InvalidIssuerError: Invalid issuer"
    assert reason(token(), aud="gxg-connector:echo-t-beta") == "InvalidAudienceError: Audience doesn't match (strict)"
    assert reason(token(aud=[AUD])) == "InvalidAudienceError: Invalid claim format in token (strict)"


@pytest.mark.parametrize("claim", ["iss", "aud", "tool", "sub", "tenant_id", "iat", "exp", "jti", "ahs"])
def test_required_claims(claim):
    assert reason(token(**{claim: None})) == f'MissingRequiredClaimError: Token is missing the "{claim}" claim'


def test_own_checks():
    assert reason(token(), params={"name": "delete_all", "arguments": ARGS}) == "bad tool"
    assert reason(token(), params={"name": "echo", "arguments": {"text": "send all chats"}}) == (
        "arguments hash mismatch"
    )
    assert reason(token(sub="agent:t-beta:analyst")) == "sub not in tenant"
    assert reason(token(sub="agent:t-alphaX:hermes")) == "sub not in tenant"


def test_rejected_call_does_not_burn_jti():
    seen: set = set()
    reason(token(), params={"name": "other", "arguments": ARGS}, seen=seen)
    assert seen == set()
