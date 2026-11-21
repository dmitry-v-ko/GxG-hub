# M2 cycle 2 — ponytail-review

Diff: `9929a67..fbd522a` (spec commit `8b616c4`, plan `ca57257`, code `e10cf06`, merge `fbd522a`). Load assumed: CI of a repo with one maintainer. Agents open PRs and act through the Claude GitHub App. There are a few PRs a day. The attacker model is the one in AC-M2-04: an agent that wants a widening merged without the owner's label.

What this change does: it makes `grants-widening` fail closed outside pull requests. Pushes and `workflow_dispatch` to a branch are diffed against the merge-base with `origin/main`. It binds the owner's label to the head commit on every event, using server time from workflow runs and force-push events, and PR runs are never cancelled. `gxg-policy diff` now expands `group:` in approval `requesters` and `approvers`. The cycle also adds a mutation script, new fixtures, `--built-at` validation, one MARKER, and a 6-hour schedule that re-signs the bundle on `main` (GR-13a).

Checked by hand (clean clone of `fbd522a`, empty `UV_CACHE_DIR`, `UV_PYTHON=3.12` → 3.12.3, `UV_LOCKED=1`, uv 0.11.32):

- `uv run pytest` → 202 passed, 136 subtests passed, 0 skipped. `scripts/ci/check-all.sh` → `check-all: OK`.
- `uv run python scripts/ci/mutate_m2.py` → `39/39 mutations killed` (4 min). The list is the same as `reports/mutations/M2-cycle-2.txt`.
- AC-M2-01: `validate policies/` → exit 0, 0 errors. `lint policies/` → exit 0, 2 warnings.
- AC-M2-02: all 19 fixtures through the CLI (`lint <case>/policies`, plus `--manifest` where the case has one) → exit 1, exactly one `error: <file>: <path>: [<code>]` each. All 12 cases from the AC are there. 19 is the new `group:ghost` subject.
- AC-M2-03: the CLI report for all 8 pairs equals `expected.json`.
- AC-M2-05: two builds in separate processes (`PYTHONHASHSEED` 1 and 7, `built_at` 2 s apart) → the bytes are equal once `built_at` is removed.
- AC-M2-06: `verify` → `ok`. One flipped bit at byte 200 → `bundle rejected: bad signature`, exit 1. `--built-at yesterday` → exit 2.
- CI run 44797618586 on `fbd522a` (event `push`): lint, types, tests, schema-guard, secrets, policy → success. `grants-widening` → failure (12 widening changes against `main`, by design). `ci-ok` → failure. `bundle` → skipped.
- Cycle-1 findings: F1 is fixed for the plain case (push and dispatch are red). F3, F4, `--built-at`, MARKER and GR-13a are fixed. F2 is fixed for the cases the critic named, but it is still open for one sequence (finding 2). F5 is done, but the guide now claims something that is false (finding 4).
- No secret material in the diff. There is no private key header, and keys come only from env or `os.urandom`.

The three bypasses below were reproduced with the repository's own test helpers. The repro files are `test_repro_tag.py` and `test_repro_ffwd.py` in the critic's scratchpad; they are not committed. Each test passes, which shows the hole.

## Must fix

1. **Change the PR base after a green run: a widening PR into `main` without the label** (`.github/workflows/ci.yml:7-8`, `scripts/ci/grants_widening.sh:7-8`)
   - **What this is:** On `pull_request` the job diffs the PR head against `github.event.pull_request.base.sha`. That is the tip of whatever base branch the PR targets at the time of the run. The workflow reacts to `opened, synchronize, reopened, labeled, unlabeled`, but not to `edited`. `edited` is the event GitHub sends when someone changes the PR's base branch.
   - **Problem:** Case: the agent pushes widening commit W to `claude/base`, then commit N (any non-policy change) on top of W to `claude/w`. It opens a PR `claude/w → claude/base`. The PR run diffs W against N, finds no widening, and goes green: `grants-widening` and `ci-ok` succeed on N. The agent then changes the PR base to `main` (GitHub App, `update_pull_request`). No new run starts, because `edited` is not in `types`. N is up to date with `main` (W branched from the main tip), and N has a green `ci-ok` from a `pull_request` run, so the merge button is available. The owner never labelled anything. There is also a push run for N on `claude/**`, which is red, so then it depends on which same-name check GitHub counts, and the PR run is the newer one. If the branch name is outside `claude/**` (a branch created through the API), there is no push run at all. No test and no checklist step covers a base change.
   - **Fix:** For `pull_request`, always diff against `main`, not against the event's base. Use `git merge-base refs/remotes/origin/main HEAD^2`, or keep `PR_BASE` only when `github.base_ref == 'main'` and otherwise use the push-mode rule with no label. Add `edited` to `types`. Add a test that runs a PR event with a non-main base and widening against main, and expects red.
   - **If we skip it:** One API call turns any widening into a mergeable PR without the owner's approval. GR-10 and AC-M2-04 are broken.

2. **Fast-forward back to a commit that already ran on the PR: an old label approves it** (`scripts/ci/widening_gate.py:126-150`, `head_arrival` + `stale`)
   - **What this is:** The label is stale if it is older than the *earliest* `pull_request` run for the head SHA on this PR, or older than the latest `head_ref_force_pushed` event.
   - **Problem:** Case: the PR head is widening commit B (run at 09:00). The agent force-pushes back to A (force-push event at 09:30). The owner reviews A and labels it at 10:00. The agent pushes B again as a normal fast-forward (A is B's parent). That makes no force-push event, only a `synchronize` run at 11:00. The earliest run for B is 09:00, before the label, and the last force-push was 09:30, also before the label. So `stale()` returns None, and the gate passes B with a label that was set on A. Repro with `FakeApi`: runs `[09:00, 11:00]`, events `[force-push 09:30, labeled 10:00]`, `run(api, "synchronize", WIDE)` → 0 and the label is kept. A related case: runs from a fork PR have an empty `pull_requests` list, so they pass the "this PR" filter. The only other check is the branch name.
   - **Fix:** Take the arrival time only from runs created after the latest force-push: `min(t for t in run_times if t > last_force_push)`. Also require `r["head_repository"]["id"] == pr["head"]["repo"]["id"]`. Add the test above, which must be red.
   - **If we skip it:** The agent can get the owner to approve a harmless commit, then put back the widening one, and merge it with a green check. That is exactly the F2 class from cycle 1.

## Should fix

3. **A tag named `origin/main` turns the push run green on widening** (`scripts/ci/grants_widening.sh:14`)
   - **What this is:** Push mode runs `git merge-base origin/main HEAD`. `actions/checkout` with `fetch-depth: 0` also fetches all tags.
   - **Problem:** Git resolves the name `origin/main` as `refs/tags/origin/main` before `refs/remotes/origin/main`. If the agent creates a tag `origin/main` that points at its own head, the merge-base is HEAD. The diff is then empty, and the run prints `no widening against the merge-base with main` and passes. Reproduced with the repo's `test_grants_widening_job` helpers: first `rc=1`, then after `git tag origin/main HEAD` `rc=0` (git warns `refname 'origin/main' is ambiguous`). A green push or dispatch run can then sit next to the red PR run on the head SHA. That breaks the guide's promise ("push-запуск … красные"), and if GitHub counts the newest same-name check, it is a bypass (a dispatch after the PR run).
   - **Fix:** Use the full ref: `git merge-base refs/remotes/origin/main HEAD`, the same in finding 1. Add a test with the tag.
   - **If we skip it:** The F1 fix can be undone by creating one ref.

4. **The guide says "no green `ci-ok` without the label", but cases 1–3 make one; the checklist does not test them** (`docs/setup/widening-approval.md` «Как работает проверка», «Сквозная проверка»)
   - **What this is:** The owner's 7 steps are the only end-to-end proof of AC-M2-04.
   - **Problem:** The guide says "на head SHA агентского PR с расширением нет зелёного `ci-ok` без метки". Findings 1–3 show three ways to get one. No step changes the base, fast-forwards back to an older SHA, or creates a tag. Step 4 checks only one order of the two same-name `ci-ok` checks (push red, PR green). It never checks the reverse order: a newer green check next to an older red one.
   - **Fix:** After 1–3, add these steps with the exact expected result: (a) open the PR into another branch, then change the base to `main` → red; (b) B, then force-push to A, label, then fast-forward to B → red, label removed; (c) push the tag `origin/main` and run `workflow_dispatch` → red. Also record which `ci-ok` GitHub counts when the newer one is green.
   - **If we skip it:** The owner passes the checklist and believes the gate holds, while three bypasses stay open.

5. **A home-made mutation script where mutmut is now required** (`scripts/ci/mutate_m2.py`, `reports/mutations/M2-cycle-2.txt`)
   - **What this is:** DoD-4 proves that the tests catch broken behavior. The owner changed it on 2026-11-20 (plan on `claude/admiring-planck-vzys0x`, commit `892d570`): Python is checked with **mutmut**, a tool that makes many small code changes ("mutants") on its own, and home-made mutation scripts are not used.
   - **Problem:** `mutate_m2.py` is a hand-picked list of 39 edits. 35 of them are in Python, where mutmut applies. A hand list only checks what its author thought of. Finding 2 is such a case: `stale()` misses a fast-forward to an older SHA, and no mutation or test covers it. The 39/39 result is real (re-run), but it does not meet the new DoD-4. The rule is newer than the code, and the score is already capped by findings 1–2.
   - **Fix:** Configure mutmut in `pyproject.toml` for `gxg_policy/{diff,lint,bundle,load}.py` and `scripts/ci/widening_gate.py`, run it with one command, and attach the report with no surviving non-equivalent mutants. Delete the Python part of `mutate_m2.py`. Keep the custom check only for `grants_widening.sh` and `ci.yml`, since no mature mutation tool exists for bash or workflow YAML, and write that reason in the plan.
   - **If we skip it:** DoD-4 stays open, and gaps like finding 2 stay invisible.

## Existing solutions (ponytail «is there a ready solution»)

| Component | Ready alternative | Verdict |
| --- | --- | --- |
| `validate` (JSON Schema) | `jsonschema`, already used | use existing |
| `lint` cross-file rules | conftest/OPA, yamllint: frameworks only, every rule would still be written (in Rego, plus a new binary) | justified custom |
| YAML duplicate keys | yamllint `key-duplicates`, ruamel.yaml: a few lines on PyYAML keep the error in the same report with file and path | justified custom |
| `diff` widening/narrowing | dyff, deepdiff: structure only, no permission pairs | justified custom |
| JCS (`bundle.py`, ~20 lines) | `rfc8785` on PyPI. The cycle-1 plan records "stdlib json + UTF-16 key sort, no floats". I found no input where the output differs | justified custom |
| Ed25519 | `cryptography`, used | use existing |
| `.sig` format | minisign, cosign: the format is fixed by GR-11a for the M1 loader | justified custom |
| Widening gate (label, CODEOWNERS, staleness) | Code Owner review (excluded by the owner, AC-M0-03); environment with required reviewers (needs a paid plan for private repos; AC-M2-04 prescribes the label) | justified custom |
| GitHub API client (`urllib`) | `gh api --paginate`, PyGithub: stdlib keeps the gate testable with a fake API | justified custom |
| Mutation testing | **mutmut** (new DoD-4) for Python; bash and workflow YAML have no mature tool | use existing (finding 5) |
| Cron gap check (test) | croniter: test-only, about 10 lines | justified custom |

## Nice to have

None worth a line.

Verdict: fix 1, 2 and 3 first, then 4 and 5.
Not checked: mutmut itself was not run; which of two same-name `ci-ok` checks on one SHA GitHub branch protection counts (needs a real PR); that `actions/checkout` fetches tags was taken from its documented `fetch-depth: 0` behavior, not seen in this repo's CI log.
