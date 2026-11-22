# M8 cycle 4 — plan (ponytail lite)

Input: `reports/critic/M8-cycle-3.json` (score 6.0 on ddf8c9f, caps: dod, major — the critic reviewed while the
cycle-3 self-check was still running). Its `required_improvements` 1–3 are the closed checklist.

Escalation rule: scores 7.0 → 6.0 → 6.0 are two cycles in a row with growth < 0.5, which the process turns into
`reports/M8-ESCALATION.md`. The orchestrator (for the owner, 2026-11-22 08:28) ordered a short cycle 4 for the
remaining formal items instead; this is recorded here and in the final report, not decided by the owner of the
milestone. Improvement cycles used: 3 of 4 (cycles 2, 3, 4).

| # | Item | Change | Status |
| --- | --- | --- | --- |
| 1 | stale equivalent id | `take__mutmut_29` → `take__mutmut_36`; approval: 13 survivors, all listed, none stale | done in 1e7c951 (before the critic report reached the branch) |
| 2 | evidence on the final commit | `scripts/ci/check-all.sh` to the end in a full clone, exit 0; `reports/conformance/M8.json` with `ci_local` (commit, command, exit, per step); `reports/mutmut/M8.txt` per package, survivor ids, full SQL driver output | running on ff0b209 |
| 3 | minors 3–5 | 3 (old provider table on reconcile) → `docs/backlog.md`; 4 (check-ignore test without `.git`) fixed: skips with a reason; 5 already in the backlog | done in ff0b209 |
