# M1 cycle 2 — ponytail-review

Diff: `9406f94..c64693a` (branch `claude/m1-connector-kit`). Report commit `5379d42` checked as a claim.
Specs and plan for the rules: `claude/admiring-planck-vzys0x` (DoD-4 = mutmut only, DoD-6 = ready solution first; both apply now by owner decision). Spec changes made after cycle 2 (AS-06 `public_keys`, GR-13a on-disk anti-rollback, new vectors) are listed separately as next-cycle work, not as executor faults.
Assumed load: a few connectors and services, low call rate. Execution tokens are rare (one per approved effect).

**What this change does:** It closes the three cycle-1 majors with tests (jti lifetime at T0+30 and exp+5, the exact 5 s AS-11 boundary) and one `if` in `BundleStore.load` that rejects `built_at > now + 5 s`. It also closes the minors (RID `fullmatch`, `GxgError` schema bounds, `ExecutorJwks.readiness()`, `format_public_key` moved to tests) and adds a 393-line stdlib mutation script `scripts/ci/mutate_kit.py`.

How I checked: clean clone at `c64693a`, empty `UV_CACHE_DIR`, Python 3.12.3, `uv sync` → `uv run pytest` = 379 passed, 136 subtests, 0 skipped; kit alone 276 passed; branch coverage 100 % in all kit modules; `ruff check`, `ruff format --check`, `mypy --strict` clean. GitHub Actions run 44804004035 on `c64693a` (and 44804331032 on `5379d42`): `ci` = success, so `ci-ok` is green. **mutmut 3.8.1** on `exec_token`, `svc_jwt`, `_jwt`, `grants`, `canonical`: 943 mutants, 898 killed, **45 survived** (list and classification at the end). Probes: RFC 8037 thumbprint vector, `svc_jwt.sign` jti uniqueness, JWKS outage with 10 concurrent callers, orchestrator vectors `canonical-hash.json` and `bundle-signature.json`.

Cycle-1 items: jti TTL — done (mutmut kills every mutant of `exp + SKEW_S` and of the prune condition). AS-11 boundary — done (no `SKEW_S` mutant survives). Future `built_at` — done (no mutant of the new check survives; the next correct bundle still loads).

## Must fix

1. **The mutation proof is a self-written script, and real mutmut finds survivors it missed** (`scripts/ci/mutate_kit.py`, `reports/conformance/M1.json` DoD-4, `reports/plans/M1-cycle-2.md` row 4)
   - **What this is:** DoD-4 now says: mutmut on the modules with key conditions, no surviving non-equivalent mutants, report attached, "самописные скрипты мутаций не используются". The plan row says "stdlib only" and does not mention mutmut at all (DoD-6: no ready solution considered).
   - **Problem:** The script checks 81 hand-picked snippets and reports "0 non-equivalent survivors". mutmut on the same five modules makes 943 mutants; 45 survive, of which 20 are not equivalent (items 2–4 below and the list at the end). The repo has no `[tool.mutmut]` config, so `mutmut run` fails at once ("Could not figure out where the code to mutate is"). Also `tests/kit_testing.py:16` finds the repo as `Path(__file__).parents[3]`: from mutmut's `mutants/` copy that points to the wrong folder, so I had to add scratch-only symlinks `packages/policies`, `packages/schemas` to run it.
   - **Fix:** Delete `scripts/ci/mutate_kit.py`. Add `[tool.mutmut]` to `packages/gxg-connector-kit/pyproject.toml` (config that worked for me is at the end), make `kit_testing.REPO` find the root by walking up to `uv.lock`, run `mutmut run` from the package dir, attach `mutmut results` to the report, kill or justify each survivor.
   - **If we skip it:** DoD-4 stays failed (cap 6), and the report claims a proof that a standard tool does not confirm.

## Should fix

2. **The AS-06a `kid` thumbprint has no test against a known value** (`src/gxg_connector_kit/_jwt.py:L85-88`; tests only call `_jwt.thumbprint` on both sides)
   - **What this is:** AS-06a: service JWT `kid` = RFC 7638 thumbprint of the registry key; the receiver recomputes and compares.
   - **Problem:** Sign and verify use the same function, so any wrong thumbprint still matches itself. 15 mutmut mutants survive: `separators=None` (JSON with spaces), member names `CRV`/`KTY`/`X`, values `ed25519`/`okp`. Each one gives a non-RFC `kid`; a caller built with any other library (TypeScript MCP SDK side, jwcrypto, joserfc) would then be rejected with `kid`. The code today is right: RFC 8037 §A.3 key `11qYAYKx…URo` → `kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k` (my probe). The cycle-1 report quoted that probe as if it were a test; no such test exists.
   - **Fix:** One test: RFC 8037 §A.3 public key → expected thumbprint. 4 lines; kills all 15.
   - **If we skip it:** A refactor of `thumbprint` silently breaks service-to-service auth with every non-kit caller.

3. **`svc_jwt.sign()` could give every token the same `jti` and no test notices** (`src/gxg_connector_kit/svc_jwt.py:L63`)
   - **What this is:** Each service JWT needs a unique `jti`; receivers reject a seen `jti` as replay.
   - **Problem:** Mutant `"jti": str(None)` survives. With it, the second call from `demo` to Approval Service within ~65 s is rejected as `replay`, so every service would do one call per minute. The code today uses `uuid4` and is right (probe: two `sign()` calls → different `jti`).
   - **Fix:** In `test_sign_and_verify`, sign twice and verify both (or assert different `jti`). 2 lines.
   - **If we skip it:** A change to `sign()` can break all internal calls after the first one.

4. **Small boundaries without tests** (`_jwt.py:L113`, `_jwt.py:L172`, `svc_jwt.py:L60`, `svc_jwt.py:L84`)
   - **What this is:** Input limits of the JWT core.
   - **Problem:** Surviving non-equivalent mutants: token length `>` → `>=` (a token of exactly 8192 chars flips), `0 < exp - iat` → `1 < exp - iat` (a valid 1 s token is rejected), `sign(ttl_s=1)` → `ValueError`, and `isinstance(iss, str) or True` (a token with `"iss": ["demo"]` then raises `TypeError` out of `verify` instead of `TokenError` → 500 instead of 401). All fail closed today; they are test gaps that DoD-4 counts.
   - **Fix:** Four parametrized cases: length 8192 accepted / 8193 malformed, `exp - iat = 1` accepted, `sign(ttl_s=1)` works, `iss` as list → `iss_unregistered`.
   - **If we skip it:** DoD-4 stays red on these lines.

5. **During a JWKS outage every call and every `/readyz` refetches, one after another** (`src/gxg_connector_kit/exec_token.py:L74-84`, `L92-96`)
   - **What this is:** `_current()` refetches when the cache is stale; after a failed fetch it sets `_fetched_at = None`.
   - **Problem:** So after one failure the cache is "stale" forever: every `key()` and every `readiness()` does a blocking fetch inside the lock. Probe: fetch fails after 0.2 s, 10 concurrent `readiness()` calls → 10 fetches, 2.0 s total. With a real HTTP timeout of 5 s, 10 waiting requests take 50 s each to get 401. The cycle-2 plan promised "pure read, no fetch from `/readyz`"; the code does fetch. Still fail closed, so not a security bug.
   - **Fix:** On failure keep `_fetched_at = t` (retry after a short back-off, e.g. 5 s) while `_keys = {}`. 1 line plus a test that counts fetches.
   - **If we skip it:** An executor outage turns into slow requests and slow probes on every connector.

6. **Plan does not record ready solutions for cycle-2 code** (`reports/plans/M1-cycle-2.md`)
   - **What this is:** DoD-6 (applies now): for each self-written component, the plan names the ready solutions considered and why not.
   - **Problem:** The only new component, the mutation script, says "stdlib only" without naming mutmut, cosmic-ray or mutatest. The cycle-1 plan covers the other components (canonical → `rfc8785` rejected, JWT → PyJWT), so that part is fine; see `existing_solutions` in the JSON.
   - **Fix:** Add a "ready solutions" column to the cycle plan; for this cycle it reduces to "mutmut, used".

## Nice to have

7. **`check_coverage.py` is custom** (`scripts/ci/check_coverage.py`, 36 lines, cycle 1). `coverage report --fail-under` counts lines and branches together and has no per-file branch-only threshold, so the script is justified for AC-M1-09 "покрытие ветвей". The cycle-1 plan row says "platform feature: `--cov-fail-under`", which is not what was built; update the plan line.

## Next cycle: alignment with the orchestrator specs (changed after cycle 2)

- `bundle-signature.json`: `canonical.dumps(bundle)` reproduces `bundle_bytes_b64` byte for byte, but `load_bundle` fails with `bundle: malformed (KeyError)`: the registry now has `public_keys` (1–2 keys, AS-06) instead of `public_key`. Needed: `Service.public_keys`, `kid` = thumbprint of any of them, `sign` with the newest; a test that loads this vector.
- `canonical-hash.json` (9 vectors, 3 invalid, UTF-16 key order): the implementation passes all of them (probe). The tests do not: `test_vector_file_has_the_expected_cases` (pinned to 6) and `test_invalid_inputs[float_one_point_zero]` (does not read `input_json`) fail with the new file.
- GR-13a: the max loaded `built_at` must be stored on local disk and survive restart; today it is only in memory.
- `error.schema.json`: `rate_limited` now requires `retry_after_s`; `test_schema_bounds_are_accepted` fails with the new schema (`GxgError("rate_limited", "x", retry_after_s=0, …)` passes, the bare case does not). Require `retry_after_s` for `rate_limited` in `GxgError.__init__`.
- `grants-bundle.schema.json` / `grants-bundle-sig.schema.json` exist now: a contract test that kit's loader accepts exactly what the schema accepts (at least the vector).

Verdict: fix 1–4 first (DoD-4), then 5–6.
Lean: -380 lines possible (delete `scripts/ci/mutate_kit.py`, ~13 lines of `[tool.mutmut]` config instead).
Not checked: mutmut on `identity` and `redact` (not in the requested module list).

## mutmut: setup used and survivors

Config (in `packages/gxg-connector-kit/pyproject.toml`, scratch only; run from that dir with symlinks `packages/policies → ../policies`, `packages/schemas → ../schemas` because of `kit_testing.REPO`):

```toml
[tool.mutmut]
source_paths = ["src"]
only_mutate = ["src/gxg_connector_kit/exec_token.py", "src/gxg_connector_kit/svc_jwt.py",
               "src/gxg_connector_kit/_jwt.py", "src/gxg_connector_kit/grants.py",
               "src/gxg_connector_kit/canonical.py"]
pytest_add_cli_args_test_selection = ["tests"]
pytest_add_cli_args = ["-p", "no:cacheprovider", "-q"]
```

Result: 943 mutants, 898 killed, 0 timeout, 45 survived. `canonical`: 0 survivors.

| Mutant(s) | Change | Verdict |
| --- | --- | --- |
| `_jwt.x_thumbprint__mutmut_3,5–16,19,20` (15) | separators / member names / values of the RFC 7638 JSON | **not equivalent** — finding 2 |
| `_jwt.x_verify__mutmut_3` | `len > 8192` → `>=` | **not equivalent** — finding 4 |
| `_jwt.x__check_claims__mutmut_39` | `0 < exp - iat` → `1 <` | **not equivalent** — finding 4 |
| `svc_jwt.x_sign__mutmut_3` | `0 < ttl_s` → `1 <` | **not equivalent** — finding 4 |
| `svc_jwt.x_sign__mutmut_23` | `jti = str(None)` | **not equivalent** — finding 3 |
| `svc_jwt.xǁSvcJwtVerifierǁverify__mutmut_8` | `isinstance(iss, str) or True` | **not equivalent** (list `iss` → `TypeError`) — finding 4 |
| `_jwt.x_verify__mutmut_41` | kid `or` → `and` | equivalent: a non-string or empty `kid` matches no key → same `kid` reason |
| `_jwt.x_verify__mutmut_70–72, 76–78, 82–84` (9) | PyJWT `verify_nbf/iss/jti` key renamed or `True` | equivalent: our tokens carry no `nbf` (stricter if present), `issuer=None` skips the iss check, `jti` is required as str anyway |
| `grants.xǁBundleStoreǁreadiness__mutmut_3,13`, `exec_token.xǁExecutorJwksǁreadiness__mutmut_2` | `Readiness(None, …)` | equivalent while callers test truthiness (`all_ready`, tests use `not r.ready`) |
| `grants.…readiness__mutmut_9,10`, `exec_token.…readiness__mutmut_7,9`, `exec_token.x_parse_jwks__mutmut_33–36`, `svc_jwt.x_sign__mutmut_7` | message text | equivalent (log text, not contract) |
| `exec_token.x_parse_jwks__mutmut_26` | `or` → `and` in JWK check | equivalent: wrong length still fails in `from_public_bytes`; non-str `kid` can never match a header `kid` (must be str) |
| `exec_token.xǁExecutorJwksǁ__init____mutmut_4` | `_keys = None` | equivalent: never read before the first `_current()` assigns it |
| `exec_token.xǁExecTokenVerifierǁverify__mutmut_19` | `cast(None, …)` | equivalent at runtime |
