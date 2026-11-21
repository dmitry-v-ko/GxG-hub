"""ToolHive token_exchange as the connector would check it: is the RFC 8693 bearer echo received usable as identity?

Usage (mock IdP still running): python3 -I verify_tx.py  -> JSON to stdout (no token values).
For every tools/call echo got: verify the bearer with the IdP JWKS (PyJWT: signature, iss, exp;
aud reported), report claims,
then answer the M4 questions: does it carry tenant/principal, per-call jti, binding to tool/arguments, and do the
direct replays (bypass, tampered args, other tool, other connector, after 65 s) still verify.
"""

import hashlib
import json
from pathlib import Path

import jwt

HERE = Path(__file__).resolve().parent
JWKS = jwt.PyJWKClient("http://127.0.0.1:8080/gxg/jwks")
ISS = "http://127.0.0.1:8080/gxg"


def check(token, aud):
    try:
        key = JWKS.get_signing_key_from_jwt(token).key
        # aud is reported, not required: the mock IdP issues exchanged tokens without aud (see aud_matches_connector)
        c = jwt.decode(token, key, algorithms=["RS256", "ES256"], issuer=ISS, options={"verify_aud": False})
        claims = {k: c[k] for k in sorted(c) if k not in ("nbf", "jti")} | {"jti": c.get("jti", "")[:8] + "…"}
        return {
            "valid": True,
            "aud_matches_connector": aud in ([c["aud"]] if isinstance(c.get("aud"), str) else c.get("aud") or []),
            "claims": claims,
        }
    except jwt.PyJWTError as e:
        return {"valid": False, "error": f"{type(e).__name__}: {e}"}


rows, tokens = [], {}
for line in (HERE / "secrets/raw/echo.jsonl").read_text().splitlines():
    r = json.loads(line)
    if r["body"].get("method") != "tools/call":
        continue
    h = {k.lower(): v for k, v in r["headers"].items()}
    bearer = h.get("authorization", "")[7:] or None
    aud = "gxg-connector:echo-" + r["path"].rsplit("/", 1)[-1]
    res = check(bearer, aud) if bearer else {"valid": False, "error": "no bearer"}
    fp = hashlib.sha256(bearer.encode()).hexdigest()[:12] if bearer else None
    rows.append(
        {
            "at": r["at"],
            "source": h.get("x-spike-attack", "via-gateway"),
            "path": r["path"],
            "args": r["body"]["params"].get("arguments"),
            "token_sha256_12": fp,
            **res,
        }
    )
    if bearer and h.get("x-spike-attack") is None:
        tokens.setdefault((res.get("claims") or {}).get("sub"), set()).add(fp)
via = [x for x in rows if x["source"] == "via-gateway"]
out = {
    "calls": rows,
    "all_gateway_calls_verify_with_idp_jwks": all(x["valid"] for x in via),
    "distinct_tokens_per_subject": {k: len(v) for k, v in tokens.items()},
    "direct_replays_still_valid": {x["source"]: x["valid"] for x in rows if x["source"] != "via-gateway"},
    "jti_reused_across_gateway_calls": len({x["claims"]["jti"] for x in via if x["valid"]}) < len(via),
    "claims_present": sorted({k for x in via if x["valid"] for k in x["claims"]}),
}
print(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False))
