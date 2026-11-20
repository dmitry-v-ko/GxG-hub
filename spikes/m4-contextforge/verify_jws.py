"""Connector-side verifier for ADR-0001 variant C, run over every request echo received, in arrival order.

It is what gxg-connector-kit's IdentityVerifier must do: Ed25519 signature by the adapter key (kid),
alg pinned to EdDSA, iss, aud = this connector, tool = params.name, iat/exp with 5 s skew and exp - iat <= 60,
ahs = sha256(canonical arguments), jti never seen, sub inside tenant_id. "now" = the moment echo received the request.
shortcut: jti set lives in this process; production keeps it in a store shared by all replicas
of the connector, entries kept until exp + SKEW.
Usage: python3 -I verify_jws.py [echo.jsonl]  -> one line per tools/call: accepted identity or the rejection reason.
"""

import base64
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key

HERE = Path(__file__).resolve().parent
PUB = {"spike-1": load_pem_public_key((HERE / "secrets" / "adapter_ed25519.pub.pem").read_bytes())}
ISS, SKEW, MAX_TTL = "gxg-identity-adapter", 5, 60


def ub64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(obj) -> bytes:
    """SEC-03 canonical JSON: UTF-8, keys sorted, no spaces, non-ASCII as is (not RFC 8785: floats differ)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify(token: str | None, aud: str, params: dict, now: float, seen_jti: set) -> dict:  # noqa: PLR0912
    if not token:
        raise ValueError("missing X-GxG-Identity")
    head_b, claims_b, sig_b = token.split(".")
    head = json.loads(ub64(head_b))
    if head.get("alg") != "EdDSA":
        raise ValueError("bad alg")
    key = PUB.get(head.get("kid"))
    if key is None:
        raise ValueError("unknown kid")
    try:
        key.verify(ub64(sig_b), f"{head_b}.{claims_b}".encode())
    except InvalidSignature:
        raise ValueError("bad signature") from None
    c = json.loads(ub64(claims_b))
    for k in ("iss", "aud", "tool", "sub", "tenant_id", "iat", "exp", "jti", "ahs"):
        if k not in c:
            raise ValueError(f"missing claim {k}")
    if c["iss"] != ISS:
        raise ValueError("bad iss")
    if c["aud"] != aud:
        raise ValueError("bad aud")
    if c["tool"] != params.get("name"):
        raise ValueError("bad tool")
    if not (c["iat"] - SKEW <= now <= c["exp"] + SKEW):
        raise ValueError("expired or not yet valid")
    if c["exp"] - c["iat"] > MAX_TTL:
        raise ValueError("ttl too long")
    if c["ahs"] != hashlib.sha256(canonical(params.get("arguments") or {})).hexdigest():
        raise ValueError("arguments hash mismatch")
    if c["jti"] in seen_jti:
        raise ValueError("replayed jti")
    if not c["sub"].startswith(f"agent:{c['tenant_id']}:"):
        raise ValueError("sub not in tenant")
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


if __name__ == "__main__":
    seen: set = set()
    log = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "secrets" / "raw" / "echo.jsonl"
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    for r in rows:
        if r["body"].get("method") != "tools/call":
            continue
        h = {k.lower(): v for k, v in r["headers"].items()}
        aud = (
            "gxg-connector:echo-" + r["path"].rsplit("/", 1)[-1]
        )  # echo serves /mcp/<tenant>; one connector id per gateway
        now = datetime.fromisoformat(r["at"]).timestamp()
        src = h.get("x-spike-attack", "via-contextforge")
        try:
            out = {"accepted": verify(h.get("x-gxg-identity"), aud, r["body"]["params"], now, seen)}
        except (ValueError, KeyError) as e:
            out = {"rejected": str(e)}
        print(json.dumps({"at": r["at"], "source": src, "path": r["path"], **out}, sort_keys=True))
