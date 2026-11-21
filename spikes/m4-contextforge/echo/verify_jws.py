"""Connector-side verifier for ADR-0001 variant C (prototype of gxg-connector-kit's IdentityVerifier).

PyJWT checks the compact JWS: Ed25519 signature by the adapter key (kid), alg pinned to EdDSA, iss,
aud = this connector (a string, strict), iat/exp against the current time with 5 s leeway, required claims.
Ours: tool = params.name, exp - iat <= 60, ahs = sha256(canonical arguments), sub inside tenant_id, jti never seen.
echo_mcp.py calls verify() when a request arrives, so "now" is the arrival time.
shortcut: jti set lives in this process; production keeps it in a store shared by all replicas
of the connector, entries kept until exp + SKEW.
"""

import hashlib
import json

import jwt

ISS, SKEW, MAX_TTL = "gxg-identity-adapter", 5, 60
REQUIRED = ["iss", "aud", "tool", "sub", "tenant_id", "iat", "exp", "jti", "ahs"]


class Rejected(Exception):
    pass


def canonical(obj) -> bytes:
    """SEC-03 canonical JSON: UTF-8, keys sorted, no spaces, non-ASCII as is (not RFC 8785: floats differ)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify(token: str | None, aud: str, params: dict, seen_jti: set, keys: dict) -> dict:
    """Accepted identity, or Rejected(reason). keys: kid -> Ed25519 public key."""
    if not token:
        raise Rejected("missing X-GxG-Identity")
    try:
        key = keys.get(jwt.get_unverified_header(token).get("kid"))  # PyJWT: header is an object, kid a string
        if key is None:
            raise Rejected("unknown kid")
        c = jwt.decode(
            token,
            key,
            algorithms=["EdDSA"],
            audience=aud,
            issuer=ISS,
            leeway=SKEW,
            options={"require": REQUIRED, "strict_aud": True},
        )
    except jwt.PyJWTError as e:
        raise Rejected(f"{type(e).__name__}: {e}") from None
    if c["tool"] != params.get("name"):
        raise Rejected("bad tool")
    if c["exp"] - c["iat"] > MAX_TTL:
        raise Rejected("ttl too long")
    if c["ahs"] != hashlib.sha256(canonical(params.get("arguments") or {})).hexdigest():
        raise Rejected("arguments hash mismatch")
    if c["jti"] in seen_jti:
        raise Rejected("replayed jti")
    if not c["sub"].startswith(f"agent:{c['tenant_id']}:"):
        raise Rejected("sub not in tenant")
    seen_jti.add(c["jti"])
    return {
        "tenant_id": c["tenant_id"],
        "principal": c["sub"],
        "tool": c["tool"],
        "iat": c["iat"],
        "exp": c["exp"],
        "jti": c["jti"][:8] + "…",
        "request_id": c.get("request_id"),
    }
