# M2 cycle 3 — ponytail-review

Diff: `2869731..e1dae73` (executor commits `e8d70ed`, `85eaad0`, `06c5636`, `e1dae73`; the merge `204d97d` of
`claude/integration` brings specs/schemas/plan and is read as input, not reviewed as M2 code).
Load assumed: one maintainer, a few agents opening PRs; the gate runs once per workflow event. Nothing here is
hot or shared, so the review is about correctness and the security of the approval gate, not scale.

What this change does: the `grants-widening` job now always diffs the PR head against
`merge-base(refs/remotes/origin/main, head)`, re-runs on `edited` (base change), and dates the head commit by the
first `pull_request` run after the latest force-push from the PR's own head repository. `gxg-policy build` now
fills `platform.services[].tools` from `connectors/*/gxg-manifest.yaml` (GR-11a, AS-26) and validates the bundle
by `grants-bundle.schema.json`; the signature test vector is reproduced byte for byte. Python mutation checks moved
to mutmut (`scripts/ci/mutmut.sh`), the custom script keeps only bash/YAML mutations.

## What I re-ran (clean clone of `e1dae73`, empty `UV_CACHE_DIR`, `UV_PYTHON=3.12` → 3.12.3, `UV_LOCKED=1`, uv 0.11.32)

- `uv run pytest`: 286 passed, 148 subtests, 0 skipped. `scripts/ci/check-all.sh`: OK.
- AC-M2-01: `validate policies/` → 0 errors; `lint policies/` → exit 0, 3 warnings.
- AC-M2-02: 20/20 fixtures in `tests/bad/` → exit 1, `error: <file>: <path>: [<code>]` (all 12 AC cases present).
- AC-M2-03: 9/9 `diff/*` pairs equal `expected.json`.
- AC-M2-05: two builds in separate processes (PYTHONHASHSEED 1 and 7, 2 s apart) equal except `built_at`.
- AC-M2-06: `verify` ok on a fresh build; one bit flipped at byte 200 → `bundle rejected: bad signature`, exit 1.
- mutmut (`bash scripts/ci/mutmut.sh`, 4 min 23 s): gxg_policy 2458 mutants, 2424 killed, 34 survived; gate
  536, 514 killed, 22 survived; all 56 survivors listed as equivalent → "mutmut: OK", exit 0. Same numbers as
  `reports/mutations/M2-cycle-3-mutmut.txt`. I read `mutmut show` for `bundle_doc_33`, `number_12`, `rule_9`:
  equivalent as described (guards `base != head`, decisions differ; lint rejects a group ref without groups).
- `mutate_m2.py` (bash/YAML only): 14/14 killed.
- CI on `e1dae73`: run 44820686966 (push). lint, types, tests, schema-guard, secrets, policy — success;
  `grants-widening` and `ci-ok` red by design ("12 widening change(s) outside a pull request"), `bundle` skipped.

## Bypass paths for a green `grants-widening`/`ci-ok` on a widening head without the owner's label

| Path | Status | Proof |
| --- | --- | --- |
| Base change (PR into `claude/base`, then retarget to `main`) | closed | diff is against main for every PR (`test_pull_request_into_another_branch_is_diffed_against_main`), `edited` in types (`test_base_change_reruns_the_gate…`), mutation "diff vs the PR base" killed |
| Fast-forward back to an earlier head (B → force-push A → label → FF B) | closed | `test_fast_forward_back_to_an_earlier_head_needs_a_new_label`; arrival = first run after the last force-push; killed under mutmut |
| Tag `origin/main` | closed | `refs/remotes/origin/main`; `test_tag_named_origin_main_does_not_shadow_the_branch`, mutation killed |
| push / workflow_dispatch on a branch | closed (red on any widening) | `test_branch_with_widening_is_red_without_a_pr[push/workflow_dispatch]` |
| schedule | not a path: runs only on `main`, check lands on the main SHA | `test_main_needs_no_approval` |
| Run cancellation | closed: PR runs never cancelled by concurrency; a manually cancelled run is `cancelled` ≠ success | `test_pull_request_runs_are_never_cancelled`, mutation killed |
| label / unlabel / reopen with an old label | closed | `test_label_older_than_head_commit_is_removed_on_any_event[*]` |
| Concurrency groups | closed (PR group = run_id) | same test |
| Runs of other PRs / branches / fork repos dating the head | closed | `test_runs_of_other_prs…`, `test_runs_from_another_head_repository_are_ignored`, `test_unknown_head_repository_fails_closed` |
| **PR edits the gate itself** (`ci.yml`, `scripts/ci/*`, `tools/gxg-policy/src/**`, `pyproject.toml`, `uv.lock`, `schemas/`) | **open** | finding 1 |
| **PR edits a connector manifest** (effect class used by the bundle) | **open** | finding 2, reproduced |
| Which of two same-name `ci-ok` checks counts; `performed_via_github_app` for app tokens; API fields | owner checklist (steps 3, 4, 11) | not testable offline |
| Re-run of an old run keeps `created_at`; a deleted and recreated head branch cannot be reopened at another SHA | assumption, not in the checklist | finding 4 |

## Must fix

1. **A PR can switch off its own gate** (`.github/workflows/ci.yml:173-178`, `scripts/ci/grants_widening.sh`)
   - **What this is:** `pull_request` workflows run the PR's own copy of `ci.yml`, and the job runs the PR's own
     `grants_widening.sh`, `widening_gate.py`, `gxg_policy` and whatever `uv.lock` installs.
   - **Problem:** A PR that adds a grant and also changes the first line of `grants_widening.sh` to `exit 0`
     (or changes `diff.py`, or pins a package in `uv.lock`) gets a green `grants-widening` and `ci-ok` on its head
     with no label. AC-M2-04 says such a PR does not merge. The executor documents this as a known limit
     (`docs/setup/widening-approval.md` «Ограничения»), but no owner decision accepting it is recorded in the plan.
     Five of the six paths can be closed in the repo: if the step runs the gate and `gxg-policy` from a worktree of
     `refs/remotes/origin/main` and that main-side gate also turns red (label needed) when the PR diff touches the
     gate paths, then only an edit of `ci.yml` itself is left. `ci.yml` cannot be protected from inside the repo on
     this plan (needs an org ruleset "required workflow" or a required code-owner review by a second person).
   - **Fix:** (a) executor: run gate code from main, and add "gate paths changed vs main → label required" with a
     job test; (b) owner: record in the plan that the remaining `ci.yml` path is accepted (owner reads every PR that
     touches `.github/workflows/`) or move the repo to an org ruleset with a required workflow.
   - **If we skip it:** an agent can merge widening by editing a lockfile or a tool module that the owner skims.

2. **Weakening a tool's effect class in a manifest is not widening** (`scripts/ci/grants_widening.sh:21`,
   `tools/gxg-policy/src/gxg_policy/bundle.py:73-77`, `diff.py`)
   - **What this is:** since this cycle `build` copies `effect_class`, `executes_via`, `action` of each tool from
     `connectors/*/gxg-manifest.yaml` into the signed bundle; by AS-26 the Approval Service takes them only from
     there, and AP-01 picks the base decision by class.
   - **Problem:** `gxg-policy diff` and the job read only `policies/`. Reproduced: same `policies/`, manifest with
     `demo.message.propose: external_effect` → `write_reversible`: the two bundles differ in `effect_class`, and
     `diff` reports 0 widening, 0 narrowing. After merge the bundle says `write_reversible` → approval
     `not_required` (AP-01 step 1): an effect without a human. `high_risk` → `external_effect` makes the tool
     match an existing `effect_classes: [external_effect]` `auto_approve` rule. `connectors/` is not even in
     CODEOWNERS. No connector manifest exists yet (`connectors/demo` has none), so the path opens with M3.
   - **Fix:** pass base/head manifests to `diff` (the job adds `connectors/` of both worktrees) and report, per
     connector tool: new tool, lower class (read < write_reversible < external_effect < high_risk), changed
     `action`/`executes_via` → widening; the reverse → narrowing. One fixture pair. Add `/connectors/*/gxg-manifest.yaml`
     to CODEOWNERS. ~30 lines.
   - **If we skip it:** the gate that M2 builds misses exactly the field that decides whether a human is asked.

## Should fix

3. **AP-02 lint uses one class per tool name across all connectors** (`lint.py:71`)
   - **What this is:** `self.classes` maps tool name → class over every manifest; a later manifest overwrites an
     earlier one.
   - **Problem:** manifests `a` and `b` both define `x.msg.propose`, one `high_risk`, one `external_effect`
     (sorted later wins): `auto_approve` for `x.msg.propose` passes lint while the bundle has a `high_risk` tool of
     that name. Runtime AP-02 still ignores it, so this is a lint gap, not an effect.
   - **Fix:** error `duplicate-tool` when two manifests declare the same tool name, or keep the strictest class.
     3 lines.
   - **If we skip it:** CI accepts a policy the spec says CI must reject.

4. **Two platform assumptions of the arrival rule are not in the owner checklist**
   (`scripts/ci/widening_gate.py:129-148`, `docs/setup/widening-approval.md`)
   - **What this is:** arrival = earliest `created_at` of a `pull_request` run for the head SHA after the last
     `head_ref_force_pushed`.
   - **Problem:** it is safe only if (a) re-running an old run does not change its `created_at`, and (b) a head can
     return to an older SHA only with a force-push event. Case for (b): head B ran 09:00, branch deleted, recreated
     at A, PR reopened, owner labels A at 10:00, fast-forward to B at 11:00 → no force-push event, arrival 09:00 <
     label → green on B. GitHub normally refuses to reopen a PR whose branch was recreated, but this is not checked.
   - **Fix:** two checklist steps: re-run an old run and compare `created_at`; delete/recreate the head branch and
     try to reopen. No code.
   - **If we skip it:** one undocumented way around the label may stay unknown.

## Nice to have

5. **Hand-written `tools` stay in `policies/platform/registry.example.yaml`** only to be dropped by `build` and
   warned about by lint (`ignored-tools`). Deleting the two lines leaves the example honest. -2 lines.

## Existing solutions (ponytail ladder, rungs 2-4)

- mutmut used for all Python key modules (DoD-4). `mutmut.sh` is needed: mutmut 3 exits 0 with survivors and has
  no allow-list of equivalent mutants (`# pragma: no mutate` would also drop the non-equivalent mutants on that
  line). Justified.
- Bash/workflow mutations (`mutate_m2.py`, 14 cases): no maintained mutation tool for shell or Actions YAML. Justified.
- Bundle shape: `jsonschema` + `referencing` (installed). Manifest shape: same. RFC 3339: `rfc3339-validator`
  (the jsonschema extra). Signature: `cryptography`. All existing.
- Arrival/staleness rule: GitHub's own "dismiss stale approvals / approval of the most recent push" needs a second
  reviewer (excluded by AC-M0-03); environments with reviewers are not the label AC-M2-04 prescribes. Justified.
- For finding 1 the existing solutions are org rulesets with a required workflow or a required code-owner review;
  both need an owner decision, not code.

Verdict: fix 1 and 2 first.
Lean: -2 lines possible.
Not checked: how GitHub picks between two same-name `ci-ok` checks on one SHA, `performed_via_github_app` for agent
tokens, re-run `created_at` and branch-recreate reopen (owner checklist / finding 4).
