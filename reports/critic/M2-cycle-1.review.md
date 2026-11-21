# M2 cycle 1 — ponytail-review

Diff: `6b3fe48..63d7c18` (spec commit `8128dee`, plan `d4300c9`, code `63d7c18`). Load assumed: CI of a repo with one maintainer, PRs opened by agents from `claude/**` branches, a few PRs a day.

What this change does: it adds the `gxg-policy` CLI (`validate`, `lint`, `diff`, `build`, `verify`) for `policies/`, with 18 bad fixtures, 6 diff fixture pairs and bundle tests. CI gets a `policy` job (validate, lint, two builds with a throw-away key, verify), a `grants-widening` job that gates widening PRs on a `widening-approved` label from a code owner, and a `bundle` job that signs the bundle on `main`. Two setup guides explain the label and the signing key.

Checked by hand (clean clone of 63d7c18, empty `UV_CACHE_DIR`, `UV_PYTHON=3.12` → 3.12.3, `UV_LOCKED=1`):

- `uv sync && uv run pytest` → 165 passed, 136 subtests passed, 0 skipped. `scripts/ci/check-all.sh` → `check-all: OK`.
- CI run 44792404466 on 63d7c18 (event `push`): lint, types, tests, schema-guard, secrets, policy, grants-widening, ci-ok — success; bundle — skipped. In `grants-widening` the gate step was skipped and the step "Not a pull request" passed.
- AC-M2-01: `validate policies/` → exit 0, 0 errors; `lint policies/` → exit 0, 2 warnings.
- AC-M2-02: each of the 18 fixtures through the CLI → exit 1, stderr `error: <file>: <path>: [<code>] …`; all 12 cases from the AC are there.
- AC-M2-03: the CLI report for all 6 pairs equals `expected.json`.
- AC-M2-05: two builds in separate processes (different time, `PYTHONHASHSEED`) → equal bytes once `built_at` is replaced.
- AC-M2-06: `verify` accepts the build; one flipped bit → `bundle rejected: bad signature`, exit 1.
- 41 single-condition mutations (`lint`, `load`, `diff`, `bundle`, `verify`, gate): 36 make the tests red, 5 do not (list in finding 4).
- No secret material: the key is only in env (`GXG_BUNDLE_SIGNING_KEY`). Tests and the smoke run make keys with `os.urandom`. The artifact holds only the bundle and `.sig`. The tool prints no key, and its error texts do not quote the seed. There is no private key header in the diff.
- GR-11a format: keys `schema_version`, `bundle_version`, `built_at`, `tenants{t: {grants, approval_policy}}`, `platform`. JCS bytes. `.sig` = `{"alg":"EdDSA","kid","sig"}` (base64url without padding). The public key comes from the CLI, not from the bundle. This matches the spec. The kept `groups` map is not a finding: the file is still a valid `resource-grants` document, no grant subject contains `group:` any more (so a loader that expands groups again gets the same result), and the approval policies need the groups for `requesters`/`approvers`. `built_at` from `build` without the flag is `YYYY-MM-DDTHH:MM:SSZ`, which is RFC 3339 UTC. Only the manual `--built-at` flag is not checked (finding 6).

## Must fix

1. **A green `grants-widening` and `ci-ok` exist on the PR head without any label** (`.github/workflows/ci.yml:6-11`, job `grants-widening`, step "Not a pull request")
   - **What this is:** The workflow runs on `pull_request` and also on `push` to `claude/**` and on `workflow_dispatch`. The `grants-widening` job decides only for `pull_request`. On every other event it prints "applies to pull requests only" and passes.
   - **Problem:** Agents open PRs from `claude/**` branches. Every push to such a branch makes a second run on the same head SHA, and there `grants-widening` and `ci-ok` are green, whatever the policy diff says. Run 44792404466 on 63d7c18 is that case. The agent can also start a fresh green run any time with `workflow_dispatch`. Branch protection picks a check by name (`ci-ok`) and source (GitHub Actions). It does not know which event made the run. So a PR with widening has a green `ci-ok` on its head without any label. The AC says the check `grants-widening` is red until the label is there. That is not true for any agent PR.
   - **Fix:** Outside `pull_request`, fail closed. On `push` and `workflow_dispatch` to a branch other than `main`, run `gxg-policy diff` of `origin/main` (merge-base) against HEAD and exit 1 if there is any widening. There is no PR and no label, so there can be no approval. Add a test for that branch of the script. The alternative, dropping `push: claude/**` and `workflow_dispatch`, also works, but it loses CI on branches without a PR.
   - **If we skip it:** A widening PR merges with only the push-run check, and the owner never approves it. GR-10 is broken.

2. **The approval survives a new push if the `synchronize` run is cancelled** (`scripts/ci/widening_gate.py:121-124`, `ci.yml` `concurrency`)
   - **What this is:** After a new push the gate removes the label, so the approval covers only what the owner saw. It does this only when `event.action == "synchronize"`.
   - **Problem:** The concurrency group is `ci-${{ github.ref }}` with `cancel-in-progress: true`, and `pull_request` also fires on `labeled`, `unlabeled` and `reopened`. Case: the owner labels commit A. The agent pushes B with more widening, and within a few seconds adds any other label (or closes and reopens the PR). The new run cancels the `synchronize` run before its gate step (that run first spends time on `uv sync`). The new run has `action = labeled` or `reopened`, so it does not remove the label. The owner's label is still on the PR, so the check passes on B. The guide says "Подтверждение относится к тому, что владелец видел", and in this case that is false. No test covers it: all tests use `action = synchronize`.
   - **Fix:** Check on every run whether the label is older than the head commit, not only on `synchronize`. One way: take the earliest `created_at` of the `pull_request` workflow runs for `head_sha` from the server (`GET /actions/runs?head_sha=…`; git commit dates can be faked, these times cannot). If the `labeled` event is older, remove the label and fail. Also set `cancel-in-progress: false` for `pull_request` runs. Add tests: a run with `action = labeled` (another label) and `reopened` after a push that was not processed → red.
   - **If we skip it:** An agent turns an approved small widening into an unapproved big one with two API calls.

3. **Widening through group membership in approval rules is not reported** (`tools/gxg-policy/src/gxg_policy/diff.py:107-160`)
   - **What this is:** `diff` expands groups only for grant subjects. Approval rules are compared by `match` and `approvers` as written, with `group:<name>` left as is.
   - **Problem:** Reproduced on a copy of `policies/`. The `auto_approve` rule `tg-partner-auto` has `requesters: [group:actors]`. The PR adds `agent:t-alpha:hermes` to `actors`, and the `crm-two-person` rule gets `approvers: [group:fin]` with one more human added to `fin`. `gxg-policy diff` → `{"widening": [], "narrowing": []}`. Hermes's intents are now approved without a human, and the circle of approvers is wider. The gate stays green with no label. The approval-policy spec («Lint в CI») and AC-M2-03 count a new `auto_approve` as widening, and the tool itself counts `approvers_added` written directly as widening. Through a group it does not.
   - **Fix:** Before comparing, expand `group:` in `match.requesters` and `approvers` with the groups of the tenant's grants file, on both sides. Compare the expanded sets: new requesters of an `auto_approve` rule and new approvers are widening. About 10 lines, plus one diff fixture pair with a golden report.
   - **If we skip it:** A one-line YAML change widens auto-approval and nobody has to approve it.

## Should fix

4. **Tests do not catch mutations of key widening rules; the "17/17" claim cannot be reproduced** (`tools/gxg-policy/tests/test_diff.py`, `test_bundle.py`, `test_lint.py`)
   - **What this is:** DoD-4 requires that breaking the checked behaviour turns a test red. The report says 17/17 mutations were killed, but the script was not committed.
   - **Problem:** These mutations keep all 165 tests green:
     - `diff.py:136` `match_changed` reported as narrowing: an `auto_approve` rule whose `resources` change from one chat to `chat/*` passes with no label;
     - `diff.py:153` `approvers_added` reported as narrowing;
     - `lint.py:114` dangling `group:<name>` in grant subjects: no fixture has one (with the mutation lint crashes with a `KeyError`, it does not quietly pass);
     - `bundle.py:114` shape check in `verify` removed: no test signs a wrong-shaped bundle;
     - `bundle.py:105` unknown `kid` check removed: the CLI test still sees exit 1, but only because of a traceback. That one is harmless.
   - **Fix:** One diff fixture pair with a widened `match` of an `auto_approve` rule and a direct `approvers` change; one bad fixture `grants[0].subjects: [group:ghost]`; one test that `verify` rejects a validly signed `{"schema_version": 2, …}`. Commit the mutation script (the list from this review is a ready start), so the next cycle can run it again.
   - **If we skip it:** The next edit of `diff.py` can turn a widening rule into narrowing, and CI stays green.

5. **The setup guide promises more than the gate does** (`docs/setup/widening-approval.md`, sections «Как работает проверка» and «Ограничения»)
   - **What this is:** The page the owner reads to know how far to trust the gate.
   - **Problem:** It names only `ci.yml` and `widening_gate.py` as files whose change disables the check. The job also runs the PR's own `tools/gxg-policy/src/**` (`diff.py` decides what is widening), `pyproject.toml`/`uv.lock` (`uv sync` installs what the PR says), and `schemas/`. The guide says nothing about the push-run check (finding 1) or the cancelled run (finding 2). It says the approval "относится к тому, что владелец видел" without saying when that is not true.
   - **Fix:** List every path the job executes. After findings 1–2 are fixed, describe what is left and give the owner a short end-to-end check (see below).
   - **If we skip it:** The owner trusts the green check more than they should.

## Nice to have

6. **`build --built-at` accepts any text and signs it** (`tools/gxg-policy/src/gxg_policy/cli.py:94`)
   - **What this is:** GR-11a: `built_at` is RFC 3339 UTC. The M1 loader uses it for the 24 h rule (AC-M1-07).
   - **Problem:** `build … --built-at yesterday` writes and signs `"built_at": "yesterday"` (checked). CI does not pass the flag, so `main` is not affected. It is only a manual-use trap.
   - **Fix:** `parse_ts(a.built_at)` plus a check for the `Z` suffix, exit 2 otherwise. Two lines.
   - **If we skip it:** A hand-built bundle with a bad `built_at` is rejected by the loader later, far from the cause.

7. **The comment marker is defined twice** (`diff.py:198` and `widening_gate.py:32`)
   - **What this is:** The gate finds its own comment by `MARKER`, and `markdown()` writes it.
   - **Problem:** Two constants that must stay equal. If one changes, the gate posts a new comment on every run instead of updating its own.
   - **Fix:** One constant: the gate already reads the Markdown file, so `MARKER` can be imported from `gxg_policy.diff`, or the gate can look for the first line of the file.
   - **If we skip it:** A wall of duplicate comments after a harmless rename.

## What the owner must check on a real PR (AC-M2-04)

After findings 1–3 are fixed, a throw-away PR from a `claude/**` branch that adds one grant pair:

1. The PR gets a "Policy diff (gxg-policy)" comment with one widening line. Every `grants-widening` and `ci-ok` check on the head SHA (the PR run, the push run, a manual `workflow_dispatch`) is red. `gh pr merge` refuses.
2. The agent adds `widening-approved` through the Claude GitHub App or MCP. The check stays red with "added via the GitHub App". This confirms that `performed_via_github_app` is filled for the user-to-server token that agents use. Until this is checked, the app check is unproven.
3. The owner adds the label by hand → green → merge is allowed.
4. Push one more widening commit, and right after it add any other label → red, and the label is removed.
5. A PR that changes `scripts/ci/widening_gate.py` to `return 0` → green. This is the known limit: make sure it is written down and accepted.

Verdict: fix 1, 2, 3 and 4 first.
Lean: -10 lines possible (marker constant; diff `_pairs` expiry merge can be `max` over parsed values in one expression).
Not checked: how GitHub picks between two same-name check runs on one SHA for a required check (needs a real PR; finding 1 stands either way, because a green `grants-widening` check on the PR head contradicts the AC); whether `performed_via_github_app` is set for user-to-server tokens.

## Addendum after the review (spec change during the cycle)

The spec in `claude/admiring-planck-vzys0x@7466dfa` now has **GR-13a**: CI re-signs the bundle on a schedule, at least every 12 h. In 63d7c18 job `bundle` runs only on a push to `main`. With no policy change for a day, the last bundle becomes older than 24 h, and every service goes not ready (GR-13). This does not lower this cycle's score, because the requirement came after the cycle's spec. It is in `required_improvements` for the next cycle: add `schedule` to the workflow, let `bundle` run on it on `main`, and bring the spec text in unchanged.

The M1 loader accepts both expanded subjects and raw `group:<id>`. This confirms that keeping the `groups` map is not a compatibility issue.
