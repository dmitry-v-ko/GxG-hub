# M0 cycle 3 — plan (ponytail full)

Input: reports/critic/M0-cycle-2.json (8.0), reports/critic/M0-cycle-2.review.md. I agree with all findings.

## Scope (every place the change reaches)

| Part | Files | Rung |
| --- | --- | --- |
| AC-M0-04: exempt the one quote, not the whole plan file | scripts/ci/check_secrets.py (`CONTENT_ALLOWLIST`, `content_rule`, docstring) | one line: `bytes.replace(quote, b"", 1)` before the scan; the quote (backtick + header + backtick) is built from parts in code |
| Tests for the narrowed exception | tests/ci/test_check_secrets.py | plan with the quote passes; quote + second header fails; header + key body fails; existing allowlist tests adjusted (a bare header in the plan is now reported) |
| AC-M0-03: self-authored PR gets no review request | docs/setup/branch-protection.md (decision paragraph, rule list, ruleset step, probe step 5), .github/CODEOWNERS header comment | text: state that GitHub does not request review from the PR author; step 5 → observable check (no CODEOWNERS errors shown; files in `schemas/` marked as owned in Files changed) |
| AC wording issue | reports/conformance/M0.json `escalation` | text; the AC itself is not changed — orchestrator agrees the wording with the owner |
| Report | reports/conformance/M0.json | new commit, CI run ID, new tests, reason text fixed, gzip limit in `known_limitations` |

## Not done, on purpose

- No decompression of gzip/zip in the scanner: AC-M0-04 does not ask for it; recorded as a known limitation.
- AC-M0-03 text, specs and the plan are not changed.
- No generic "allowed fragments" mechanism: one path, one fragment.
