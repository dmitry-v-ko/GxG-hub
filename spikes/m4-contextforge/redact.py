"""Copy raw spike output (secrets/, git-ignored) to evidence/ with credentials removed.

Usage: python3 -I redact.py <variant>   (a | c | upgrade)
- Authorization / tokens / HMAC signatures -> "<redacted:len=N>" (HMAC prefix kept to show it repeats).
- X-GxG-Identity JWS -> decoded header and claims, signature redacted.
"""

import base64
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRET_KEY = re.compile(r"(authorization|token|password|secret|cookie)", re.I)


def ub64(s):
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def scrub(obj, key=""):  # noqa: PLR0911 — one return per value kind
    if isinstance(obj, dict):
        return {k: scrub(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, key) for v in obj]
    if not isinstance(obj, str):
        return obj
    k = key.lower()
    if k == "x-gxg-identity":
        h, c, s = obj.split(".")
        return {"jws_header": ub64(h), "jws_claims": ub64(c), "signature": f"<redacted:len={len(s)}>"}
    if k.endswith("claims-signature"):
        return f"{obj[:8]}…<redacted:len={len(obj)}>"
    if SECRET_KEY.search(k) and k not in ("auth_method", "x-forwarded-user-auth-method"):
        return f"<redacted:len={len(obj)}>"
    if obj.startswith("eyJ"):
        return f"<redacted-jwt:len={len(obj)}>"
    return obj


def copy(src: Path, dst: Path):
    out = [json.dumps(scrub(json.loads(line)), sort_keys=True) for line in src.read_text().splitlines() if line.strip()]
    dst.write_text("\n".join(out) + "\n")
    print(f"{dst.relative_to(HERE)}: {len(out)} lines")


if __name__ == "__main__":
    v = sys.argv[1]
    ev = HERE / "evidence"
    ev.mkdir(exist_ok=True)
    copy(HERE / "secrets" / "raw" / "echo.jsonl", ev / f"variant-{v}.echo-received.jsonl")
    copy(HERE / "secrets" / "observations.jsonl", ev / f"variant-{v}.observations.jsonl")
    leaked = [p.name for p in ev.glob(f"variant-{v}.*") if re.search(r"eyJ[A-Za-z0-9_-]{20,}\.eyJ", p.read_text())]
    if leaked:
        raise SystemExit(f"unredacted JWT in {leaked}")
