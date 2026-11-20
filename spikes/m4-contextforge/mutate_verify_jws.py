"""Mutation self-check for verify_jws.py: drop one check, the attack it guards must become accepted.

Usage: python3 -I mutate_verify_jws.py [secrets/echo.variant-c.jsonl]  (raw signed log kept by run_spike.sh)
Exit 1 if the unmutated verifier accepts any attack or a mutation goes unnoticed.
"""

import contextlib
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "secrets" / "echo.variant-c.jsonl")
SRC = (HERE / "verify_jws.py").read_text()
JTI = 'raise ValueError("replayed jti")'
# Delayed and tampered replays are also caught by the jti cache. A production jti store keeps entries only
# until exp, so exp and ahs must hold on their own: those mutations drop the jti check too.
MUTATIONS = {  # dropped checks -> attack that must now be accepted
    ('raise ValueError("bad signature") from None',): "forged_jws_attacker_key",
    ('raise ValueError("expired or not yet valid")', JTI): "replay_after_65s",
    ('raise ValueError("arguments hash mismatch")', JTI): "replay_tampered_args",
    (JTI,): "replay_immediate",
}


def accepted_attacks(src: str) -> set[str]:
    out = io.StringIO()
    sys.argv = ["verify_jws.py", LOG]
    with contextlib.redirect_stdout(out):
        code = compile(src, "verify_jws_mutant.py", "exec")
        exec(code, {"__name__": "__main__", "__file__": str(HERE / "verify_jws.py")})  # noqa: S102 — our own file
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    return {r["source"] for r in rows if r["source"] != "via-contextforge" and "accepted" in r}


ok = True
base = accepted_attacks(SRC)
print(f"unmutated: attacks accepted = {sorted(base)}")
ok &= not base
for checks, attack in MUTATIONS.items():
    mutant = SRC
    for check in checks:
        if check not in mutant:
            raise SystemExit(f"check not found in verify_jws.py: {check}")
        mutant = mutant.replace(check, "pass")
    caught = attack in accepted_attacks(mutant)
    ok &= caught
    print(f"drop {' + '.join(checks)}: {attack} accepted -> {'yes (mutation caught)' if caught else 'NO'}")
sys.exit(0 if ok else 1)
