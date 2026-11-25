"""Connector-side check of ContextForge v1.1.1 claim signatures on captured echo requests.

Formula, from mcpgateway/utils/identity_propagation.py (v1.1.1, build_identity_headers/_sign_claims):
    X-Forwarded-User-Claims-Signature = hex(HMAC-SHA256(IDENTITY_CLAIMS_SECRET or JWT_SECRET_KEY, f"{user_id}:{email}"))
Usage: python3 -I verify_hmac.py  (reads .env and secrets/raw/echo.jsonl). Prints facts only, no secrets.
"""

import hashlib
import hmac
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = dict(line.split("=", 1) for line in (HERE / ".env").read_text().split() if "=" in line)
KEY = ENV["IDENTITY_CLAIMS_SECRET"].encode()
P = "x-forwarded-user"


def sig(user_id: str, email: str) -> str:
    return hmac.new(KEY, f"{user_id}:{email}".encode(), hashlib.sha256).hexdigest()


def valid(h: dict) -> bool:
    got = h.get(f"{P}-claims-signature", "")
    return hmac.compare_digest(got, sig(h.get(f"{P}-id", ""), h.get(f"{P}-email", "")))


rows = [json.loads(x) for x in (HERE / "secrets" / "raw" / "echo.jsonl").read_text().splitlines()]
calls = [r for r in rows if r["body"].get("method") == "tools/call"]
signed = [r for r in calls if any(k.lower() == f"{P}-claims-signature" for k in r["headers"])]
by_user = defaultdict(set)
checks = []
for r in signed:
    h = {k.lower(): v for k, v in r["headers"].items()}
    by_user[h[f"{P}-id"]].add(h[f"{P}-claims-signature"])
    checks.append(valid(h))

h = {k.lower(): v for k, v in signed[0]["headers"].items()}
tampered_teams = dict(h, **{f"{P}-teams": "some-other-team-id", f"{P}-admin": "true"})
tampered_id = dict(h, **{f"{P}-id": "analyst@t-beta.spike.example.com"})
other_key = hmac.new(
    ENV["JWT_SECRET_KEY"].encode(), f"{h[f'{P}-id']}:{h[f'{P}-email']}".encode(), hashlib.sha256
).hexdigest()

result = {
    "tools_call_requests_seen_by_echo": len(calls),
    "with_signature": len(signed),
    "signatures_valid": f"{sum(checks)}/{len(checks)}",
    "distinct_signatures_per_user": {u: len(s) for u, s in by_user.items()},
    "signature_still_valid_after_changing_teams_and_admin": valid(tampered_teams),
    "signature_valid_after_changing_user_id": valid(tampered_id),
    "signature_matches_JWT_SECRET_KEY_when_IDENTITY_CLAIMS_SECRET_is_set": other_key == h[f"{P}-claims-signature"],
    "signed_fields": "user_id:email only (no teams, admin, time, nonce, audience, method, path, body)",
}
print(json.dumps(result, indent=1))
