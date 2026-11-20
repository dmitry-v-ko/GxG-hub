"""Connector-side verifier for the option C identity JWS (prototype of IdentityVerifier).

Rejects (fail closed): missing/duplicate header, bad signature or alg, wrong typ/kid/iss/aud,
missing claims, lifetime > MAX_TTL, expired / not yet valid (LEEWAY), arguments hash mismatch,
replayed jti (in-memory cache until exp).
"""

import hashlib
import json
import threading
import time

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key

ISS, KID, TYP = "gxg-identity-adapter", "gxg-adapter-1", "gxg-id+jwt"
MAX_TTL, LEEWAY = 60, 5
REQUIRED = ["iss", "aud", "sub", "tenant_id", "iat", "exp", "jti", "ahs"]


def args_hash(args):
    raw = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class Verifier:
    def __init__(self, public_key_pem: bytes):
        self._key = load_pem_public_key(public_key_pem)
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, values: list[str], arguments, audience: str):
        """Returns (True, claims) or (False, reason). `values` = all X-GXG-Identity header values."""
        if not values:
            return False, "missing identity"
        if len(values) != 1:
            return False, "duplicate identity header"
        token = values[0]
        try:
            hdr = jwt.get_unverified_header(token)
            if hdr.get("typ") != TYP or hdr.get("kid") != KID or hdr.get("alg") != "EdDSA":
                return False, f"bad header {hdr}"
            claims = jwt.decode(
                token,
                self._key,
                algorithms=["EdDSA"],
                audience=audience,
                issuer=ISS,
                leeway=LEEWAY,
                options={"require": REQUIRED},
            )
        except jwt.PyJWTError as e:
            return False, f"{type(e).__name__}: {e}"
        if claims["exp"] - claims["iat"] > MAX_TTL:
            return False, "lifetime too long"
        if claims["iat"] > time.time() + LEEWAY:
            return False, "iat in the future"
        if claims["ahs"] != args_hash(arguments):
            return False, "arguments hash mismatch"
        with self._lock:
            now = time.time()
            for j in [j for j, exp in self._seen.items() if exp + LEEWAY < now]:
                del self._seen[j]
            if claims["jti"] in self._seen:
                return False, "replayed jti"
            self._seen[claims["jti"]] = claims["exp"]
        return True, {k: claims[k] for k in ("sub", "tenant_id", "jti", "iat", "exp", "tool")}
