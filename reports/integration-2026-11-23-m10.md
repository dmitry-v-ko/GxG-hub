# Integration 2026-11-23: M10 (Webhooks and operations, 9.0) on top of M1.1

Branch `claude/integration`. Code under test: `1c6c970` (later commits change only `reports/`). Light process (plan, "Облегчённый процесс"): no mutmut run.

## Merged

| What | From |
|---|---|
| M10 code, tests, reports | `claude/m10-ops@280541d` (`d75ab91` is its ancestor; the two later commits touch only `docs/backlog.md` and `reports/M10-READY.md`) |
| `docs/specs`, `docs/adr`, `docs/implementation-plan-v1.md`, `reports/GOAL.md`, `reports/progress.md` | `claude/admiring-planck-vzys0x@d6d5e35` |

`git diff origin/claude/admiring-planck-vzys0x -- docs/specs docs/adr docs/implementation-plan-v1.md` is empty.

## Conflicts and what was done

| File | Resolution |
|---|---|
| `docs/backlog.md`, `scripts/ci/mutmut-equivalent.txt` | union of both sides (no row or mutant dropped; checked with `comm` against `a5e2cc1` and `m10-ops`) |
| `packages/gxg-connector-kit/pyproject.toml` | both `only_mutate` lists (M1.1 `mcp.py`, `effects.py` + M10 `webhooks.py`) |
| `connectors/demo/pyproject.toml` | `gxg-connector-kit[mcp]` (M1.1) + `prometheus-client` (M10) |
| `uv.lock`, `connectors/demo/uv.lock` | HEAD side, then `uv lock` |
| `approval/notifier/uv.lock` | not a conflict, but stale: M1.1 added the kit `mcp` extra; relocked (`2f29d0c`) |
| `docs/specs/approval-policy-v1.md` | taken from the docs branch (M10 had added a `max_attempts` row, see below) |

## Findings of the integration (both were red in the first gate run, both fixed)

1. **demo metrics counter vs `check-connectors`** (M10 × M1.1). M10 registered `gxg_token_verify_failures_total` in prometheus' default registry at import of `demo_connector.effects`. `check-connectors` imports several copies of demo (broken copies) in one process, so the second import raised `DuplicateTimeseries` (21 errors in `tools/gxg-connector-check`, then `test_cli_on_demo_and_usage`). Fix: demo keeps its own `CollectorRegistry` (`effects.METRICS`) and `/metrics` serves it. The M10 test reads the text of `/metrics`, so it did not change.
2. **A time bomb in an M1.1 test.** `test_bundle_is_byte_identical_to_gxg_policy_build` built a bundle with a fixed `built_at` of 2026-11-22 10:00; the kit refuses a bundle older than 24 h (GR-13), so the test went red on 2026-11-23 on its own. Fix: `BUILT_AT` is now the current time. Other fixed dates in tests use an injected clock (checked by grep).

## demo and the template (M1.1 DEMO_DRIFT)

M10 changed `effects.py`, `provider.py` (mode `execute_quota`, EX-25) and `tests/test_effects.py`; none of them is a template-owned file, `server.py` is not touched. `test_demo_is_on_the_template` passes unchanged, `DEMO_DRIFT` has no new entry. `make_patches.py` rerun: no diff in the broken copies.

## Nothing lost

- Every file of both branches is present (`git diff --diff-filter=D` against `a5e2cc1` and `m10-ops`: empty).
- Every `test_*` function of `a5e2cc1` and of `m10-ops` is present except 12 that M10 itself removed against `40cec84` (`test_step_up_*`, `test_registration_at_five_minutes`, `test_reject_needs_no_step_up`): step-up in the ui was replaced by the `decider` (AS-30). The merge lost none.
- All backlog rows and equivalent mutants of both sides are present (union, nothing obsolete found).
- `reports/critic|conformance|mutmut|plans/*` of both branches unchanged (the stand rewrote `reports/conformance/M5.json` and `M11.json` during the gate; reverted, not committed).

## Gate

Environment: full clone (unshallowed), Docker 29.10.0 started by hand (`kernel.pid_max` 4194304, mirror `mirror.gcr.io`), `postgresql-16-pgvector` installed in the container for the telegram tests, `DOCKER_BUILD_FLAGS='--network host --secret id=ca-bundle,src=/root/.ccr/ca-bundle.crt'`. Run: `check-all.sh` without its last line (`mutmut.sh`) and with `UPSTREAM_DRIFT=skip`; ends with `check-all: OK`.

| Check | Result |
|---|---|
| `uv lock --check` (5 projects), `uv sync --locked`, ruff check/format | OK |
| mypy (all calls of `check-all.sh`) | OK |
| `uv run pytest` | 1301 passed |
| `approval/tests_db` | 438 passed |
| `approval/ui/tests_db` + `approval/notifier/tests_db` (E2E included) | 143 passed |
| `approval/deploy/tests` | 9 passed |
| kit 473 passed; coverage of key modules (grants 98.1 %, rest 100 % branches); schema lock; schema guard | OK |
| demo 149 passed; `check-connectors` on demo | passed |
| telegram | 110 passed |
| gitleaks tree + range; `tests/secrets` 21 | no leaks found |
| policy validate/lint, bundle smoke, `docker_smoke.sh` | OK |
| `stand/run.sh` | fresh 54/0, after-restart 54/0, after-recreate 54/0 |
| `reports/conformance/stage-C.json` rerun on the merged code (`45dd530`, stand JUnit of this run) | 26/26 passed (EX-01…EX-25, MC-05) |
| `scripts/ci/mutmut.sh` | not run (light process: full run once before `main`) |

Not done: `check_upstream.sh` (pinned and `--drift`) against `gxg_agents_v2`. There is no clone in this environment and `add_repo` is forbidden for this thread, so `UPSTREAM_DRIFT=skip`. M10 does not touch `connectors/telegram` or its archive schema; the step must be run where a clone exists (or by giving this thread one) before `main`. GitHub CI not checked.

Earlier gate runs on this branch: run 1 (red: stale notifier lock; after the relock: two failures from the findings above, plus 75 telegram errors because pgvector was missing in the container); run 2 (426 DB errors: Docker was not started). Run 3 on `1c6c970` is the one counted.

## Spec vs code: `max_attempts`

`approval-policy-v1.md` on the docs branch has no `max_attempts` (1…3, default 3, in `defaults` and rules; T1 fixes it). M10 documented it as `pending_owner_decision` and implemented it (schemas `approval-policy.schema.json`, kit, approval). Taking the docs branch removed the row from the spec in this branch; the code still implements it. The owner/orchestrator must either put the row into the spec on `claude/admiring-planck-vzys0x` or tell M10 to drop the field.

## ponytail-review of the integration diff

What this change does: merges M10 (webhook signature in the kit, `decider` replacing ui step-up, executor, quotas, metrics and alerts, runbook) into the branch with M1.1, and makes two small repairs.

Checked: my two repairs, the conflict files, `webhooks.py`, the `grants.py` change, `decider.py` (reread in full: the ui is not trusted, the human must own the passkey, one refusal text for every verification failure, challenge used once, input lengths bounded). Not read line by line: the SQL migrations, `executor.py`, the ui and the 4000 lines of M10 tests (the critic of M10 did, 9.0).

Must fix: none.

Should fix: none.

Nice to have:
1. **`webhooks.verify` keeps `seen` forever** (`packages/gxg-connector-kit/src/gxg_connector_kit/webhooks.py`).
   - What this is: the receiver's set of handled `event_id`s is owned by the caller.
   - Problem: a receiver that runs for months with one set grows without bound. A delivery older than 300 s is refused as `stale` anyway, so ids older than about 10 minutes can be dropped.
   - Fix: when M12 (Signals) writes the first receiver, keep ids in a dict with a time and drop those older than 2 × `TOLERANCE_S`, or use a table with `ON CONFLICT DO NOTHING`.
   - If we skip it: slow memory growth in a long-running receiver. No receiver exists yet.
2. **demo `/metrics` no longer shows the python/process metrics** (`connectors/demo/src/demo_connector/effects.py`).
   - What this is: my fix serves only demo's own registry.
   - Problem: the default registry also had `python_gc_*` and `process_*`; no alert rule uses them (grep of `approval/deploy/prometheus`: none).
   - Fix: none needed now; if a dashboard wants them, register `ProcessCollector` on `METRICS`.
   - If we skip it: nothing.

Verdict: Ship.
Lean: no lean findings.
Not checked: SQL migrations, `executor.py` and ui code beyond the critic of M10.

## Commits of this integration

`7eb5aab` merge; `2f29d0c` notifier lock; `1c6c970` two fixes; `45dd530` heartbeat; `1440b94` stage-C rerun; the report commit.
