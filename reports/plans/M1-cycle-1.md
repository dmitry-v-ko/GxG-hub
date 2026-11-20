# M1 cycle 1 — plan (ponytail ultra)

Input: plan section M1, EFF-05, EFF-18 п.1, AS-06, AS-09…AS-11, AS-25, GR-01…GR-17 (+ GR-11a), SEC-03, SEC-18, MCC-05, ADR-0001.
Base: `claude/m0-foundation` @ 6b3fe48. Everything lives in `packages/gxg-connector-kit`.

## Scope (every place the change reaches)

| Part | Files | Rung |
| --- | --- | --- |
| `canonical` (EFF-05, AC-M1-01/02) | `canonical.py` | minimal code on stdlib: `unicodedata.normalize`, `json.dumps(ensure_ascii=False, separators)`, `hashlib`. JCS without floats = sorted keys (by UTF-16 code units) + stdlib JSON escaping; no `rfc8785` dependency for ~30 lines |
| `errors` (MCC-05, error.schema) | `errors.py` | minimal code: one exception class with `to_dict()`; code list = schema enum (contract test) |
| shared JWS core | `_jwt.py` | installed-to-be dependency: **PyJWT** (EdDSA, alg allowlist) on **cryptography** (Ed25519). We do only claim checks the libraries don't express: exact `aud`, `typ`, 5 s skew both ways (AS-11), `exp − iat ≤ 60`, required claims, `jti` cache |
| `exec_token` (EFF-18 п.1, AS-09…AS-11, AC-M1-03) | `exec_token.py` | minimal code: verifier + JWKS cache (≤ 10 min, only `kid` from JWKS; transport injected as `fetch()` — no HTTP client dependency) |
| `svc_jwt` (AS-06, AS-25, AC-M1-04) | `svc_jwt.py` | minimal code: verifier against the platform registry from the bundle + `sign()` for callers |
| `grants` (GR-01…GR-17, GR-11a, AC-M1-05…07) | `grants.py` | minimal code: `GrantResolver` (check / allowed / version), bundle loader with Ed25519 signature check, tenant slice, staleness |
| `identity` (ADR-0001, SEC-03) | `identity.py` | `IdentityVerifier` Protocol + `VerifiedIdentity` (ADR shape) + test issuer/verifier gated by `GXG_PROFILE=test` |
| `redact` (SEC-18, AC-M1-08) | `redact.py` | minimal code: recursive copy dropping sensitive keys, `re` for JWTs |
| `readiness` | `readiness.py` | one dataclass + `all_ready()` |
| Deps | `packages/gxg-connector-kit/pyproject.toml`, root `pyproject.toml` (dev: `hypothesis`, `pytest-cov`), `uv.lock`, `.gitignore` (`.hypothesis/`) | — |
| AC-M1-09 coverage gate | `.github/workflows/ci.yml`, `scripts/ci/check-all.sh` | platform feature: `pytest --cov --cov-branch --cov-fail-under=90` on the 5 modules |
| Tests | `packages/gxg-connector-kit/tests/*` | test keys generated at test time (never stored); vectors and example policies read from the repo |
| Docs | `packages/gxg-connector-kit/README.md` | short: modules, config, what is test-only |
| Report | `reports/conformance/M1.json` | — |

## Interpretations (spec is silent; recorded, not silently invented)

1. **Service JWT `kid`.** AS-06 and the registry have one key per service and no `kid`, but AC-M1-04 requires "unknown `kid`". Chosen: `kid` = RFC 7638 JWK thumbprint of the issuer's registry key. The verifier finds the service by `iss` (absent → reject, AS-25), then requires `kid` = that key's thumbprint (else reject), then checks the signature. Survives future key rotation without a format change. `svc_jwt.sign()` sets it, so M7 needs no own code. **Owner to confirm.**
2. **Service JWT "wrong `iss`" case.** There is no fixed issuer (unlike `gxg-executor`). Chosen: the receiving endpoint passes its allowlist of issuer IDs (AS-06 "сервис → endpoint"); `iss` outside it → reject. Separate case: `iss` not in the registry → reject.
3. **Staleness (GR-13 / AC-M1-07)** is measured by `built_at`. Consequence for M2/CI: the bundle must be rebuilt and re-signed at least daily, or every service goes not-ready after 24 h without merges.
4. **Groups in the bundle.** GR-11a says groups are expanded; AC-M1-05 runs on the unexpanded `example.yaml`. The resolver expands `group:<id>` itself from `groups` and treats an unknown group as no subject (deny). Works with both shapes.
5. **Failed reload with a valid bundle already loaded:** keep the last valid one (readiness follows it and its age). A bundle with `built_at` older than the loaded one is rejected (rollback of a revoked grant). First load failing → not ready.

## Not done, on purpose (ultra pushback)

- No HTTP client for JWKS: `fetch: Callable[[], bytes]` is injected; the real transport comes with M8.
- No execution-token **issuer**: the Executor is M8. Tests sign with the shared `_jwt.sign`.
- No EFF-18 п.2–5 (hash compare, config match, `dispatch`): M8. Only п.1 is in M1.
- No Approval Service client, no MCP tool-error wrapper, no logging.Filter: no caller before M3/M7.
- No JSON Schema validation of the bundle at runtime: M2 validates before signing; the loader parses structurally and fails closed on anything malformed. No `jsonschema` runtime dependency.
- No real `IdentityVerifier`: M4 spike decides the mechanism. There is no production factory, so outside the test profile a connector has no verifier (fail closed).
- Not changed: specs, acceptance criteria, plan.
