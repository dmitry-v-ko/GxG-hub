# M2 cycle 5 — ponytail-review of `ed3faad..6ffcadd`

Reviewer: independent critic, cycle 5 (last by the plan limit). Code commit `17ce7f4`, mutmut list and reports `6ffcadd`,
executor report `e169154`.
Load: one maintainer, a few agent PRs a day, the gate runs once per PR event. Scale is not a concern.
Owner decisions in force: a PR that edits `.github/workflows/` can switch the gate off (accepted risk, `8f6754e`, the
owner reviews such PRs); AC-M2-04 on a real PR is the owner's manual check. Neither is a finding.

## What this change does

Two cycle-4 majors, about 10 lines of code:

1. `lint.load_manifests` now adds every tool name to `seen` while it walks a manifest, so a name repeated **inside one
   manifest** is a `duplicate-tool` error, as across manifests. `diff._load` raises on errors, so the diff exits 2
   and the gate is red.
2. `_Diff.tools`: any `effect_class` change is widening (`effect_class_changed`), in both directions. `EFFECT_ORDER`
   is gone. Golden reports of `10-manifest-tools` (both directions) are updated.

Plus: one lint test, two diff tests (duplicate inside one manifest → `PolicyError` and CLI exit 2; raised class under a
class-matched `deny` → widening), docs line in `widening-approval.md`, one stale mutmut equivalent removed.

## Checked and fine

- **Cycle-4 finding 1 reproduced and now closed.** Same input as the cycle-4 report (fixture 10 base, a
  `demo.message.propose` 2.0.0 `write_reversible` inserted before the 1.0.0 `external_effect`): `gxg-policy diff`
  → exit 2, `tools[2].name: [duplicate-tool] tool 'demo.message.propose' is declared twice`. The same copy placed in
  the `other` manifest → exit 2 as well.
- **Cycle-4 finding 2 closed.** Covered by `test_raised_effect_class_under_a_class_matched_deny_is_widening`
  (exact report: one widening entry, no narrowing). Goldens: raise and lower are both in `widening`;
  reverse golden matches (`diff head base` with swapped connector dirs = `expected-reverse.json`).
- **New attempts on the manifest path, all closed** (my own inputs on top of fixture 10):
  - YAML duplicate key in one tool (`effect_class` twice) → `_Loader` rejects it, exit 2.
  - YAML merge key `<<: {effect_class: write_reversible}` → not constructible by the loader, exit 2.
  - `effect_class` removed → schema error, exit 2.
  - `connector: demo` renamed to `demo2` → 4 `added` widening + 4 `removed` narrowing: red.
  - New manifest in a hidden directory `connectors/.hid/` → glob sees it, 4 `added` widening. `build` uses the same
    glob (`default_manifests`), so bundle and diff see the same files.
  - Fields not compared (`version`, `dispatch_timeout_s`, `preview`, `resource`): none changes the class or the
    approval decision (AP-01 uses class, tool name, resource RID, requester); with one entry per name a version
    change only changes which version AS-26 accepts. Documented in `widening-approval.md`.
- **Regressions:** none found. `git grep` finds no leftover `effect_class_lowered|raised`, `EFFECT_ORDER` or the old
  message text. Policy-side diff code is untouched by this cycle.
- **Re-run** (clean clone of `6ffcadd`, empty `UV_CACHE_DIR`, `UV_PYTHON=3.12`, `UV_LOCKED=1`): `check-all.sh` OK,
  pytest 307 passed, 0 skipped; AC-M2-01…03, 05, 06 by CLI (validate 0 errors; lint 20/20 bad fixtures exit 1 with
  file and path; diff 10/10 goldens + reverse golden; two builds with PYTHONHASHSEED 1/7 equal without `built_at`;
  verify ok, one flipped bit → `bad signature`, exit 1). `scripts/ci/mutmut.sh` exit 0: gxg_policy 2555/2590 killed,
  widening_gate.py 557/581, 59 not killed, all 59 listed as equivalent, no stale entries; `mutate_m2.py` 24/24 killed.
  The new code is covered: no new entry in the equivalent list, and the old `tools__mutmut_54` is killed now.
- **CI on `6ffcadd`** (run 44895018401, push): lint, types, tests, schema-guard, secrets, policy success;
  grants-widening and ci-ok red by design (outside a PR, bootstrap, widening vs main); bundle skipped.

## Must fix

None.

## Should fix

None.

## Nice to have

1. **`reports/conformance/M2.json` → `bundle_version` is stale** (`"dd0beb1 (local smoke build in check-all…)"`).
   - The report is for `6ffcadd`; `check-all.sh` on `6ffcadd` prints `ok: bundle_version=6ffcaddc…`. The field was
     not updated this cycle. Cosmetic: no AC depends on it, every other commit field is right.
   - Fix: write the commit the smoke build actually used.

## Ready-made solutions (ponytail ladder 2–4)

- Duplicate names inside one manifest: a set while iterating, the same rung as the existing cross-manifest check.
  JSON Schema `uniqueItems` compares whole objects, not one key, so it cannot express this. Justified.
- `effect_class` change: one comparison, no tool needed.
- Unchanged components (gate from main via `git worktree` + `uv run --locked --project`, `git diff --name-only` for
  gate paths, mutmut 3.8.1 for Python, `mutate_m2.py` for bash/YAML where no maintained mutation tool exists,
  JCS/Ed25519/jsonschema): verdicts as in `M2-cycle-4.json` / `M2-cycle-3.json`, re-checked: nothing changed.

Lean: -0 lines possible.

Verdict: ship. Both majors of cycle 4 are closed by the smallest fix, with tests, and nothing regressed.
Not checked: the real-PR checklist (owner); how the M7 Approval Service will read the bundle (outside M2).
