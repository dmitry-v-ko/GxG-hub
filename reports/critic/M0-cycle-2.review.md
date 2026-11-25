# M0 cycle 2 — ponytail-review

Diff: `5265b6f..77662af` (all of M0 from the orchestrator branch). Reviewer: independent critic, cycle 2.
Expected load: one maintainer plus agent executors pushing PRs; CI runs per push/PR. Scale is not a concern at this size.

What this change does: it sets up the uv workspace with four package skeletons, ruff and mypy, and a GitHub Actions
pipeline whose single gate is `ci-ok`. CI checks lint, types, contract tests over the JSON Schemas (with a schema lock,
lock-independent tests for security-critical fields and a mutation proof), a repository-wide pytest policy (no network,
no skip/xfail) and a secret scanner over the tree and every pushed commit. Branch protection is a manual step described
in `docs/setup/branch-protection.md`.

What I checked (clean clone, empty `UV_CACHE_DIR`, Python 3.12.3):
- `uv sync --locked && uv run pytest` → 94 passed, 136 subtests passed, 0 skipped; `scripts/ci/check-all.sh` → `check-all: OK`;
  `prove_schema_guard.py` → 59/59 mutations caught.
- CI on `77662af`: run 44785675116, conclusion `success` (all jobs incl. `ci-ok`).
- Mutations of key conditions, each turns `pytest tests` red: no `pytest_collectreport` hook, no `wasxfail` check,
  no runtime skip check, `sessionfinish` no-op, PEM regex anchored to line start, case-sensitive names, no UTF-16 decode,
  allowlist widened to a prefix, `idempotency_key` dropped from `required` (with and without `schema_lock.py --write`).
- Secret scanner, by hand (`--range` and `--tree`): caught — inline key after `K="`, RSA header in `.py`, `KEY.PEM`,
  `.ENV`, `Server.Key`, UTF-16 header, key added and deleted inside the range, a pinned old blob copied to another path.
  Not caught — see finding 1.
- `HISTORICAL_BLOB_ALLOWLIST` is needed: without the `reports/critic/M0-cycle-1.json` entry,
  `--range origin/main..77662af` reports `3182606…:reports/critic/M0-cycle-1.json`. The entry is pinned to a Git blob
  SHA-1 of content that is already public and holds no key; forging another blob with the same SHA-1 is a second-preimage
  attack, which is not practical. Restoring that exact old file passes, which is harmless. This allowlist is narrow enough.

## Must fix

(none)

## Should fix

1. **A real private key passes CI if it is pasted into the plan** (`scripts/ci/check_secrets.py:55-57, 108-110`)
   - **What this is:** `CONTENT_ALLOWLIST` turns off the content check for the whole file `docs/implementation-plan-v1.md`,
     because the plan quotes the PEM header in AC-M0-04.
   - **Problem:** The exception covers any content of that file, not the one quote. I appended a PEM private key header,
     a base64 body line and the END line to the plan, committed it: `check_secrets.py --range` → exit 0, `--tree` → exit 0.
     The stated reason, "the plan must not be edited", is not true: the plan was edited in this very diff (`389ed23`, +37 lines),
     and it is the most edited normative document. No test covers "key material inside the allowlisted file".
   - **Fix:** Exempt the quote, not the file. For an allowlisted path, remove exactly the quoted fragment
     (backtick + header + backtick, built in code without spelling the header) once, then scan the rest as usual.
     About three lines. Add a test: the plan with the quote passes; the plan with the quote plus a second header (or a
     header with a body) fails. Correct the reason string and the sentence in `reports/conformance/M0.json`.
   - **If we skip it:** A key that lands in the plan (copy-paste from a terminal, an agent pasting a "sample") goes into
     history green. CODEOWNERS does not stop it: by the owner's decision it does not block the merge.

2. **Probe step 5 expects a review request that GitHub will not make** (`docs/setup/branch-protection.md`, "Проверка попыткой", step 5; `.github/CODEOWNERS`)
   - **What this is:** AC-M0-03 says changes to CODEOWNERS paths "automatically request the owner's review". The probe
     says: open a PR touching `schemas/`, and `dmitry-v-ko` appears in Reviewers.
   - **Problem:** The only code owner is `@dmitry-v-ko`, and PRs are opened under the same account (PR #1; this is the
     reason for the owner's decision). GitHub never requests a review from the PR author, so the reviewer list stays empty.
     Step 5, done literally, fails, and the CODEOWNERS half of AC-M0-03 cannot be observed in the chosen setup. The doc
     itself says the author's approval does not count, but it does not draw this conclusion. The executor narrowed nothing,
     but the contradiction between the AC text and the setup is not reported to the owner.
   - **Fix:** Say plainly in the doc that with a single owner who is also the PR author no review is requested. Replace
     step 5 with something observable: GitHub shows no errors for `.github/CODEOWNERS` (file view banner) and, in the PR's
     "Files changed", the files under `schemas/` show the code-owner shield "Owned by you". Raise the AC wording with the
     owner (escalation in the conformance report / to the orchestrator), not a silent rewrite.
   - **If we skip it:** The owner runs the probe, step 5 fails, and AC-M0-03 is either marked `failed` or `passed` against
     the doc's own text.

## Nice to have

3. **Compressed files are not looked into** (`scripts/ci/check_secrets.py:108-117`)
   - A gzip of a PEM key (`k.gz`) passes. AC-M0-04 does not ask for it and the plan scopes out a general scanner;
     note it in `known_limitations`, no code.

Verdict: fix 1 and 2 first.
Not checked: real branch-protection behaviour (needs the owner and a plan that enforces rules on a private repo); exact
GitHub error texts in the probe table.
