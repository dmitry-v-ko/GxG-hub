# INTEGRATION READY: M1.1 (MCP Toolkit) on top of M3, M4.1, M5, M7, M8, M9, M11

- Branch: `claude/integration`; code under test: `bc712f5` (later commits change only `reports/`).
- Merged: `claude/m1.1-toolkit@bffcbd4` (services pin `7e7ce56` is in the history); `docs/specs`, `docs/adr`, `docs/implementation-plan-v1.md`, `reports/GOAL.md`, `reports/progress.md` from `claude/admiring-planck-vzys0x@f559284`.
- `git diff origin/claude/admiring-planck-vzys0x -- docs/specs docs/adr docs/implementation-plan-v1.md` is empty.
- demo M8 vs `gxg_connector_kit.effects`: orchestrator's decision, option 1. demo keeps its own `effects.py`. The M8 tests are unchanged and green (81/81, demo 146/146). demo is the template render `with_effects: true` up to `DEMO_DRIFT`: six replacements in `server.py`, each with a reason. The broken copies were regenerated. Moving demo onto the kit is a `docs/backlog.md` row. Critic of the M1.1 test changes: accept with minor, 8/10; the minors are fixed or in the backlog. Details: `reports/integration-2026-11-23-m1.1.md`.
- AC-M1.1-04: the M8 tests are green, and demo is on the template up to `DEMO_DRIFT`. `check-connectors` on demo: 19/19, EX-01…EX-16 included.
- Nothing lost: every file, every `test_*` function and every backlog row of `40cec84` and `m1.1-toolkit` is present. `reports/critic|conformance|mutmut|plans/*` of both branches are unchanged; the equivalents list and `.gitleaksignore` are a union.

## Checks (local, full clone, Docker 29.10.0 started by hand, registry mirror `mirror.gcr.io`, `kernel.pid_max` 4194304)

Run: `bash -x scripts/ci/check-all.sh` on `bc712f5`, `DOCKER_BUILD_FLAGS='--network host --secret id=ca-bundle,src=/root/.ccr/ca-bundle.crt'`, `gxg_agents_v2` clone at `../gxg_agents_v2` (pinned `f1c4d6d`, read-only).

| Check | Result |
|---|---|
| `uv lock --check` (root, demo, ui, notifier, telegram), `uv sync --locked` | OK |
| ruff check / format | OK (180 files) |
| mypy (all 9 calls from `check-all.sh`) | OK |
| `uv run pytest` (all packages, `tests/contract`, `tests/toolkit`, `tools/gxg-connector-check`) | 1275 passed |
| `approval/tests_db` | 360 passed |
| `approval/ui/tests_db`, `approval/notifier/tests_db` (incl. E2E) | 109 passed |
| `approval/deploy/tests` | 6 passed |
| kit 457 + coverage of the key modules 100 % branches; schema lock; schema guard | OK |
| demo 146 + `check-connectors` 19/19; telegram 110 | OK |
| `check_upstream.sh` pinned and `--drift` | OK |
| gitleaks tree HEAD and full range; `tests/secrets` 21 | no leaks found |
| policy validate / lint / bundle smoke | OK |
| `connectors/demo/docker_smoke.sh` | OK |
| `stand/run.sh` (M5 + M8 EX-01 + M11) | fresh 54/0, after-restart 54/0, after-recreate 54/0 |
| `scripts/ci/mutmut.sh` | not run: owner's decision of 2026-11-23 (plan `f559284`): mutmut only before the merge to main |

`check-all: OK` was not printed because it was stopped at `mutmut.sh`; every step before that passed in one run on `bc712f5`.

GitHub CI was not checked; everything above is local.
