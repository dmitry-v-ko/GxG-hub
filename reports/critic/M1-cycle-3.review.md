# M1 cycle 3 — ponytail-review

Diff: `cebca50..a0c0b4a` (branch `claude/m1-connector-kit`). The executor's own changes are `68762dd..a0c0b4a`; `4a5dd9f` is the merge of `claude/integration` (stage A audit specs and schemas), which is not executor code and is only checked for alignment. Report commits `0eb244b` and `cbfaa2f` (CI fields) are treated as claims to check.
Assumed load: a few connectors and services, one process each, low call rate. Execution tokens are rare (one per approved effect). Bundles change every few hours (GR-13a: re-signed at least every 12 h).

**What this change does:** It replaces the self-written mutation script with mutmut (config in the kit `pyproject.toml`) and adds tests that kill all 20 non-equivalent survivors of cycle 2. It brings the kit to the stage A specs: `public_keys` (1–2 keys per service), bundle and execution-token claims validated by the JSON Schemas through `jsonschema`, the GR-13a maximum `built_at` kept on disk, `rate_limited` requires `retry_after_s ≥ 1`, and the new 9-vector `canonical-hash.json`. `ExecutorJwks` now waits 5 s after a failed fetch instead of refetching on every call.

## How I checked

- Clean clone at `a0c0b4a`, empty `UV_CACHE_DIR`, `UV_PYTHON=3.12` (CPython 3.12.3), `UV_LOCKED=1`: `uv lock --check`, `uv sync`, `uv run pytest` = **430 passed, 148 subtests, 0 skipped/xfail**. Kit alone: 326 passed. `ruff check`, `ruff format --check`, `mypy --strict` (CI set): clean. `schema_lock.py`: up to date. `check_coverage.py`: 100 % branches in `exec_token` 14/14, `svc_jwt` 12/12, `grants` 50/50, `canonical` 20/20, `identity` 8/8, `_jwt` 38/38.
- CI: run 44814878816 on `a0c0b4a` was **cancelled** (superseded by the next push, `cancel-in-progress`). Run 44815009584 on `0eb244b` — every job, `ci-ok` included, **success**. `0eb244b` changes only `reports/conformance/M1.json`, so the tested code tree is the same as `a0c0b4a`. Run 44815291834 on `cbfaa2f` (reports only): success.
- **mutmut 3.8.1**, fresh clone, empty cache, Python 3.12.3, from `packages/gxg-connector-kit`: **955 mutants, 945 killed, 0 timeout, 10 survived**, 1 m 22 s. Same numbers and the same 10 names as the report. Each survivor is checked below.
- First attempt, exactly as the README says (`cd packages/gxg-connector-kit && uv run mutmut run` in a fresh clone): **`error: Failed to spawn: mutmut`**. It works only after `uv sync` at the repository root (finding 1).
- Alignment probes: `bundle-signature.json` (bytes reproduced, signature verifies, store ready; one changed byte → `does not verify`); `public_keys` with 2 keys (kid of either accepted, retired key → `kid`); GR-13a restart (older bundle → `rollback`, same bundle loads again; rejected or future bundles do not move the state); `canonical-hash.json` 9 vectors incl. `utf16_key_order` and 3 invalid inputs incl. `float_one_point_zero` read from `input_json`; `rate_limited` without `retry_after_s` or with 0 → `ValueError`, and the schema agrees; exec token `sub`/`jti` not UUID → `claims`.

## Cycle-2 required improvements

| # | Item | Status |
| --- | --- | --- |
| 1 | mutmut instead of `mutate_kit.py`, `[tool.mutmut]`, `REPO` via `uv.lock`, one command in a clean clone, `mutmut results` attached | Done, except "one command in a clean clone": needs a root `uv sync` first (finding 1). Script deleted, results attached with a reason per survivor |
| 2 | Kill the 20 non-equivalent survivors | Done. None of them survives in my run |
| 3 | "Ready solutions considered" per component (DoD-6), mutmut, corrected `check_coverage.py` line | Done. The `check_coverage.py` reason is incomplete (finding 2) |
| 4 | `ExecutorJwks` back-off, fetch-counting test | Done (`test_outage_fetches_once_per_back_off`) |
| 5 | Stage A specs: `public_keys` + vector test, GR-13a on disk, `canonical-hash.json`, `rate_limited` | Done |

## mutmut survivors: are they really equivalent?

| Mutant | Change | Verdict |
| --- | --- | --- |
| `_jwt.x_verify__mutmut_41` | `not isinstance(kid, str) or not kid` → `and` | Equivalent in outcome. A non-string `kid` never gets here: PyJWT `_validate_kid` raises `InvalidTokenError` → `malformed`. An empty `kid` passes to `key_for` and is still rejected (no JWKS `kid` and no thumbprint is empty). One visible difference: a service JWT with an empty `kid` **and** an unknown `iss` gets reason `iss_unregistered` instead of `kid`. Both are rejections; no spec fixes the order of reasons. Accepted |
| `_jwt.x_verify__mutmut_70/71/72` | PyJWT option `verify_nbf: False` removed or set to `True` | Equivalent for spec tokens. PyJWT then checks `nbf` only if it is present. Execution tokens cannot carry `nbf` (schema `additionalProperties: false` → `claims`); AS-06 service JWTs have no `nbf` |
| `_jwt.x_verify__mutmut_76/77/78` | PyJWT option `verify_iss: False` removed or set to `True` | Equivalent. PyJWT `_validate_iss` returns at once when `issuer is None`, and the kit never passes `issuer` (checked in PyJWT 2.15.1 source) |
| `exec_token.ExecutorJwks.__init__ mutmut_4` | `_keys = None` | Equivalent. `_refetch_at = -inf` forces `_current()` to fetch on first use, and both branches assign `_keys` before it is read |
| `exec_token.ExecutorJwks.__init__ mutmut_8` | `float("-INF")` | Equivalent: same value |
| `exec_token.ExecTokenVerifier.verify mutmut_27` | `cast(None, claims)` | Equivalent: `typing.cast` does nothing at runtime |

No non-equivalent survivor. DoD-4 holds.

## Findings

### Should fix

1. **The documented mutmut command fails in a clean clone** (`packages/gxg-connector-kit/README.md:36`, `packages/gxg-connector-kit/pyproject.toml:19`, `reports/conformance/M1.json` DoD-4 note)
   - **What this is:** DoD-4 and cycle-2 item 1 ask that mutmut runs with one command in a clean clone. The README says: from the kit folder, `uv run mutmut run`.
   - **Problem:** mutmut is a dev dependency of the **root** project. `uv run` inside a workspace member syncs only that member's dependencies, so in a fresh clone the command stops with `Failed to spawn: mutmut`. It works only after `uv sync` at the root. The report says "Checked in a clean clone with an empty UV_CACHE_DIR: one command, same result", which is not true as written.
   - **Fix:** Write the real command in the README, `[tool.mutmut]` comment and report: `uv sync && cd packages/gxg-connector-kit && uv run mutmut run` (or run from the root with `uv run --directory packages/gxg-connector-kit mutmut run` after `uv sync`). Text only.
   - **If we skip it:** The next person, or a CI job, copies the command from the README and it fails at once.

2. **`check_coverage.py` has a ready replacement** (`scripts/ci/check_coverage.py`, `tests/ci/test_check_coverage.py`, `reports/plans/M1-cycle-3.md:38`, `reports/plans/M1-cycle-1.md` AC-M1-09 row)
   - **What this is:** A 36-line script plus 39 lines of tests that checks branch coverage ≥ 90 % for each security module (AC-M1-09).
   - **Problem:** The plan says no tool can do this, because `--cov-fail-under` counts lines and branches together over the total. That is true for coverage and pytest-cov, but **coverage-threshold** (PyPI, 0.6.2) reads the same `coverage.json` and has `file_branch_coverage_min` with per-path settings under `[tool.coverage-threshold.modules."…"]`. The plan does not mention it. The owner's M0.1 decision is to replace self-written tools with ready ones.
   - **Fix:** Add `coverage-threshold` as a dev dependency with `[tool.coverage-threshold.modules."packages/gxg-connector-kit/src/gxg_connector_kit/<module>.py"] file_branch_coverage_min = 90` for the six modules, call it in `ci.yml` and `check-all.sh`, delete both files. Or keep the script and write in the plan why coverage-threshold does not fit (it is a small project). A test that the gate fails below 90 % stays.
   - **If we skip it:** 75 lines of our own code to keep, for a job a small config does.

3. **The GR-13a state file is written without `fsync`** (`packages/gxg-connector-kit/src/gxg_connector_kit/grants.py:236-239`)
   - **What this is:** After a newer bundle loads, the store writes `{"max_built_at": t}` to `<state>.tmp` and renames it over the state file (`os.replace`).
   - **Problem:** The data is not flushed to disk before the rename. If the host loses power right after a load, the renamed file can be empty. At the next start `json.loads("")` fails, and by design the service does not start (`test_unreadable_state_file_stops_the_start` with `""`). The only way out is to delete the file by hand, and that also removes the rollback guard until the next load.
   - **Fix:** Open the tmp file, write, `f.flush()`, `os.fsync(f.fileno())`, then `os.replace`. Three lines, stdlib.
   - **If we skip it:** A rare power cut turns into a connector that does not start until someone deletes the file.

### Nice to have

4. **A disk error in `load()` is not a `BundleError`** (`grants.py:236-239`)
   - **What this is:** `BundleStore.load` documents `BundleError` for every failed load; its message goes to `readiness()`.
   - **Problem:** If the state file cannot be written (disk full, read-only volume), `write_text` raises `OSError`. A reload loop that catches `BundleError` (the pattern the docstring invites) stops, and `readiness()` still shows the previous reason or "not loaded", not "cannot write state". It stays fail closed: the new bundle is not taken.
   - **Fix:** Catch `OSError` around the write, set `_last_error = "bundle: cannot record built_at (GR-13a)"`, raise `BundleError`. A test with a state path inside a missing folder.
   - **If we skip it:** The operator sees a stuck bundle with a wrong reason.

Notes, not findings:
- `jsonschema` checks `format: date-time` (`expires_at`) only when `rfc3339-validator` is installed. It is in the dev environment but not a kit dependency, so the test environment checks more than a production install. The report records this, and `parse_tenant` rejects a time without zone either way (tested). Adding `rfc3339-validator` to the kit dependencies would make both the same (one line).
- `ExecutorJwks` uses the wall clock (`time.time`) for the cache age, as before this cycle. A big backward clock step would keep keys longer than 10 minutes. Hosts are NTP-synced (AS-11), so I found no realistic case at the expected load.

Verdict: Ship. Fix 1 (text) and 3 (three lines) when convenient; 2 and 4 go to the backlog.
Lean: -70 lines possible (finding 2).
Not checked: behaviour on a real filesystem after power loss (finding 3 is from the code, not reproduced).
