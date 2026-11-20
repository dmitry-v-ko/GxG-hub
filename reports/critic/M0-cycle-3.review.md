# M0 cycle 3 — ponytail-review

Diff: `5265b6f..2ab1faf` (whole milestone; cycle 3 itself is `a0e3777..2ab1faf`). Load assumed: CI of a repo with one maintainer, a few PRs a day.

What this change does: it sets up the uv workspace, package skeletons, ruff/mypy, the CI with schema guard and secret scan, CODEOWNERS and the branch protection guide. In cycle 3 the secret scanner stops exempting the whole plan file and removes only one quoted PEM header (backtick, header, backtick) before scanning it. The branch protection guide and CODEOWNERS now say that GitHub does not ask the PR author for review, and the CODEOWNERS check is something you can see on the page. The plan carries the AC-M0-03 wording approved by the owner.

Checked by hand (clean clone of 2ab1faf, empty uv cache, Python 3.12.3):

- `uv sync --locked && uv run pytest` -> 100 passed, 136 subtests passed; `scripts/ci/check-all.sh` -> OK; `prove_schema_guard.py` 59/59. CI run 44788073458 on 2ab1faf: all 6 jobs success, `ci-ok` success.
- Plan file (commit + `--tree` + `--range`): quote plus a key placed before AC-M0-04 -> red; quote changed to an RSA header -> red; quote without backticks -> red; the same quote in `README.md`, `docs/implementation-plan-v1.md.bak`, `Docs/implementation-plan-v1.md` -> red; CRLF line endings -> green (correct, nothing new).
- Mutations of the new rule: remove every quote instead of one -> 2 tests red; exempt the whole file again -> 6 red; exempt the quote in every file -> 4 red.
- AC-M0-02 by hand: drop `idempotency_key` from `required` + `schema_lock.py --write` -> 3 failed. AC-M0-04: `probe.pem`, `x/.env`, `KEY.PEM`, inline key after `K="`, header in a `.py` -> red. DoD-5: module-level `importorskip`, `skip` in a package test, passing `xfail(strict=False)` -> pytest exit 1.
- Plan text in 2ab1faf is identical to `claude/admiring-planck-vzys0x` (no diff).

## Nice to have

1. **The one free quote can sit anywhere in the plan, with a key body after it** (`scripts/ci/check_secrets.py:113-114`)
   - **What this is:** In the plan file the scanner removes the first quoted header, then looks for other headers.
   - **Problem:** The removal does not care where the quote is. If a PR moves or deletes the AC-M0-04 quote and adds a code block with the quoted header, a base64 key body and the END line, the scan is green (checked: `--tree` and `--range` exit 0). What is left is not a usable PEM file (`openssl pkey` refuses it, checked), and a header-less key body passes in any file anyway, so this opens no new kind of leak. It only means the rule is not tied to the AC row.
   - **Fix:** Remove the quote only from the line that starts with `| AC-M0-04 |`, or also report an END line of a private key anywhere. A few lines and one test.
   - **If we skip it:** A careless edit of the plan can carry a key body past CI, as it could in any other file.

Verdict: Ship.
Not checked: the exact tooltip text on the code owner shield and the CODEOWNERS error banner in the GitHub UI (needs the owner's browser); the guide could also name the API `GET /repos/{owner}/{repo}/codeowners/errors` as a check that gives the same answer in text.
