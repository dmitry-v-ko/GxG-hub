# M8 cycle 3 — ponytail-review

Diff: `f348c6a` (cycle-2 critic report) .. `ddf8c9f` (head of `claude/m8-executor`), 20 files, +191 −83.
Load assumed: dev stand and CI; the executor runs as several processes on one PostgreSQL (AC-M8-02: 10 executors,
50 intents). The fake provider is a test double in one process with a SQLite journal.

What this change does: the demo connector now sends the `intent_id` to the provider as its idempotency key
(EFF-18 p.5), and it also uses that key for reconcile. Before, it sent the agent's `idempotency_key`. The provider
journal moves to a new table, `sends`, keyed by `provider_key`. The cycle also adds an `exec-keys/` line to
`.gitignore` with a `git check-ignore` test, moves the 0003 row of the SQL driver to the full DB suite, adds a
test for one principal in two tenants, a WARNING when no signing key is active, a test that the propose enum
equals the intent-status enum, two `.gitleaksignore` lines and the backlog rows.

Checked: every caller of `Provider.send`, `lookup` and `calls` (`effects.py`, `testctl.py`, `effects_stack.py`,
the demo and DB tests). Nothing else calls them: `grep idempotency_key stand connectors/demo/src`. With
`provider_key=claims["idk"]` put back by hand, 4 DB tests fail, including the new two-requesters test (AC-01,
AC-08 ×2).

## Must fix

1. **The mutation gate is red: one equivalent-mutant id is stale** (`scripts/ci/mutmut-equivalent.txt:133`, cause `approval/src/gxg_approval/executor.py:138`)
   - **What this is:** `scripts/ci/mutmut.sh` (a step of `check-all.sh` and of the CI job `mutmut`) fails when a
     mutant survives and is not in the list of equivalent mutants. It also fails when a listed id is killed now.
     An equivalent mutant is a change that cannot alter behaviour.
   - **Problem:** commit `45017be` adds one `log.warning` line to `Executor.run_once`. mutmut numbers mutants by
     their position, so the ids in `take()` moved. The list still holds `take__mutmut_29`. The surviving mutant is
     now `take__mutmut_36`, the same `cast(None, …)` no-op. `ddf8c9f` renumbered only the demo ids. This critic's
     run (`mutmut run 'gxg_approval.executor*' 'gxg_approval.keys*' 'gxg_approval.intents.x_propose*'`): 13
     survived; 12 are listed, `take__mutmut_36` is not, and `take__mutmut_29` is stale. So `mutmut.sh` exits 1
     and `check-all.sh` is red.
   - **Fix:** rename the id on line 133 to `take__mutmut_36`. Then run `check-all.sh` to the end.
   - **If we skip it:** DoD-1 (green `check-all` on the reviewed commit) cannot be met, and the CI job `mutmut`
     is red before the merge to `main`.

## Nice to have

2. **Reconcile misses sends journalled before the upgrade** (`connectors/demo/src/demo_connector/provider.py:29`, `:117-124`)
   - **What this is:** the new code opens a new table, `sends`, and leaves the old `calls` table as it is, so an
     old volume still starts (`test_provider_opens_a_journal_of_the_old_layout`).
   - **Problem:** `lookup()` reads only `sends`. An intent that ended `outcome_unknown` on the old code was
     journalled in `calls`. After the upgrade, reconcile answers `not_executed` for it, although the provider did
     execute it.
   - **Fix:** none needed for a fake provider. If one is wanted, write one line in the README: «after the cycle-3
     upgrade, drop the demo provider volume or settle open `outcome_unknown` first».
   - **If we skip it:** only a stand volume that holds an `outcome_unknown` from cycle 2 is affected. Its
     reconcile result is wrong.

3. **The `git check-ignore` test needs a Git checkout** (`approval/tests/test_deploy.py:93-97`)
   - **What this is:** the test runs `git -C REPO check-ignore` for the seeds path and the secrets path.
   - **Problem:** in a source tarball or a Docker build context without `.git`, `git` exits 128 and the test
     fails with a message that does not say why.
   - **Fix:** none needed now: CI and `check-all` always run in a clone. If needed, skip it when `REPO/.git` is
     missing.
   - **If we skip it:** someone runs the tests from an export and sees a red test that does not mean a leak.

4. **The `.gitleaksignore` lines fit only one clone depth** (`.gitleaksignore:33-36`)
   - **What this is:** two fingerprints for the fixture constant in base commits `c10fd9b` and `8128dee`.
   - **Problem:** in a shallow clone, gitleaks reports the commits at the shallow boundary as if they added the
     file. This critic's clone (203 commits, shallow) has other boundary commits (`bcff838`, `2869731`, `77d04f6`,
     `77662af`), so `gitleaks.sh range 0000..HEAD` finds 4 leaks and `tests/secrets` is red. In a full clone
     (327 commits) both scans are clean. The author already wrote this into `docs/backlog.md` (M8 cycle-2 #8).
   - **Fix:** the backlog fix: `check-all.sh` runs `git fetch --unshallow`, or refuses a shallow repository,
     before `range`. Then the two lines can go away.
   - **If we skip it:** every new shallow clone of a critic or CI job with a small depth is red for a reason that
     is not a leak.

## Not findings (checked)

- Provider dedupe and lookup no longer filter by `tenant`. Correct: `intent_id` is a UUID that the Approval
  Service issues per intent. Reconcile takes `intent_id` from the executor's request, which is signed with a
  service JWT. The `tenant` column stays in the journal for the tests.
- `RECONCILE_BODY` still requires `idempotency_key`. That is the request contract of approval-service-v1, not
  the provider key. Leave it.
- The 0003 SQL row is now in `MUTATIONS` (all of `tests_db`). It is killed, and so is «idempotency key shared
  across tenants» (driver run by this critic, see the JSON).

Verdict: fix 1 first. Also missing: the evidence in `reports/` (`M8.json` with `ci_local`, `reports/mutmut/M8.txt`), see the critic JSON.
Lean: 0 lines.
Not checked: an upgrade of a real stand volume from cycle 2 with an open `outcome_unknown` (finding 2). Read from
the code only.
