# M1 cycle 3 — plan (ponytail full)

Input: `reports/critic/M1-cycle-2.json` (6.0, caps `dod` + `major`) and its review; specs, schemas and plan from `claude/integration` @ e7d245f (stage A audit), merged in the previous commit.
Base: `claude/m1-connector-kit` @ 4a5dd9f. Changes stay in `packages/gxg-connector-kit`, root `pyproject.toml`/`uv.lock` (dev dep `mutmut`), README, report; `scripts/ci/mutate_kit.py` is deleted.

## Scope (every place the change reaches)

| # | Item | Files | Rung |
| --- | --- | --- | --- |
| 1 | DoD-4: mutmut instead of `mutate_kit.py` | kit `pyproject.toml` (`[tool.mutmut]`), `tests/kit_testing.py` (`REPO` = first parent with `uv.lock`), `scripts/ci/mutate_kit.py` (deleted), README, `reports/conformance/M1.json` (`mutmut results` + reason per equivalent survivor) | known tool (mutmut 3.8, root dev dep). One command from the package dir: `uv run mutmut run` |
| 2 | Kill non-equivalent survivors | `tests/test_svc_jwt.py`, `tests/test_exec_token.py` (or a small `test_jwt.py`) | tests only: RFC 8037 §A.3 thumbprint; two `sign()` → distinct `jti`, both verify; token of 8192 chars passes the length gate / 8193 → `malformed`; `exp − iat = 1` accepted; `sign(ttl_s=1)`; `iss` list → `iss_unregistered` |
| 3 | `ExecutorJwks` back-off (MCC-09) | `exec_token.py`, `tests/test_exec_token.py` | one field: `_refetch_at` = `t + max_age` on success, `t + 5 s` on failure (keys stay empty, fail closed). Test counts `fetch()` calls during an outage |
| 4 | AS-06 `public_keys` (1–2, `kid` of any key) | `svc_jwt.py` (`Service.public_keys`, `key_for` picks the key whose thumbprint = `kid`), tests, README | minimal code; `sign()` already takes the private key and derives `kid` from it, so "the newest signs" is the caller's key choice |
| 5 | Loader passes `bundle-signature.json` and validates by `grants-bundle.schema.json` | `grants.py`, `src/gxg_connector_kit/schemas/*.json` (copies), `tests/test_grants.py`, kit `pyproject.toml` (runtime dep `jsonschema`) | installed dependency `jsonschema` (+ `referencing`) instead of more hand-written structure checks. Schema copies ship in the wheel; a test fails if a copy differs from `schemas/` |
| 6 | GR-13a: max `built_at` on disk | `grants.py` (`BundleStore(state_file=…)`, read at start, write after a load via `os.replace`), tests | stdlib (`json`, `os.replace`). `state_file` is required: a store without it could be rolled back by a restart. Unreadable state file → error at start (fail closed) |
| 7 | New `canonical-hash.json` (9 vectors, `input_json`) | `tests/test_canonical.py` | tests only: the implementation already sorts by UTF-16 and rejects `1.0` from `json.loads` (float) |
| 8 | `rate_limited` requires `retry_after_s ≥ 1` | `errors.py`, `tests/test_errors.py` | one check in `__init__`, like `outcome_unknown` |
| 9 | UUID IDs where schemas require them | `exec_token.py` (claims validated by `execution-token.schema.json` with `FormatChecker`: `sub`, `jti` = UUID), test fixtures | same `jsonschema` as #5 instead of a hand-written regex; covers every other claim bound of the schema too |
| 10 | DoD-6 line for `check_coverage.py` | `reports/plans/M1-cycle-1.md` (AC-M1-09 row) | text fix |

## Ready solutions considered (DoD-6), every self-written component of the kit

| Component | Ready solutions considered | Decision |
| --- | --- | --- |
| mutation testing | **mutmut** (DoD-4), cosmic-ray, mutatest | **mutmut used**; `scripts/ci/mutate_kit.py` deleted |
| `canonical` (EFF-05) | `rfc8785` (PyPI), `canonicaljson` (Matrix, sorts by code point, not JCS), stdlib `json` | own ~25 lines on stdlib `json`/`unicodedata`/`hashlib`: NFC, no floats and the safe-int range are ours anyway; `rfc8785` would only replace `sort_keys` |
| `_jwt` (JWS core) | PyJWT options (`audience`, `issuer`, `leeway`, `require`), joserfc, authlib, jwcrypto | PyJWT used for signature and `alg`. Own claim rules: PyJWT accepts a list `aud`, has no `typ` check, no `exp − iat ≤ 60`, no `jti` cache, no reason codes |
| `thumbprint` (RFC 7638) | jwcrypto `JWK.thumbprint()`, joserfc (not installed) | 3 lines; a new dependency is not worth it; now pinned by the RFC 8037 §A.3 vector |
| `b64url_decode` | `base64.urlsafe_b64decode` | used underneath; the wrapper adds the strict alphabet check the stdlib skips |
| `JtiCache` | `cachetools.TTLCache` (not installed) | per-entry expiry (`exp + 5 s`) is not a global TTL; 12 lines |
| `ExecutorJwks` | PyJWT `PyJWKClient` | refetches on an unknown `kid` (an unauthenticated caller could force fetches) and has no fail-closed rule after a failed refresh (AS-09); transport is injected |
| exec-token claim bounds, UUIDs | **jsonschema** + `execution-token.schema.json` | **used** (this cycle) instead of hand-written regexes |
| bundle loader structure | **jsonschema** + `grants-bundle.schema.json`; `cryptography` Ed25519 verify | **both used**; own code only maps the valid document to dataclasses |
| schema files at runtime | symlink `src/…/schemas → schemas/` (tried: `uv build` fails, "Is a directory"), path walk to the repo (no repo in a wheel), copies | copies of the 6 schemas the kit reads + a test that each equals `schemas/` byte for byte |
| GR-13a state | stdlib `json` + `os.replace`; sqlite | one number in a small JSON file; atomic replace is enough |
| `redact` | structlog processors (still need our key list) | own 10 lines |
| `identity` test issuer | PyJWT | PyJWT used through `_jwt` |
| `scripts/ci/check_coverage.py` | `coverage report --fail-under`, `pytest --cov-fail-under` | **own script, justified**: both thresholds count lines and branches together over the total; AC-M1-09 needs branch coverage per module. The cycle-1 plan row said "platform feature", which was wrong; corrected there |

## Not done, on purpose

- mutmut on `identity`, `redact`, `errors`: not in the DoD-4 module list of the critic; the run config lists exactly `exec_token`, `svc_jwt`, `_jwt`, `grants`, `canonical`.
- mutmut is not a CI job: ~1000 mutants, minutes of runtime; the report records the run, the config makes it one command.
- No "which key is newest" logic in the kit: the verifier accepts any active key (AS-06); the signer uses the key it holds.
- Not changed: specs, schemas in `schemas/`, acceptance criteria, plan.

## Disagreements with the critic

None.
