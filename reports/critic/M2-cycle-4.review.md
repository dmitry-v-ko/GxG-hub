# M2 cycle 4 — ponytail-review of `285a99e..dd0beb1`

Reviewer: independent critic, cycle 4. Code commit `dd0beb1` (code in `90d4985`, mutmut list and assertions in `dd0beb1`).
Load assumed: one maintainer, a few agent PRs a day. The gate runs once per PR event. Scale is not a concern here.
Accepted risk (owner, 2026-11-21): a PR that edits `.github/workflows/` can switch the gate off. The owner reviews
such PRs, and CODEOWNERS marks them. This is not a finding. The text in `docs/setup/widening-approval.md`
«Ограничения» describes it accurately (checked: step removed / `exit 0` / `needs` edited are named, and so is the bootstrap).

## What this change does

The `grants-widening` job now runs its gate (`grants_widening.sh`, `widening_gate.py`, `gxg-policy`, `uv.lock`) from
a worktree of `refs/remotes/origin/main`. Any change to gate paths in the PR counts as widening. `gxg-policy diff`
now also compares connector manifests (`connectors/*/gxg-manifest.yaml`). A new tool, a lowered `effect_class`, or a
changed `action` / `executes_via` counts as widening. Lint rejects the same tool name in two manifests.

## Checked and fine

- Gate code from main: `uv run --locked --project <main worktree>` ignores the PR's files in the working directory.
  Tested: a bogus `uv.toml` and a `.python-version` in the cwd were ignored. The job has no `uv sync` of the PR.
  CODEOWNERS is read from main. Job tests: a PR whose `grants_widening.sh` is `exit 0` and that widens → red; a
  gate-path edit alone → red; an unrelated edit → green.
- Gate paths: `git diff --name-only base head -- <paths>`, and every path is a `gate_path` widening entry.
- Manifest diff for the four cases in the plan: fixture `10-manifest-tools` with a golden report in both
  directions. A tool moved to another connector → widening. An invalid manifest → exit 2 (red).
- A duplicate tool name **across** two manifests → `duplicate-tool` error.

## Must fix

1. **The same tool name twice in one manifest: lint and diff stay silent, and the signed bundle gets a weaker class** (`tools/gxg-policy/src/gxg_policy/lint.py:59-65`, `diff.py:211-212`)
   - **What this is:** The new `duplicate-tool` check only compares a manifest with the manifests loaded **before**
     it. The diff builds `{(connector, name): tool}`, so when a name repeats, the last entry wins. The bundle copies
     **every** entry (`bundle.py:76-77`). AS-26 finds the tool in the bundle by `(iss, tool_key, tool_version)`.
   - **Problem:** Reproduced on the base of fixture `10-manifest-tools`. Insert at the top of `demo`'s tools
     `demo.message.propose` with `version: 2.0.0`, `effect_class: write_reversible`. The original `1.0.0
     external_effect` stays last. Result: `lint` gives 0 errors, `diff` gives 0 widening and 0 narrowing, so the gate
     is green with no label. `build` signs a bundle with both entries: `2.0.0 write_reversible` and `1.0.0
     external_effect`. A connector that calls version 2.0.0 gets `write_reversible`, and AP-01 then gives
     `not_required`: an effect with no human approval. A variant, also reproduced: `demo.wire.propose` listed twice, `high_risk` first and
     `external_effect` last, plus an `auto_approve` rule on that name. Lint gives 0 errors, so AP-02 is not
     checked. In this order the diff does show widening.
   - **Fix:** In `load_manifests`, also count names inside the same document (add each name to `seen` while
     iterating). `diff._load` already raises on `pol.errors`, so the diff then fails closed. Add one test for a
     duplicate inside one manifest, in both lint and diff.
   - **If we skip it:** Once the first manifest lands (M3), a PR can add an unapproved lower-class version of an
     approved tool, with a green gate.

2. **Raising `effect_class` is marked narrowing, but it can drop a `deny` or a quorum that matches by class** (`tools/gxg-policy/src/gxg_policy/diff.py:220-225`)
   - **What this is:** `effect_class_raised` always goes to `narrowing`. Approval rules can match by
     `match.effect_classes` (approval-policy-v1 «Поля правила»). A higher class can stop matching such a rule.
   - **Problem:** Reproduced. Base and head have the same approval policy, with one rule
     `{match: {effect_classes: [external_effect]}, decision: deny}`. The head manifest raises
     `demo.message.propose` from `external_effect` to `high_risk`. Result: lint gives 0 errors, and diff gives
     `widening: []`, `narrowing: [effect_class_raised]`. The gate is green with no label. Before, the tool was
     denied. After, it goes through with one approver (AP-01 base for `high_risk`). The same thing happens to a
     `require_approval` rule with `approvals: 2` on `external_effect`. The spec counts "removed `require_approval`/`deny`"
     as widening (approval-policy-v1 «Lint в CI», GR-10).
   - **Fix:** Count every `effect_class` change as widening: one line, `self.add(True, ...)` for both directions. It
     is rare, so the owner adds the label. Update `expected*.json` of `10-manifest-tools`.
   - **If we skip it:** A manifest edit that looks stricter turns off a tenant's class-based `deny`, with a green gate.

## Should fix

None beyond the two above. The test gap (no fixture with a class-matched rule, no duplicate inside one manifest) is
part of fixes 1 and 2.

## Ready-made solutions (ponytail ladder 2–4)

- Gate from main: `git worktree` of the already fetched ref. Alternatives: `pull_request_target` (pwn-request
  pattern, base is not main), an org ruleset required workflow (platform). Justified.
- Changed paths: `git diff --name-only`. `dorny/paths-filter` and `tj-actions/changed-files` would add a
  third-party action for one git command. Justified.
- Manifest diff: project-specific projection, no tool exists. It reuses `load_manifests` (jsonschema). Justified.
- Unique tool names: JSON Schema `uniqueItems` compares whole objects, not one key, so it cannot express this.
  A set check is the right rung, but it must also cover names inside one file (finding 1).
- Mutation testing: mutmut 3.8.1 for Python. For bash/YAML there is still no maintained tool, so `mutate_m2.py`
  is justified.

Verdict: fix 1 and 2 first.
Not checked: how the future Approval Service (M7) picks among same-name bundle entries; AS-26 says it keys by version, and the finding relies on that.
