"""DoD-4 mutation sweep for gxg-connector-kit: one condition changed → the kit tests must fail.

Usage: uv run python scripts/ci/mutate_kit.py [--out report.json] [name ...]
Each mutation replaces one exact snippet (it must occur exactly once, so a stale list
fails loudly), runs the kit tests, and restores the file. Exit code 1 if a mutation not
marked equivalent survives. Not a CI job: ~70 pytest runs; run it once per cycle.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "packages/gxg-connector-kit/src/gxg_connector_kit"
TESTS = "packages/gxg-connector-kit/tests"

# (name, file, old, new, why it is equivalent or None)
MUTATIONS: list[tuple[str, str, str, str, str | None]] = [
    # --- shared JWS core (_jwt.py): AS-06, AS-09…AS-11, EFF-18 п.1
    ("alg header check off", "_jwt.py", 'if header.get("alg") != "EdDSA":', "if False:", None),
    (
        "pyjwt algorithms widened",
        "_jwt.py",
        'algorithms=["EdDSA"],',
        'algorithms=["EdDSA", "HS256", "none"],',
        "the explicit header alg check runs first; its own mutation is caught",
    ),
    ("typ check off", "_jwt.py", "if header.get(\"typ\") != typ:", "if False:", None),
    (
        "kid empty allowed",
        "_jwt.py",
        "if not isinstance(kid, str) or not kid:",
        "if not isinstance(kid, str):",
        "no key has an empty kid: JWKS lookup, registry thumbprint and stub kid all reject it with the same reason",
    ),
    ("token length limit off", "_jwt.py", "len(token) > _MAX_TOKEN_LEN", "len(token) > 10**9", None),
    ("iss check off", "_jwt.py", 'if claims["iss"] not in issuers:', "if False:", None),
    ("aud check off", "_jwt.py", 'if claims["aud"] != audience:', "if False:", None),
    ("iat skew +1", "_jwt.py", "if iat > now + SKEW_S:", "if iat > now + SKEW_S + 1:", None),
    ("iat skew -1 (4 s)", "_jwt.py", "if iat > now + SKEW_S:", "if iat > now + SKEW_S - 1:", None),
    ("iat check off", "_jwt.py", "if iat > now + SKEW_S:", "if False:", None),
    ("exp skew +1", "_jwt.py", "if now > exp + SKEW_S:", "if now > exp + SKEW_S + 1:", None),
    ("exp skew -1 (4 s)", "_jwt.py", "if now > exp + SKEW_S:", "if now > exp + SKEW_S - 1:", None),
    ("exp check off", "_jwt.py", "if now > exp + SKEW_S:", "if False:", None),
    ("ttl max +1", "_jwt.py", "if not 0 < exp - iat <= MAX_TTL_S:", "if not 0 < exp - iat <= MAX_TTL_S + 1:", None),
    ("ttl zero allowed", "_jwt.py", "if not 0 < exp - iat <= MAX_TTL_S:", "if not 0 <= exp - iat <= MAX_TTL_S:", None),
    (
        "jti check off",
        "_jwt.py",
        'if not jti_cache.add(f"{claims[\'iss\']}\\x00{claims[\'jti\']}", exp + SKEW_S, now):',
        "if False:",
        None,
    ),
    (
        "jti lifetime now + 1",
        "_jwt.py",
        "claims['jti']}\", exp + SKEW_S, now)",
        "claims['jti']}\", now + 1, now)",
        None,
    ),
    ("jti lifetime exp", "_jwt.py", "claims['jti']}\", exp + SKEW_S, now)", "claims['jti']}\", exp, now)", None),
    (
        "jti lifetime exp - 30",
        "_jwt.py",
        "claims['jti']}\", exp + SKEW_S, now)",
        "claims['jti']}\", exp - 30, now)",
        None,
    ),
    (
        "jti lifetime iat + 1",
        "_jwt.py",
        "claims['jti']}\", exp + SKEW_S, now)",
        "claims['jti']}\", iat + 1, now)",
        None,
    ),
    ("jti key without iss", "_jwt.py", "f\"{claims['iss']}\\x00{claims['jti']}\"", "claims['jti']", None),
    ("jti prune keeps nothing", "_jwt.py", "if t < now]", "if True]", None),
    ("jti seen check off", "_jwt.py", "            if key in self._seen:\n", "            if False:\n", None),
    (
        "int claim accepts bool",
        "_jwt.py",
        "if not isinstance(value, int) or isinstance(value, bool):",
        "if not isinstance(value, int):",
        None,
    ),
    (
        "str claim accepts empty",
        "_jwt.py",
        "elif not isinstance(value, str) or not value:",
        "elif not isinstance(value, str):",
        None,
    ),
    ("b64url strict alphabet off", "_jwt.py", "not _B64URL.fullmatch(data) or ", "", None),
    (
        "b64url length check off",
        "_jwt.py",
        " or len(data) % 4 == 1",
        "",
        "urlsafe_b64decode raises binascii.Error (a ValueError) for length 1 mod 4 anyway",
    ),
    (
        "registry key length off",
        "_jwt.py",
        "if len(raw) != 32:",
        "if False:",
        "Ed25519PublicKey.from_public_bytes raises ValueError for any length other than 32",
    ),
    # --- execution token (exec_token.py)
    ("exec issuer", "exec_token.py", 'ISSUER = "gxg-executor"', 'ISSUER = "gxg-approval"', None),
    (
        "JWKS max age +1",
        "exec_token.py",
        "t - self._fetched_at >= self._max_age_s",
        "t - self._fetched_at > self._max_age_s",
        None,
    ),
    (
        "JWKS keep stale keys on failure",
        "exec_token.py",
        "                    self._keys = {}\n",
        "                    pass\n",
        None,
    ),
    (
        "JWKS kid check off",
        "exec_token.py",
        "        if found is None:\n            raise TokenError(\"kid\")",
        "        if found is None:\n            found = next(iter(self._current().values()))",
        None,
    ),
    (
        "JWKS non-Ed25519 accepted",
        "exec_token.py",
        'if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":',
        'if jwk.get("kty") != "OKP":',
        None,
    ),
    ("JWKS readiness always ready", "exec_token.py", "        if not self._current():\n", "        if False:\n", None),
    # --- service JWT (svc_jwt.py)
    (
        "svc unregistered iss",
        "svc_jwt.py",
        "            if service is None:\n                raise TokenError(\"iss_unregistered\")",
        "            if service is None:\n                service = next(iter(services.values()))",
        None,
    ),
    ("svc kid thumbprint check off", "svc_jwt.py", "if kid != _jwt.thumbprint(service.public_key):", "if False:", None),
    (
        "svc serves any tenant",
        "svc_jwt.py",
        'return tenant in self.tenants or "*" in self.tenants',
        "return True",
        None,
    ),
    ("svc sign ttl limit off", "svc_jwt.py", "if not 0 < ttl_s <= _jwt.MAX_TTL_S:", "if False:", None),
    ("svc duplicate service allowed", "svc_jwt.py", 'if s["id"] in services:', "if False:", None),
    # --- grants (grants.py)
    ("bundle signature check off", "grants.py", "        key.verify(signature, data)\n", "        pass\n", None),
    ("bundle alg check off", "grants.py", 'or envelope.get("alg") != "EdDSA"', "", None),
    (
        "bundle future built_at off",
        "grants.py",
        "if bundle.built_at.timestamp() > self._now() + _jwt.SKEW_S:",
        "if False:",
        None,
    ),
    (
        "bundle future skew +60",
        "grants.py",
        "if bundle.built_at.timestamp() > self._now() + _jwt.SKEW_S:",
        "if bundle.built_at.timestamp() > self._now() + 60:",
        None,
    ),
    (
        "bundle future skew -1",
        "grants.py",
        "if bundle.built_at.timestamp() > self._now() + _jwt.SKEW_S:",
        "if bundle.built_at.timestamp() > self._now() + _jwt.SKEW_S - 1:",
        None,
    ),
    (
        "rollback check off",
        "grants.py",
        "if self._bundle is not None and bundle.built_at < self._bundle.built_at:",
        "if False:",
        None,
    ),
    (
        "rollback rejects equal",
        "grants.py",
        "bundle.built_at < self._bundle.built_at:",
        "bundle.built_at <= self._bundle.built_at:",
        None,
    ),
    (
        "staleness in readiness off",
        "grants.py",
        "        if self._age_s(bundle) > self._max_age_s:\n",
        "        if False:\n",
        None,
    ),
    (
        "staleness boundary",
        "grants.py",
        "        if self._age_s(bundle) > self._max_age_s:\n",
        "        if self._age_s(bundle) >= self._max_age_s:\n",
        None,
    ),
    (
        "staleness in current off",
        "grants.py",
        "if bundle is None or self._age_s(bundle) > self._max_age_s:",
        "if bundle is None:",
        None,
    ),
    ("built_at UTC check off", "grants.py", "if built_at.utcoffset() != timedelta(0):", "if False:", None),
    (
        "other tenant's file accepted",
        "grants.py",
        "if parsed.tenant != tid or (policy is not None and policy.get(\"tenant\") != tid):",
        "if False:",
        None,
    ),
    (
        "tenant slice off",
        "grants.py",
        "            if tenant is not None and tid != tenant:\n                continue\n",
        "",
        None,
    ),
    (
        "RID trailing newline ($)",
        "grants.py",
        r'([^/*\s]+)")',
        r'([^/*\s]+)$")',
        "with fullmatch a `$` before a final newline still needs the end of the string; "
        "the fullmatch mutation is caught",
    ),
    ("RID fullmatch -> match", "grants.py", "        m = _RID.fullmatch(rid)", "        m = _RID.match(rid)", None),
    (
        "mask matches any type",
        "grants.py",
        "(domain, connection, rtype) in g.masks:",
        "any(x[:2] == (domain, connection) for x in g.masks):",
        None,
    ),
    (
        "foreign connection allowed",
        "grants.py",
        "        if t.connections.get(connection) != domain:",
        "        if False:",
        None,
    ),
    (
        "foreign domain allowed",
        "grants.py",
        "        if t.connections.get(connection) != domain:",
        "        if connection not in t.connections:",
        None,
    ),
    ("expiry off", "grants.py", "(g.expires_at is None or now < g.expires_at)", "True", None),
    ("expiry boundary", "grants.py", "now < g.expires_at)", "now <= g.expires_at)", None),
    (
        "principal listed check off",
        "grants.py",
        "if t is None or principal not in t.principals:",
        "if t is None:",
        None,
    ),
    ("action check off", "grants.py", " and action in g.actions and ", " and ", None),
    ("subject check off", "grants.py", "if principal in g.subjects and ", "if ", None),
    (
        "unknown group as literal subject",
        "grants.py",
        'groups.get(s[len("group:") :], [])',
        'groups.get(s[len("group:") :], [s])',
        "a group:<id> subject is never a principal ID, so the principal check denies it",
    ),
    ("allowed() prefix off", "grants.py", "for r in g.exact if r.startswith(prefix)", "for r in g.exact", None),
    (
        "allowed() connection check off",
        "grants.py",
        "if found is None or connection_id not in found[0].connections:",
        "if found is None:",
        None,
    ),
    # --- canonical (canonical.py)
    ("canonical no NFC", "canonical.py", 'out = unicodedata.normalize("NFC", value)', "out = value", None),
    ("canonical no sort", "canonical.py", "for k in sorted(items, key=_utf16_key)}", "for k in items}", None),
    ("canonical sort by code point", "canonical.py", "sorted(items, key=_utf16_key)", "sorted(items)", None),
    (
        "canonical float allowed",
        "canonical.py",
        "    if isinstance(value, float):\n        raise",
        "    if isinstance(value, float):\n        return value\n        raise",
        None,
    ),
    ("canonical int range off", "canonical.py", "if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:", "if False:", None),
    # --- identity, redact, errors, readiness
    ("stub profile check off", "identity.py", 'if os.environ.get(PROFILE_ENV) != "test":', "if False:", None),
    ("stub kid check off", "identity.py", "            if kid != self._kid:\n", "            if False:\n", None),
    (
        "stub principal tenant check off",
        "identity.py",
        "if not claims[\"sub\"].startswith(f\"agent:{claims['tenant_id']}:\"):",
        "if False:",
        None,
    ),
    ("redact case sensitive", "redact.py", "k.lower() in SENSITIVE_KEYS", "k in SENSITIVE_KEYS", None),
    ("redact no JWT", "redact.py", "return _JWT.sub(JWT_PLACEHOLDER, value)", "return value", None),
    ("redact skips lists", "redact.py", "return [redact(v) for v in value]", "return list(value)", None),
    ("error outcome_unknown retryable", "errors.py", 'if code == "outcome_unknown" and retryable:', "if False:", None),
    ("error retry_after bound off", "errors.py", "or not 0 <= retry_after_s <= _RETRY_AFTER_MAX_S", "", None),
    (
        "error details bound off",
        "errors.py",
        "if details is not None and len(details) > _DETAILS_MAX_KEYS:",
        "if False:",
        None,
    ),
    (
        "error request_id bound off",
        "errors.py",
        "if request_id is not None and not 1 <= len(request_id) <= _REQUEST_ID_MAX:",
        "if False:",
        None,
    ),
    ("all_ready any", "readiness.py", "all(item.ready for item in items)", "any(item.ready for item in items)", None),
]


def run_tests() -> bool:
    """True if the kit tests pass (the mutation survived)."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        TESTS,
        "-x",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=--import-mode=importlib",
    ]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, check=False).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    selected = [m for m in MUTATIONS if not args.names or m[0] in args.names]
    if not run_tests():
        print("kit tests fail before any mutation", file=sys.stderr)
        return 2
    results: list[dict[str, Any]] = []
    bad = caught_total = 0
    for name, file, old, new, equivalent in selected:
        path = SRC / file
        original = path.read_text(encoding="utf-8")
        if original.count(old) != 1:
            print(f"{name}: snippet occurs {original.count(old)} times in {file}", file=sys.stderr)
            return 2
        line = original[: original.index(old)].count("\n") + 1
        try:
            path.write_text(original.replace(old, new), encoding="utf-8")
            caught = not run_tests()
        finally:
            path.write_text(original, encoding="utf-8")
        bad += not caught and equivalent is None
        caught_total += caught
        results.append(
            {"name": name, "where": f"packages/gxg-connector-kit/src/gxg_connector_kit/{file}:{line}",
             "replace": old.strip(), "with": new.strip(), "caught": caught, "equivalent": equivalent}
        )  # fmt: skip
        print(f"{'caught  ' if caught else 'SURVIVED'} {name}", flush=True)
    summary = {"total": len(results), "caught": caught_total,
               "survived_non_equivalent": bad, "mutations": results}  # fmt: skip
    if args.out:
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{summary['caught']}/{summary['total']} caught, non-equivalent survivors: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
