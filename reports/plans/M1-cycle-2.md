# M1 cycle 2 — plan (ponytail full)

Input: critic report `reports/critic/M1-cycle-1.json` (score 6.0, caps `dod` + `major`), its review, AS-06a / GR-13a (brought in unchanged by the previous commit).
Base: `claude/m1-connector-kit` @ 9406f94. Everything stays in `packages/gxg-connector-kit` plus one script and the report.

## Scope (every place the change reaches)

| # | Item (critic) | Files | Rung |
| --- | --- | --- | --- |
| 1 | `jti` cache lifetime (major, EFF-18 п.1, AS-06) | `tests/test_exec_token.py`, `tests/test_svc_jwt.py` | code is already right (`exp + SKEW_S`, prune `t < now`); only tests: accept at T0, replay at T0 + 30 and at exp + 5 → `replay`. exp + 5 is still inside the AS-11 window, so the token passes every other check and only the cache can reject it |
| 2 | AS-11 boundary (major) | same two test files | tests only: `exp = now − 5` and `iat = now + 5` accepted |
| 3 | `built_at` in the future (major, GR-13/13a) | `grants.py` (`BundleStore.load`), `tests/test_grants.py` | one `if` in `BundleStore.load` (it owns the clock), before the rollback check: `built_at > now + 5 s` → `BundleError`, bundle not stored. `load_bundle` stays clock-free (pure parse + signature) |
| 4 | Reproducible mutation sweep (DoD-4) | `scripts/ci/mutate_kit.py` (new), `reports/conformance/M1.json` | stdlib only: list of (name, file, old, new); apply one, run kit tests, restore (`try/finally`), print JSON. Each `old` must occur exactly once, else the script fails (no silently stale list). Not in CI: ~60 pytest runs; run manually per cycle |
| 5 | RID `$` → `fullmatch` (minor, GR-04) | `grants.py`, `tests/test_grants.py` | one line per regex: drop `^…$`, call `fullmatch` |
| 6 | `to_dict()` vs `error.schema.json` (minor, MCC-05) | `errors.py`, `tests/test_errors.py` | two `ValueError` checks in `__init__`, like the existing `outcome_unknown` one: `retry_after_s` int in [0, 86400], `details` ≤ 20 keys. Also `request_id` length 1..256 (same schema, same two lines) |
| 7 | JWKS readiness (minor, MCC-09) | `exec_token.py`, `tests/test_exec_token.py` | `ExecutorJwks.readiness()` → `Readiness(keys loaded and fetched < max_age ago)`; pure read, no fetch from `/readyz` |
| 8 | Test-only code in src (Lean) | `_jwt.py` → `tests/kit_testing.py` | move `format_public_key` (−3 lines in src) |

## Not done, on purpose

- `Service.kind` / `internal_url` stay: `kind` is asserted in tests and both are registry fields M7 reads (AS-25 / caller lookup); removing them now means re-adding them in M7.
- No change to the `jti` cache code: it is correct; the gap was only tests.
- Mutation script is not a CI job: runtime and noise; the report records the run and the script makes it repeatable.
- Not changed: specs, acceptance criteria, plan.

## Disagreements with the critic

None.
