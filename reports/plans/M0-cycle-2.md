# M0 cycle 2 — plan (ponytail full, written retrospectively)

Input: reports/critic/M0-cycle-1.json (5.0). The code was done before ponytail arrived; this plan records what was done and why at the lowest rung.

## Scope (every place the change reaches)

| Part | Files | Rung |
| --- | --- | --- |
| AC-M0-04: any PEM private key header, case-insensitive names | scripts/ci/check_secrets.py, tests/ci/test_check_secrets.py | minimal code: one regex (`re`), `fnmatch` on `name.lower()`, path allowlist dict |
| Pushed history quotes the header (cycle-1 reports) | check_secrets.py `HISTORICAL_BLOB_ALLOWLIST` | minimal code: (path, git blob sha) set; `hashlib` |
| DoD-5: collection skips, xfail/XPASS | conftest.py, tests/ci/test_pytest_policy.py | pytest hooks already used: add `pytest_collectreport`, check `wasxfail` |
| AC-M0-02: lock-independent guard | tests/contract/test_security_required_fields.py, scripts/ci/prove_schema_guard.py | installed dependency (jsonschema); existing mutation harness + `--write` wrapper |
| HINT, CODEOWNERS | tests/contract/test_schema_lock.py, scripts/ci/schema_lock.py, .github/CODEOWNERS | one string each; platform feature |
| AC-M0-03 doc | docs/setup/branch-protection.md | text; main variant = owner decision 2026-11-20 (ci-ok + CODEOWNERS, no required approval) |
| CI minors | .github/workflows/ci.yml, scripts/ci/check-all.sh | shell `git cat-file -e` fallback; expression for cancel-in-progress; timeout |
| demo lock | connectors/demo/pyproject.toml, uv.lock | dependency-group dev (pytest) |
| Report | reports/conformance/M0.json | — |

## Not done, on purpose

- No general token scanner (gitleaks etc.): AC-M0-04 names only file patterns and the PEM header.
- No pre-commit hook: CI is the enforcement point.
- No `--write` lock for the semantic test list: the list is hand-pinned to requirement IDs by design.
- Not changed: specs, acceptance criteria, history.

## Beyond the letter (kept, cheap)

- UTF-16/binary decoding in the scanner (a few lines): a header in a UTF-16 file would otherwise pass.
