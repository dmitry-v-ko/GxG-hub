# M2 cycle 4 — plan (ponytail full)

Input: critic `reports/critic/M2-cycle-3.json` (score 8.0, two major, three minor) and `.review.md`.
Spec, plan and acceptance criteria are not changed. I agree with every item.

## Scope

| Item | Files | Rung |
| --- | --- | --- |
| Gate runs from main (major 1) | `.github/workflows/ci.yml` (job `grants-widening`), `scripts/ci/grants_widening.sh` | minimum code: the step adds a worktree of `refs/remotes/origin/main` and runs **its** `grants_widening.sh`; the script runs `gxg-policy` and `widening_gate.py` with `uv run --locked --project <main worktree>`. The job no longer runs `uv sync` of the PR (PR code never executes in the job). Bootstrap: while main has no gate script, the PR copy runs (the PR then changes gate paths, so it needs the label anyway) |
| Gate paths need the label | `grants_widening.sh`, `widening_gate.py` (`--gate-paths`) | one `git diff --name-only base head -- <paths>`; each changed path is added to `widening` as `kind: gate_path`, so the existing label logic decides |
| Test: PR edits the gate and widens → red | `tests/ci/test_grants_widening_job.py` | runs the `ci.yml` step text in a temp repo whose main holds the gate: branch sets its `grants_widening.sh` to `exit 0` and widens → red; gate edit only → red; unrelated edit → green |
| Residual `ci.yml` risk | `docs/setup/widening-approval.md`, `reports/conformance/M2.json` | text, `owner_decision_needed` |
| Manifest changes are widening (major 2) | `gxg_policy/diff.py`, `cli.py` (`--base-connectors`, `--head-connectors`), `grants_widening.sh` | minimum code, reusing `load_manifests`: per (connector, tool) new → widening, removed → narrowing; `effect_class` lowered (read < write_reversible < external_effect < high_risk) → widening, raised → narrowing; `action`/`executes_via` changed → widening |
| Fixture pair + golden | `tests/diff/10-manifest-tools/{base,head,base-connectors,head-connectors,expected.json}` | data |
| CODEOWNERS | `.github/CODEOWNERS` | one line `/connectors/*/gxg-manifest.yaml` |
| Duplicate tool name (minor 3) | `gxg_policy/lint.py` `load_manifests`, `tests/bad/21-duplicate-tool` | 4 lines: `duplicate-tool` error |
| Checklist (minor 4) | `docs/setup/widening-approval.md` | two steps: re-run keeps `created_at`; delete/recreate head branch and reopen |
| Lean (minor 5) | `policies/platform/registry.example.yaml` | delete hand-written `tools` |
| Re-run | pytest, `scripts/ci/mutmut.sh`, `mutate_m2.py`, CI | new mutations for the new lines |

## DoD-6: ready solutions considered for code I write

| Component | Considered | Decision |
| --- | --- | --- |
| Running the gate from main | `pull_request_target` (runs the base workflow, but with a write token next to PR code — the classic pwn-request pattern; and the base is the PR base, not main); org ruleset "required workflow" (owner/platform decision); `actions/checkout` with `ref: main` into a subdir | `git worktree add` of `refs/remotes/origin/main` (already fetched, `fetch-depth: 0`): no new action, same ref the diff uses |
| Gate-path detection | `dorny/paths-filter`, `tj-actions/changed-files` (third-party actions, the latter had a supply-chain compromise in 2025) | `git diff --name-only` (git) |
| Manifest diff | none: GR-11a/AS-26 projection is project-specific | ~25 lines in `_Diff`, manifests loaded by the existing `load_manifests` (jsonschema) |
| Duplicate tool check | JSON Schema cannot express uniqueness across files | 4 lines in `load_manifests` |

## Not done, on purpose

- `ci.yml` itself: a PR can still rewrite the `grants-widening` job so the main copy never runs. Not closable in the
  repo on this plan (needs a required code-owner review by a second person or an org ruleset with a required
  workflow). Recorded as `owner_decision_needed`; the orchestrator asks the owner.
- Manifest fields other than `effect_class`, `action`, `executes_via` (`dispatch_timeout_s`, `preview`, `version`):
  not asked; they do not change who approves.
