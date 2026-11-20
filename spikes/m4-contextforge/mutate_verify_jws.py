"""Mutation self-check for verify_jws.py: drop one check, the attack it guards must become accepted.

Usage: python3 -I mutate_verify_jws.py [secrets/echo.variant-c.jsonl]  (raw signed log kept by run_spike.sh)
Live attacks come from the log. Checks no live attack can reach (iss, kid, max TTL, sub in tenant) get forged
requests signed with the real adapter key (secrets/), appended to a copy of the log.
Exit 1 if the unmutated verifier accepts any attack or a mutation goes unnoticed.
"""

import base64
import contextlib
import io
import json
import sys
import tempfile
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_private_key

HERE = Path(__file__).resolve().parent
LOG = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "secrets" / "echo.variant-c.jsonl")
SRC = (HERE / "verify_jws.py").read_text()
JTI = 'raise ValueError("replayed jti")'
# An attacker on the path can drop the original and send a changed copy, so its jti is fresh: aud, tool and ahs
# must hold on their own. The jti store forgets entries after exp, so exp must too. Those mutations drop jti as well.
MUTATIONS = {  # dropped checks -> attack that must now be accepted
    ('raise ValueError("bad signature") from None',): "forged_jws_attacker_key",
    ('raise ValueError("expired or not yet valid")', JTI): "replay_after_65s",
    ('raise ValueError("arguments hash mismatch")', JTI): "replay_tampered_args",
    (JTI,): "replay_immediate",
    ('raise ValueError("bad aud")', JTI): "replay_other_connector",
    ('raise ValueError("bad tool")', JTI): "replay_other_tool",
    ('raise ValueError("ttl too long")',): "forged_ttl_3600",
    ('raise ValueError("bad iss")',): "forged_bad_iss",
    ('raise ValueError("unknown kid")',): "forged_unknown_kid",
    ('raise ValueError("sub not in tenant")',): "forged_sub_other_tenant",
}
# Not mutated: the alg check is an equivalent mutant here (Ed25519 verify rejects anything else anyway).


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def ub64(s: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def forged_rows(rows: list[dict]) -> list[dict]:
    """Validly signed by the adapter key, each breaking one claim rule, based on the first delivered call."""
    key = load_pem_private_key((HERE / "secrets" / "adapter_ed25519.pem").read_bytes(), password=None)

    def jws(r):
        return next((v for k, v in r["headers"].items() if k.lower() == "x-gxg-identity"), None)

    src = next(r for r in rows if r["body"].get("method") == "tools/call" and jws(r))
    head_b, claims_b, _ = jws(src).split(".")
    head, claims = ub64(head_b), ub64(claims_b)
    cases = {
        "forged_ttl_3600": ({}, {"exp": claims["iat"] + 3600}),
        "forged_bad_iss": ({}, {"iss": "someone-else"}),
        "forged_unknown_kid": ({"kid": "spike-2"}, {}),
        "forged_sub_other_tenant": ({}, {"sub": "agent:t-beta:analyst"}),
    }
    out = []
    for tag, (h_upd, c_upd) in cases.items():
        h = json.dumps({**head, **h_upd}, sort_keys=True, separators=(",", ":")).encode()
        c = json.dumps({**claims, **c_upd, "jti": uuid.uuid4().hex}, sort_keys=True, separators=(",", ":")).encode()
        si = b64(h) + "." + b64(c)
        token = si + "." + b64(key.sign(si.encode()))
        out.append({**src, "headers": {"X-Spike-Attack": tag, "X-GxG-Identity": token}})
    return out


def accepted_attacks(src: str, log: Path) -> set[str] | str:
    out = io.StringIO()
    sys.argv = ["verify_jws.py", str(log)]
    try:
        with contextlib.redirect_stdout(out):
            code = compile(src, "verify_jws_mutant.py", "exec")
            exec(code, {"__name__": "__main__", "__file__": str(HERE / "verify_jws.py")})  # noqa: S102 — our own file
    except Exception as e:  # a crashing mutant is a red check too
        return f"crash: {type(e).__name__}"
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    return {r["source"] for r in rows if r["source"] != "via-contextforge" and "accepted" in r}


raw = [json.loads(x) for x in LOG.read_text().splitlines()]
with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=HERE / "secrets") as f:
    f.write("".join(json.dumps(r) + "\n" for r in raw + forged_rows(raw)))
full = Path(f.name)
ok = True
base = accepted_attacks(SRC, full)
print(f"unmutated: attacks accepted = {base if isinstance(base, str) else sorted(base)}")
ok &= base == set()
for checks, attack in MUTATIONS.items():
    mutant = SRC
    for check in checks:
        if check not in mutant:
            raise SystemExit(f"check not found in verify_jws.py: {check}")
        mutant = mutant.replace(check, "pass")
    got = accepted_attacks(mutant, full)
    caught = isinstance(got, str) or attack in got
    ok &= caught
    how = got if isinstance(got, str) else f"{attack} accepted"
    print(f"drop {' + '.join(checks)}: {how} -> {'mutation caught' if caught else 'NOT CAUGHT'}")
full.unlink()
sys.exit(0 if ok else 1)
