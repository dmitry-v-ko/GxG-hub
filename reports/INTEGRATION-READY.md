# INTEGRATION READY: M8, M9, M11 + specs (on top of M3, M4.1, M5, M7)

- Branch: `claude/integration`; code under test: `e869b14` (later commits change only `reports/`).
- Merged: `claude/m8-executor@69ff3e5` (M8), `claude/m9-ui-bot@3254fe0` (M9), `claude/m11-telegram-read@1dc20fb` (M11); `docs/specs`, `docs/adr`, `docs/implementation-plan-v1.md`, `reports/GOAL.md`, `reports/progress.md` from `claude/admiring-planck-vzys0x@44efcc8`.
- `git diff origin/claude/admiring-planck-vzys0x -- docs/specs docs/adr docs/implementation-plan-v1.md` is empty.
- Minimal changes beyond conflicts: M9's migration renumbered to `0004_ui_notifier` (down `0003`, two `0002` heads otherwise); `json`/`threading` imports kept in `approval/config.py`; M8's `test_human_approval_path` names an approver of the bundle (M9 freezes approvers at T1). Reasons and details: `reports/integration-2026-11-22-m8-m9-m11.md`.
- Nothing of M3/M4.1/M5/M7 or of the merged branches lost: `reports/critic|conformance|mutmut|plans/*` byte-equal, every `test_*` of every branch present, backlog rows of all branches present (10 rows dropped by M9 restored).

## Checks (local, full clone, Docker 29.10.0 started by hand, `kernel.pid_max` 4194304)

Run: `bash -x scripts/ci/check-all.sh` on `e869b14`, no `MUTMUT_PACKAGES`, `DOCKER_BUILD_FLAGS='--network host --secret id=ca-bundle,src=/root/.ccr/ca-bundle.crt'`, `gxg_agents_v2` clone at `../gxg_agents_v2` (pinned `f1c4d6d`).

| Check | Result |
|---|---|
| `uv lock --check` (root, demo, ui, notifier, telegram), `uv sync --locked` | OK |
| ruff check / format | OK (161 files) |
| mypy (all 8 calls from `check-all.sh`) | OK |
| `uv run pytest` (all packages + `tests/contract`) | 1146 passed |
| `approval/tests_db` (PostgreSQL 16.6) | 360 passed |
| `approval/ui/tests_db`, `approval/notifier/tests_db` (incl. E2E) | 109 passed |
| `approval/deploy/tests` (images, AS-29 networks) | 6 passed |
| kit + coverage 100% branches; demo 145; telegram 110 (PostgreSQL 16 + pgvector); schema lock; schema guard 66/66 | OK |
| `check_upstream.sh` pinned and `--drift` against `gxg_agents_v2` | OK, no archive changes after `f1c4d6d` |
| gitleaks tree HEAD and full range | no leaks found |
| `tests/secrets` 21; policy validate/lint (2 warnings, example files)/bundle | OK |
| `connectors/demo/docker_smoke.sh` | OK |
| `stand/run.sh` (M5 + M8 EX-01 + M11) | fresh 54/0, after-restart 54/0, after-recreate 54/0 |
| `scripts/ci/mutmut.sh`, Python part | gxg-policy 2555/2590, demo 1859/1903, telegram 1258/1279, gateway 132/133, kit 1204/1215, approval 2290/2314, approval/ui 687/689, approval/notifier 253/253, root 1211/1235 killed; not killed 162 = listed as equivalent 162, stale entries none |
| `scripts/ci/mutmut.sh`, SQL part (`mutate_m7_sql.py`) | 118 mutations, 0 not as expected (113 killed, 5 equivalent), rc=0 |

The first gate run (`aba22a5`) failed in `approval/tests_db` (`test_human_approval_path`, M8 × M9); fixed in `e869b14`, second run above. In the second run the container was lost during the last step of `mutmut.sh` (the SQL driver), after all 9 Python packages had finished. The SQL driver was re-run alone with the same PostgreSQL setup as `mutmut.sh`, and the list comparison of `mutmut.sh` was redone from the 9 `mutants/` results. Both are in the table. So `check-all: OK` was not printed by one process; every step of it passed on `e869b14`. Details: `reports/mutmut/integration-2026-11-22.txt`.

M9's three timed-out mutants (backlog): `demo_connector.server.x_build__mutmut_9` and the two `gxg_approval.transitions.x_rules` mutants are killed in this run, with no timeouts in demo or approval. That matches M8's finding (false timeouts of mutmut 3.8.1 at `pid_max` 32768). None was added to the equivalent list.

GitHub CI was not checked, everything above is local. `grants-widening` is red on a push without a PR, by M2 design.
