# INTEGRATION READY: M10 (Webhooks and operations) on top of M1.1, M3, M4.1, M5, M7, M8, M9, M11

- Branch: `claude/integration`; code under test: `1c6c970` (later commits change only `reports/`).
- Merged: `claude/m10-ops@280541d` (M10 closed at 9.0, `d75ab91` is its ancestor); `docs/specs`, `docs/adr`, `docs/implementation-plan-v1.md`, `reports/GOAL.md`, `reports/progress.md` from `claude/admiring-planck-vzys0x@d6d5e35`.
- `git diff origin/claude/admiring-planck-vzys0x -- docs/specs docs/adr docs/implementation-plan-v1.md` is empty.
- demo vs template (M1.1): M10 changed only domain files of demo (`effects.py`, `provider.py`, tests); `test_demo_is_on_the_template` passes, `DEMO_DRIFT` unchanged, broken copies regenerated with no diff.
- Two integration defects found and fixed: demo's metrics counter in the default prometheus registry broke `check-connectors` (copies imported in one process) → own registry; an M1.1 test with a fixed bundle date went stale after 24 h (GR-13) → built now. Details: `reports/integration-2026-11-23-m10.md`.
- Nothing lost: every file, every `test_*` function (except 12 step-up tests M10 itself removed for the `decider`), every backlog row and every equivalent mutant of both sides is present; `reports/critic|conformance|mutmut|plans/*` unchanged.

## Checks (local, full clone, Docker 29.10.0 started by hand, mirror `mirror.gcr.io`, `kernel.pid_max` 4194304)

Run: `check-all.sh` without `mutmut.sh`, on `1c6c970`, `DOCKER_BUILD_FLAGS='--network host --secret id=ca-bundle,src=/root/.ccr/ca-bundle.crt'`, `UPSTREAM_DRIFT=skip`. Output ends with `check-all: OK`.

| Check | Result |
|---|---|
| `uv lock --check` (5), `uv sync --locked`, ruff, mypy (all calls) | OK |
| `uv run pytest` | 1301 passed |
| `approval/tests_db` | 438 passed |
| `approval/ui/tests_db`, `approval/notifier/tests_db` (incl. E2E) | 143 passed |
| `approval/deploy/tests` | 9 passed |
| kit 473 + coverage of key modules; schema lock; schema guard | OK |
| demo 149 + `check-connectors`; telegram 110 | OK |
| gitleaks tree and range; `tests/secrets` 21 | no leaks found |
| policy validate / lint, bundle smoke, `docker_smoke.sh` | OK |
| `stand/run.sh` | fresh 54/0, after-restart 54/0, after-recreate 54/0 |
| `reports/conformance/stage-C.json` on the merged code | 26/26 |
| ponytail-review of the diff | Ship; two nice-to-have notes (receiver `seen` set grows; demo `/metrics` has no process metrics) |
| `scripts/ci/mutmut.sh` | not run (light process: full run once before `main`) |

## Open

- `check_upstream.sh` (pinned and `--drift`) against `gxg_agents_v2` **not run**: no clone here, `add_repo` forbidden for this thread (`UPSTREAM_DRIFT=skip`). M10 does not touch telegram or the archive schema. Run it where a clone exists before `main`.
- `max_attempts` (policy field): implemented by M10, absent from `approval-policy-v1.md` on the docs branch (the integration took the docs branch). Owner decision `pending_owner_decision`: add the row to the spec or drop the field.
- GitHub CI not checked; everything above is local.
