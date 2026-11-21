# M2 cycle 5 — plan (ponytail full)

Input: critic `reports/critic/M2-cycle-4.json` (score 8.0, two major) and `.review.md`.
Spec, plan and acceptance criteria are not changed. I agree with both findings.

## Scope

| Item | Files | Rung |
| --- | --- | --- |
| Duplicate tool name inside one manifest (major 1) | `gxg_policy/lint.py` `load_manifests` | existing `duplicate-tool` check: add names to `seen` while iterating; `diff._load` already fails closed on errors |
| Tests | `tests/` lint test + diff test (exit 2 / raises) | one test each |
| Any `effect_class` change is widening (major 2) | `gxg_policy/diff.py` `_Diff.tools` | one line: `self.add(True, ...)`, change `effect_class_changed` |
| Golden reports | `tests/diff/10-manifest-tools/expected*.json` | data |
| Test with class-matched deny | diff test | one test |
| Re-run | pytest, `scripts/ci/mutmut.sh`, `mutate_m2.py`, `reports/conformance/M2.json` | evidence |

Skipped: anything else (ponytail).
