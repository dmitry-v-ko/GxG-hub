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
| 2 | evidence on the final commit | `scripts/ci/check-all.sh` to the end in a full clone, exit 0; `reports/conformance/M8.json` with `ci_local` (commit, command, exit, per step); `reports/mutmut/M8.txt` per package, survivor ids, full SQL driver output | in progress: see «Gate runs» below |
| 3 | minors 3–5 | 3 (old provider table on reconcile) → `docs/backlog.md`; 4 (check-ignore test without `.git`) fixed: skips with a reason; 5 already in the backlog | done in ff0b209 |

## Closure by gate

Owner decision 2026-11-22 (plan «Ускорение процесса, третья редакция», p. 1, 6011a5d on the plan branch):
M8 cycle 4 closes by gate, with no new critic. Cycle 3 meets the conditions: AC-M8-01…14 all `passed`; its two
majors are formal (a stale mutant id, missing evidence) and items 1 and 3 are done. Item 2 is the gate run.
DoD-1 (a16fc39): the milestone's check-all runs mutmut only for the packages M8 changes:
`MUTMUT_PACKAGES="connectors/demo packages/gxg-connector-kit approval"` (the kit is included because M8 changed
`exec_token.py`), plus the SQL driver.

## Gate runs (full clone, `DOCKER_BUILD_FLAGS` with the proxy CA secret)

| Run | Commit | Result | Cause / change |
| --- | --- | --- | --- |
| 1 | ff0b209 → 5081e21 (plan committed during the run) | docker_smoke failed | `DOCKER_BUILD_FLAGS` missing (proxy CA) |
| 2 | 5081e21 | stopped at mutmut | `transitions.x_rules` 66, 68, 175 `timeout`; teardown waited 20 s for a hanging `slow:30000` request → 75a0de9, c630ad9 (`timeout_graceful_shutdown=1`) |
| 3 | c630ad9 | stopped at the SQL driver | x_rules 193, 196–198 `timeout`; lock test counted `pg_locks` cluster-wide → 01f1c68 (own database, blocker closed in `finally`) |
| 4 | 01f1c68 | exit 1 | all steps green up to mutmut (stand 33/33 ×3); mutmut: 1 `timeout` (x_rules 187, exit −24); SQL driver baseline red. Cause: mutmut 3.8.1 sends a stale wall-timeout `SIGXCPU` by PID, and `kernel.pid_max` 32768 wraps in ~150 s (backlog); `pid_max` raised to 4194304 in the container (Ubuntu runner default) |
| 5 | 01f1c68 | stopped | superseded by DoD-1 (milestone packages only) → db17e0e (`MUTMUT_PACKAGES`) |
| 6 | db17e0e | stand 6/33 | stand state from the stopped run 5 (`state.json` vs ContextForge DB, backlog); volumes and `state.json` reset |
| 7 | db17e0e (+c7b37ca, 34c9929 docs) | running: lock checks, ruff, mypy, pytest 973, tests_db 357, kit 373 + coverage gate, demo 145, schema guard, gitleaks tree/range, tests/secrets, policy validate/lint, bundle_smoke, docker_smoke green; stand 33/33 ×3 (fresh, after restart, after recreate); mutmut demo, kit, approval done (0 timeouts); SQL driver running, 61 of ~98 rows done, all as expected (20:17 UTC) | |

## Status

Gate run 7 on db17e0e: the green check-all the gate closure needs.
