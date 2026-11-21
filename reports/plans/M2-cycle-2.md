# M2 cycle 2 — plan (ponytail full)

Input: critic `reports/critic/M2-cycle-1.json` (score 5.0) and `.review.md`; spec clarifications
GR-13a / AS-06a (commit 8b616c4, unchanged). AS-06a (service JWT `kid`) is outside M2: nothing to do here.

## Scope

| Item | Files | Rung |
| --- | --- | --- |
| F1 fail closed outside `pull_request` | `ci.yml` job `grants-widening`, `scripts/ci/widening_gate.py` | minimum code: on `push`/`workflow_dispatch`/`schedule` to `main` nothing to approve → pass; on any other ref diff `merge-base(origin/main, HEAD)` vs HEAD, exit 1 on any widening (no PR → no label → no approval). The step "Not a pull request" is deleted. Gate gets `--no-pr` mode (no API calls) |
| F2 approval bound to the head commit | `widening_gate.py`, `ci.yml` (`actions: read`, concurrency) | minimum code: on every run with widening and the label present, the label is stale if its `labeled` event is older than (a) the earliest `created_at` of `pull_request` workflow runs for the head SHA on this PR (server time) or (b) the latest `head_ref_force_pushed` event. Stale → label removed, red. No runs found → red (fail closed). PR runs get their own concurrency group (`run_id`) so nothing is cancelled or replaced while pending |
| F3 groups in approval rules | `tools/gxg-policy/src/gxg_policy/diff.py` | ~15 lines: `group:` in `match.requesters` and `approvers` expanded with the tenant's groups (base side with base groups, head with head). `auto_approve`: new requesters → `requesters_added` widening, removed → narrowing; other parts of `match` changed → `match_changed` widening. Other decisions: requesters stay part of `match` (any change → widening, fail closed). Approvers: expanded sets; `"*"` (any approver) handled on both sides |
| F4 mutation-killing tests | `tests/diff/07-*`, `08-*` (+ goldens), `tests/bad/19-dangling-group-subject`, `test_bundle.py`, `scripts/ci/mutate_m2.py`, `reports/mutations/M2-cycle-2.txt` | fixtures + one test; the mutation script is a list of (file, old, new) + `pytest -x` per mutation, stdlib only |
| F5 guide | `docs/setup/widening-approval.md` | text: every executed path, remaining limits, owner end-to-end checklist with exact expected results |
| Minor: `--built-at` | `cli.py` | one check: `YYYY-MM-DDTHH:MM:SSZ` parsed by `datetime.strptime`, else exit 2 |
| Minor: one MARKER | `widening_gate.py` | gate takes the marker from the first line of the Markdown file that `gxg-policy diff` wrote; constant stays only in `diff.py` |
| GR-13a schedule | `ci.yml` (`schedule` cron every 6 h, job `bundle` also on `schedule`), `docs/setup/bundle-signing.md`, test | platform feature (Actions cron, runs on the default branch). Test parses the cron and checks the max gap ≤ 12 h and that `bundle` runs on `schedule`; determinism except `built_at` already covered by AC-M2-05 test, plus a test that two builds with different `built_at` differ only there and `built_at` is RFC 3339 UTC |
| AC-M2-04 evidence | `reports/conformance/M2.json` | split «proven by tests» / «owner-pending» |
| Local CI mirror | `scripts/ci/check-all.sh` | unchanged unless a step is added |

## Decisions

- **F1: fail closed, not fewer triggers.** Dropping `push: claude/**` loses CI on agent branches without a PR.
  A red push-run on a widening PR head is the intended price; once the owner labels, the later PR run is
  green. Which of two same-name checks GitHub counts is owner-checklist step.
- **F2: server time, two sources.** Run `created_at` alone misses "force-push back to an older SHA that already
  had runs"; the `head_ref_force_pushed` event covers it. Runs linked to *other* PRs are ignored.
- **`"*"` in approvers.** `* → [x]` is narrowing (it was reported as widening in cycle 1). Fail-closed is kept
  where meaning is unclear (`match` of non-auto rules).

## Not done, on purpose

- Loader rejection of an older `built_at` (GR-13a second sentence) — M1 loader (`gxg-connector-kit`), not M2.
- The cron cannot be proven to fire from a local test; GitHub may delay schedules and disables them after
  60 days without repository activity — written in `bundle-signing.md`.
- A PR can still change the gate itself (ci.yml, gate script, `tools/gxg-policy/src/**`, `pyproject.toml`,
  `uv.lock`, `schemas/`): known limit, documented, needs an org ruleset or a second reviewer.
- End-to-end on a real PR: owner checklist; this cycle does not open PRs.
