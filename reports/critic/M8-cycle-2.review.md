# M8 cycle 2 — ponytail-review (critic, cycle 2)

Diff: `b98ff3d` (cycle-1 critic report) `..d487b4a`, code part: `approval/` (cli, executor, intents, keys,
migration `0003`, deploy compose, tests), `connectors/demo/` (propose output schema, effects), `scripts/ci/`
(SQL driver rows, mutmut equivalents), `docs/` (owner decisions from `643ea3e`, backlog). Read with the code it
touches: `Executor.take/execute`, `SigningKeys`, `config.JsonFile`, `demo_connector.provider.Provider.send/lookup`,
`Effects.execute/_checked`, `intents.propose`, `0001_core.py` (`intents`, `gxg_release_reserved`), the README and
`approval/deploy/compose.yaml`, `.gitignore`.

Load assumed: the reference deployment (one Approval Service, a few executor replicas, several agents per tenant —
Signals and other agents in the same tenant, EFF-08 and service-integration-v1 «ExecuteAction»).

What this change does: it closes the three major findings of cycle 1 (propose output accepts every intent status;
no signing key → the executor waits instead of burning attempts; the executor key file comes from a directory
mount, with `keys release` as a separate command), makes idempotency per requester (migration `0003`, owner
decision), sends the provider the canonical args, and adds tests for each.

## Must fix

1. **Two requesters with one key: the second approved message is never sent, yet reported `succeeded`**
   (`approval/src/gxg_approval/migrations/versions/0003_idempotency_per_requester.py:16`,
   `approval/src/gxg_approval/executor.py:194`, `connectors/demo/src/demo_connector/effects.py:226`,
   `connectors/demo/src/demo_connector/provider.py:97-103`)
   - **What this is:** Since `0003` each requester has its own idempotency key space (EFF-08): agent A and agent B
     of one tenant may both use key `K` and get two intents. The executor still puts the bare key into the token
     (`idk`), and the connector passes it to the provider as the provider idempotency key (EFF-18 п.5). The
     provider dedupes per `(tenant, key)`.
   - **Problem:** B's intent reaches the provider with the same `(tenant, K)` as A's. The provider answers with A's
     receipt (journal outcome `duplicate`), the connector returns `succeeded`, and B's intent ends `succeeded`
     with A's `provider_operation_id`. B's approved message (other recipient, other text) is never sent.
     Reproduced on the real stack (`test_executor.py` stack, two requesters of `t-alpha`, key `shared-key-1`,
     texts differ, recipients `partner_1`/`partner_2`): both intents `succeeded`; provider journal
     `[(A, partner_1, ok), (B, partner_2, duplicate)]`; both executions carry the same operation id. An agent
     that knows Signals' key format (`signals:<candidate>:v<n>`, service-integration-v1) can suppress Signals'
     approved effect this way, which is the exact squatting path the owner's EFF-08 decision was meant to close.
     Reconcile by `idempotency_key` (EFF-25, as a real connector may do) would mark B `reconciled_succeeded` from
     A's operation.
   - **Fix:** make the provider idempotency key unique per `(tenant, requester, key)`: the connector derives it from
     the token, e.g. `provider_key = f"{claims['req']}/{claims['idk']}"` (or a SHA-256 of both), and a one-line note
     in EFF-18 п.5. Test: the experiment above → two provider effects, two different operation ids; retries of one
     intent (EFF-23) still share one provider key.
   - **If we skip it:** an approved, human-checked effect is silently dropped and the audit says it happened.

2. **Executor signing seeds now live in a directory Git does not ignore** (`approval/deploy/compose.yaml:86`,
   `approval/README.md:39`, `.gitignore:11`)
   - **What this is:** The seeds moved from the Docker secret `deploy/secrets/exec_signing_keys` (ignored by the
     `secrets/` rule) to `approval/deploy/exec-keys/signing-keys.json`, which the README tells the operator to
     write on the host with `keys rotate`.
   - **Problem:** `git check-ignore approval/deploy/exec-keys/signing-keys.json` → not ignored. In a checkout of
     the repository the file shows up as untracked, and `git add -A` commits the private seed. The pinned gitleaks
     with both repository configs does not catch it: a probe repo with a synthetic
     `[{"seed": <43 base64url chars>, "not_before": …}]` at that path → «no leaks found» (tree scan). SEC-17: keys
     never in Git.
   - **Fix:** add `exec-keys/` to `.gitignore` (one line, next to `secrets/`) and a `test_deploy.py` assertion that
     the executor key directory is ignored (`git check-ignore`).
   - **If we skip it:** the first careless commit on the deploy host publishes the key that signs every execution.

## Should fix

3. **Cycle-2 evidence is not in the repository** (`reports/conformance/M8.json`, `reports/mutmut/M8.txt`)
   - **What this is:** DoD-1 (owner decision 2026-11-21: `ci_local` with commit, command and result from the
     executor and the critic), DoD-3, and cycle-1 checklist item 7 (counts and survivor ids per package, full SQL
     driver output).
   - **Problem:** at `d487b4a` `M8.json` is still the cycle-1 report (`commit: a01c564`, `cycle: 1`), has no
     `ci_local`, and `M8.txt` is the cycle-1 prose without counts. The plan (`reports/plans/M8-cycle-2.md`, N2,
     item 7) promises both.
   - **Fix:** commit the cycle-2 `check-all` summary as `ci_local`, the mutmut counts and survivor ids, and the
     driver output.
   - **If we skip it:** the DoD of the milestone cannot be checked from the repository.

4. **The SQL row for `0003` runs only the executor tests** (`scripts/ci/mutate_m7_sql.py:182-183`)
   - **What this is:** `M8_MUTATIONS` rows run `M8_TESTS` (`test_executor.py`, `test_roles.py`); the new row
     «idempotency unique per tenant, not per requester» is in that list.
   - **Problem:** the tests that pin the constraint (`test_same_key_of_two_requesters_is_two_intents`,
     `test_the_database_enforces_one_key_per_requester`) are in `test_propose.py`, outside `M8_TESTS`.
     `mutate_m7_sql.py 'not per requester'` → `SURVIVED`, «1 not as expected», exit 1. The same mutant by hand
     makes both `test_propose.py` tests fail, so the guard is tested; only the row is wired to the wrong files.
   - **Fix:** move the row to `MUTATIONS` (full `tests_db`) or add `test_propose.py` to `M8_TESTS`.
   - **If we skip it:** `scripts/ci/mutmut.sh` (the last step of `check-all`) reports the row as a survivor or does
     not measure the guard it claims to.

10. **The tenant condition of the new key lookup is not tested any more** (`approval/src/gxg_approval/intents.py:56-58`,
    `approval/tests_db/test_propose.py:149-161`)
    - **What this is:** `_SQL_BY_KEY` now filters by tenant, requester and key.
    - **Problem:** the full SQL driver run shows `SURVIVED intents.py: idempotency key shared across tenants`
      (`tenant_id = %s OR true`). The only cross-tenant test uses two different principals, so the requester
      condition alone keeps them apart. A principal ID can be registered in two tenants (fixture `agent:shared`,
      AP-05 test). Under the mutant its propose in `t-beta` with the same key would return the `t-alpha` intent.
      The code is right; the test no longer proves it.
    - **Fix:** one test: `agent:shared`, one key, `t-alpha` and `t-beta` → two 201 with different intent ids.
    - **If we skip it:** a later edit can drop the tenant condition and leak intents across tenants unnoticed.

5. **Ten backlog items were deleted with the docs import** (`docs/backlog.md`, commit `67d61b6`)
   - **What this is:** `docs/backlog.md` was copied from `643ea3e`, which never had the integration rows.
   - **Problem:** M5 F11, F12, F13, F6 and six M7 items (probes of sweeper/mcp, request body limit, OpenAPI 422,
     non-JSON api answer in mcp, …) disappeared without being done or moved. The new M8 row #8 even refers to «как
     у `sweeper`, `mcp`», whose row is gone.
   - **Fix:** restore the ten rows from `3c2f0b0:docs/backlog.md`.
   - **If we skip it:** known defects are forgotten.

## Nice to have

6. **The propose status list is a copy with no tie to its source**
   (`connectors/demo/schemas/message-propose.output.json:16-29`)
   - **What this is:** the 11 statuses are copied from `schemas/intent-status.schema.json`.
   - **Problem:** a 12th status there breaks idempotent repeats again (cycle-1 finding 1) and no test notices.
   - **Fix:** one assertion in `connectors/demo/tests/test_effects.py`: the enum equals the kit's
     `intent-status.schema.json` enum.
   - **If we skip it:** the same bug comes back with the next status.

7. **A missing signing key stalls intents silently** (`approval/src/gxg_approval/executor.py:137-139`)
   - **What this is:** with no active key the row is deferred (correct, AS-10).
   - **Problem:** nothing is logged or counted. After removing the only key, intents wait up to the execution
     window (≤ 1 h) and expire with no signal to the operator (the readyz item is in the backlog, but there is
     no log line now).
   - **Fix:** `log.warning` once per deferral (or a counter label), as the kill-switch path does through
     `RESERVE_CONFLICTS`.
   - **If we skip it:** an expired batch of approved intents is found only by people asking where their messages
     went.

## Older problems seen on the way (not from this diff, `missed_earlier`, backlog)

8. **`check-all` cannot be green in a deep clone** (`.gitleaksignore`): `gitleaks.sh range 0000..HEAD` and
   `tests/secrets::test_this_repository_is_clean` report `generic-api-key` on `tests/ci/test_check_secrets.py:23`
   (a test fixture) in base commits `c10fd9b` and `8128dee`. Two fingerprint lines in `.gitleaksignore` fix it.
9. **A stuck ContextForge session hangs the stand forever** (`stand/tests/standlib.py:153`): `Stand.rpc` has no
   timeout; with ContextForge logging `ForeignKeyViolation … fk_mcp_messages_session_id` in its session cleanup,
   `test_sc03` and later `test_sc01` waited > 40 min. A timeout turns this into a failure instead of a hang.

## Checked and fine

- Cycle-1 #1: the output enum equals `intent-status.schema.json`; the real MCP client test
  (`test_repeat_propose_after_success_returns_the_intent_as_it_is_now`) passes.
- Cycle-1 #2: `take()` checks `SigningKeys.active()` before `gxg_reserve`; deferred row, `attempts` 0, no
  executions, executed when a key becomes active (test passes).
- Cycle-1 #3: directory mount; Docker experiment: `-v dir:/run/gxg/exec-keys:ro`, `os.replace` on the host →
  the container reads the new file at once (single-file mount kept the old one in cycle 1). `keys release` shares
  `release_reserved()` with `Executor`; CLI test against PostgreSQL passes.
- Cycle-1 #4 (0600 from creation), #5 (client key `(id, internal_url)`), #6 (deterministic `CONTINUE WHEN` test),
  #7 minor (NFC args sent): done, each with a test.
- Migration `0003`: drops the auto-named `intents_tenant_id_idempotency_key_key`, adds the per-requester unique
  constraint; `_SQL_BY_KEY` uses the three columns; T1 lock per tenant unchanged.
- T2 window ≠ 3600: `test_t2_deadline_is_the_window_frozen_at_t1` (600 s window, later 20 min policy ignored).

Verdict: fix 1 and 2 first, then 3, 4 and 10.
Lean: -0 lines possible.
Not checked: the user/permissions of the executor image on a real host (no Dockerfile in the repository for the
approval image): whether a file written by the host operator with 0600 is readable by the container user.
