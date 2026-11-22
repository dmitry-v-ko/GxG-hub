# M8 «Executor и исполнение» — READY (закрытие по гейту)

Closure by gate: owner decision 2026-11-22, plan «Ускорение процесса, третья редакция», p. 1 (6011a5d on the plan
branch). No new critic: the orchestrator checks the items below and the reports against the commit and closes the
milestone in `reports/progress.md`.

- Branch: `claude/m8-executor` (base 3c2f0b0, `origin/claude/integration`).
- Gate commit (code): **db17e0e**. The final code tree is identical: `git diff --stat db17e0e 9bc0eb5` lists
  `docs/backlog.md` and `reports/plans/M8-cycle-4.md` only. The commits after 9bc0eb5 add reports only.
- Gate: `scripts/ci/check-all.sh` in a full clone, **exit 0** (gate run 7). Per-step results are in
  `reports/conformance/M8.json` → `ci_local`. Command:
  `DOCKER_BUILD_FLAGS='--network host --secret id=ca-bundle,src=/root/.ccr/ca-bundle.crt' MUTMUT_PACKAGES='connectors/demo packages/gxg-connector-kit approval' scripts/ci/check-all.sh`.
- GitHub CI: not confirmed (no Actions minutes, owner decision 2026-11-21). `ci_local` stands in for it.

## Conditions of the gate

| Condition | State |
| --- | --- |
| All AC `passed` | AC-M8-01…14 passed (cycle-3 critic, `reports/critic/M8-cycle-3.json` → `acceptance`; `reports/conformance/M8.json`) |
| No open critical or major | the cycle-3 critic had 0 critical and 2 majors, both formal (a mutant id and missing evidence), both closed below |
| `required_improvements` formal only | yes: a mutant id, evidence in the repository, backlog rows |

## Cycle-3 `required_improvements` against the commit

| # | Required | Done | Where to check |
| --- | --- | --- | --- |
| 1 | `take__mutmut_29` → `take__mutmut_36` in `scripts/ci/mutmut-equivalent.txt`. Approval: 13 survivors, all listed, none stale | 1e7c951. Approval now has 24 survivors after the cycle-4 runs (the full module set: policy, transitions, quotas, intents, api, mcp, executor, keys). All 24 are listed; `Executor.take` has `_6` and `_36`; none stale | `reports/mutmut/M8.txt` («not killed: 79, listed as equivalent: 79», no «listed but killed now») |
| 2 | On the final commit, in a full clone: `check-all.sh` to the end with exit 0. Commit `M8.json` with `ci_local` (commit, command, exit, per-step result) and `M8.txt` (per-package counts, survivor ids, full SQL driver output) | Gate run 7: exit 0, 28 steps. `M8.txt` in 34fa1ed, `M8.json` in d562679 | `reports/conformance/M8.json` → `ci_local`; `reports/mutmut/M8.txt` |
| 3 | Minors 3–5 → `docs/backlog.md` (5 already there) | 3: the old provider table `calls` is a backlog row. 4: the check-ignore test skips without `.git` (ff0b209) and has a backlog row. 5: shallow-clone gitleaks was already there | `docs/backlog.md`, M8 rows |

## Gate run results (run 7)

- Lock checks, ruff, ruff format, mypy (strict where configured): clean.
- Tests:
  - pytest: 973 passed;
  - `approval/tests_db`: 357 passed on PostgreSQL 16.6;
  - kit: 373 passed, plus the coverage gate;
  - `connectors/demo`: 145 passed;
  - `tests/secrets`: 21 passed.
- Other checks:
  - `schema_lock` and `prove_schema_guard`;
  - gitleaks, tree and range: no leaks;
  - `gxg-policy validate`/`lint` and `bundle_smoke`;
  - `docker_smoke`: OK.
- Stand (ContextForge v1.1.1): 33/33 fresh, after restart and after recreate.
- mutmut, run on the packages M8 changes (DoD-1, owner decision 2026-11-22; a16fc39):
  - approval: 2229 killed, 24 survived, 0 timeout;
  - `connectors/demo`: 1859 killed, 42 survived, 2 no tests;
  - `packages/gxg-connector-kit`: 1160 killed, 11 survived;
  - all 79 not killed are listed with a reason.
- SQL driver: 98 rows, 0 not as expected.

## Score history

| Cycle | Score | Note |
| --- | --- | --- |
| 1 | 7.0 | critic, `reports/critic/M8-cycle-1.json` |
| 2 | 6.0 | critic, `reports/critic/M8-cycle-2.json` |
| 3 | 6.0 | critic, `reports/critic/M8-cycle-3.json` (formal majors only) |
| 4 | — | closure by gate, no critic (owner decision) |

7.0 → 6.0 → 6.0 is two cycles in a row with growth < 0.5, which the process turns into an escalation. Cycle 4 ran on
the orchestrator's order instead (2026-11-22 08:28), and the owner's gate rule then closed it. Improvement cycles
used: 3 of 4.

## Found and fixed while running the gate (cycle 4)

- **Shutdown wait.** Test servers waited 20 s at stop for a deliberately hanging request: uvicorn's graceful shutdown.
  `timeout_graceful_shutdown=1` (75a0de9, c630ad9).
- **Lock-wait test.** It counted `pg_locks` cluster-wide, so with mutants running in parallel it failed, or passed
  without checking anything. A failed wait also left the lock held. It now counts its own database's waits only and
  closes the blocker in `finally` (01f1c68).
- **`MUTMUT_PACKAGES` in `scripts/ci/mutmut.sh`** (db17e0e). Unset, mutmut runs every package as before, which is
  how CI calls it. Set to a subset, the stale-list check covers only the mutants that ran.
- **Committed mutant.** The heartbeat commit e7a808e (`git commit -a`) took in the SQL driver's in-flight mutant of
  `0001_core.py`. It is restored in 9bc0eb5, and the gate's code tree is db17e0e.
- **Environment.** mutmut 3.8.1 sends a stale wall-timeout `SIGXCPU` by PID. With `kernel.pid_max` at 32768, PIDs wrap
  in about 150 s, so healthy mutants got random timeouts in runs 2–4. `pid_max` was set to 4194304 in the container
  (the Ubuntu runner default); see the backlog.
- **Stand.** `stand/bootstrap.py` trusts `stand/secrets/state.json` after an interrupted run; see the backlog.

## pending_owner_decision

- **`max_attempts` per tool.** `schemas/approval-policy.schema.json` has no field for it. The default is 3 from the
  DDL, and AC-M8-08 sets the column in its test setup.

## Known limitations

- **Effect path through ContextForge.** It is not on the M5 stand, which has no Approval Service (plan decision 7).
- **Full mutmut of every package** runs at integration and before `main` (DoD-1).
