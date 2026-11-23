# M10 cycle 1: ponytail-review

Diff: `40cec84..b3ab372` (branch `claude/m10-ops`). Reviewer: independent critic, cycle 1. I read the connected code, not only the diff: migrations 0001/0004 (grants, `_gxg_blocked`, executions states), `executor.py`, kit `grants.py` (BundleStore), ui `app.py`, notifier loop.

Load assumed: the repo's own deployment, one instance of each process (compose, `approval/deploy/compose.yaml`), a small team of approvers, tens to hundreds of intents a day, a few webhook subscribers (Signals, M12).

Re-runs by the critic on `b3ab372`:
- `uv run pytest approval/tests_db approval/ui/tests_db approval/notifier/tests_db -q -p no:cacheprovider -m "not e2e"`: 517 passed, 2 deselected (e2e), 118 s.
- `uv run pytest approval/tests packages/gxg-connector-kit/tests connectors/demo/tests approval/ui/tests -q -p no:cacheprovider`: 681 passed.
- `uv run pytest approval/deploy/tests/test_alert_rules.py approval/ui/tests_db/test_e2e.py -q`: 5 passed (promtool check, promtool test, Prometheus loads the rules, Playwright e2e).
- `uv run pytest approval/deploy/tests/test_network_policy.py -q` (with `DOCKER_BUILD_FLAGS`): 6 passed.
- An extra promtool unit test of my own (scratchpad, not committed) for finding 1: it fails, so the bug is real.

## What this change does

It adds a new internal process, `decider`, that checks a passkey signature (WebAuthn) on every human decision and is the only one allowed to record it. The ui now only forwards the browser's signature. It also adds signed webhooks from the outbox, a payload purge, Prometheus metrics and alert rules, an admin API with a kill switch and passkey invites, and a runbook.

## Must fix

1. **The "new outcome_unknown" alert fires when an outcome_unknown is resolved** (`approval/src/gxg_approval/migrations/versions/0005_ops.py:299-302`, `approval/deploy/prometheus/rules.yaml:14-18`)
   - **What this is:** `gxg_executions_total{outcome}` is published as a Prometheus counter. Its value is `count(*)` of the `executions` rows in each `state`. The alert `GxgOutcomeUnknownGrowing` is `increase(...{outcome="outcome_unknown"}[1h]) > 0`.
   - **Problem:** A counter must never go down. But reconcile (T15/T16, T18) moves an execution from `outcome_unknown` to `reconciled_*` (0001 lines 343, 428), so the count drops. Prometheus reads a drop as a counter reset, so `increase()` reports a rise. I checked this with promtool: the series `2x30 1x30` (one execution resolved, none new) fires the alert at 40 min. The reverse also happens: 2 new and 2 resolved between two scrapes give no change, so there is no alert.
   - **Fix:** Make the series really monotonic. Count every execution that ever reached `outcome_unknown` (`state IN ('outcome_unknown','reconciled_executed','reconciled_not_executed')`) under `outcome="outcome_unknown"`. Add a promtool case where one is resolved and no alert fires.
   - **If we skip it:** Every successful reconcile pages the operator. When unknowns arrive and are resolved at the same rate, the alert stays silent.

2. **One lost HTTP response blocks a webhook subscriber for 24 hours** (`packages/gxg-connector-kit/src/gxg_connector_kit/webhooks.py:51-53`, `approval/notifier/src/gxg_approval_notifier/webhooks.py:104-112`, `approval/notifier/tests_db/test_webhooks.py:50-52`)
   - **What this is:** The notifier sends one event at a time per subscriber (head of line: the next event waits until this one is delivered or dead). The kit's `verify` raises `replay` for an `event_id` it has already seen. The test receiver, which is the model for Signals, answers 401 to any refusal.
   - **Problem:** The receiver handles an event, but its 2xx answer is lost (for example, the 10 s timeout runs out). The notifier then sends the same `event_id` again. The receiver says `replay` → 401. The notifier counts that as a failure and retries with backoff for 24 hours. All later events for that subscriber wait behind it, then the event goes `dead` and an alert fires, although it was delivered.
   - **Fix:** A valid signature on an already-seen `event_id` is a duplicate, not an attack. The receiver must not process it again, but it acknowledges it. For example, `verify` returns the event with `duplicate=True` (or the kit tells receivers to answer 2xx on `reason == "replay"`). Change the test receiver to do this. Add a test: the first answer is lost, the retry is acknowledged, and the next event is delivered.
   - **If we skip it:** One network hiccup stops Signals updates for a day and raises a false `GxgWebhookDead` alert.

3. **The runbook's passkey revocation destroys the proof of past decisions** (`docs/runbooks/approval-ops.md:49`)
   - **What this is:** AS-30 requires the stored assertion in `decisions` to be "verifiable later". Checking it later needs the public key in `webauthn_credentials`.
   - **Problem:** For a lost or stolen passkey, the runbook says to delete the row. After that, no decision this key ever signed can be checked again: its public key is gone.
   - **Fix:** Revoke without deleting: `UPDATE webauthn_credentials SET enrolled_by = NULL WHERE credential_id = …`. The decider and `gxg_decide` already refuse such a key (tested). Say in the runbook why the row is kept. One line of text.
   - **If we skip it:** The first stolen passkey makes the signed history unverifiable, and that is exactly the case where an audit needs it.

## Should fix

4. **`gxg_reserve_conflicts_total` on the executor port is never tested** (`approval/src/gxg_approval/cli.py:111-118`)
   - **What this is:** AC-M10-05 says every metric of the table is on `/metrics`. This metric is on the executor's own port 9101, set up in `_executor()`.
   - **Problem:** No test builds this registry, increments the counter or reads the page. `test_metrics.py` says "see their own tests", but `grep reserve_conflicts` finds no test. If someone drops `scrape.register(...)`, everything stays green and the alert `GxgReserveConflictsSpike` never has data.
   - **Fix:** Move the three lines into a small function (for example `metrics.executor_registry()`). Test that it shows `gxg_reserve_conflicts_total` after one refused reserve.
   - **If we skip it:** One metric of AC-M10-05 is proven only by reading the code.

5. **An approver can bypass the ui's "args do not match the hash" guard** (`approval/ui/src/gxg_approval_ui/app.py:390-411, 429-432`)
   - **What this is:** The ui must not let anyone approve when the canonical args in the preview envelope do not hash to `canonical_args_hash` (AS-27, EFF-33). It hides the Approve button and, in `_decide`, checks the envelope only when the form field `decision == "approve"`.
   - **Problem:** The decision that is recorded is the one inside the signed challenge, not the form field. `/challenge` does not check the envelope. So: POST `/challenge` with `decision=approve`, sign it, then POST `/decision` with `decision=reject` and that assertion. The ui skips the check, and the decider records **approve** on a mismatching intent.
   - **Fix:** In `/challenge`, refuse `decision=approve` when `envelope(row)` does not match (the same three lines as in `_decide`). Add one test.
   - **If we skip it:** The mismatch guard depends on a field the attacker controls.

6. **An unreadable webhook secret file stops the whole notifier, Telegram included** (`approval/notifier/src/gxg_approval_notifier/webhooks.py:115-120`, `approval/notifier/src/gxg_approval_notifier/cli.py:50-57`)
   - **What this is:** `_secret` reads the file `secret://<path>`. The main loop catches only `psycopg.Error`.
   - **Problem:** The notifier runs as uid 10003 (Dockerfile). Suppose the operator puts the secret file on the host with mode 0600, owned by root. Then `is_file()` is true, `read_bytes()` raises `PermissionError`, the loop dies and the process exits. Approver notifications in Telegram stop too.
   - **Fix:** In `_secret`, catch `OSError` and return `None`. The row then gets `last_status = 'no secret'`, as the runbook already describes. Add one test with a chmod-000 file.
   - **If we skip it:** One badly copied subscriber secret silences every approver notification.

7. **The admin process accepts a plain-HTTP OIDC issuer** (`approval/src/gxg_approval/cli.py:177-180`)
   - **What this is:** The admin API trusts operator tokens signed by keys from the issuer's JWKS. It fetches the discovery document and the JWKS URL with `urllib`.
   - **Problem:** With `GXG_ADMIN_OIDC_ISSUER=http://…` (for example a typo in the env), the keys come over plain HTTP. Anyone on that path can serve their own JWKS and mint a `platform_admin` token. The ui refuses this case with `require_tls`; admin does not.
   - **Fix:** Call the same check (`https://` only, unless loopback) for the issuer and for `jwks_uri`. Two lines and a test.
   - **If we skip it:** A configuration slip turns into operator impersonation.

## Nice to have

Items 8 and 9 are formal (records and evidence), so they are `minor` in the rubric, but both are in `required_improvements`.

8. **Two residual AS-30 risks are not written down for the owner** (`reports/M10-PENDING-OWNER.md`)
   - **What this is:** AS-30 says a compromised ui "cannot decide for the human".
   - **Problem:** (a) WebAuthn does not show the human what the challenge contains. A compromised ui can ask the decider for an *approve* challenge on intent B, then hand it to the browser when the human clicks *reject* on intent A. The human's real passkey signs it, and B is approved. (b) The invite code passes through the ui (`/passkeys/options`). A ui compromised while an invite is open can enroll its own software key for that human. Both need the human to act within the window, and neither can be fixed inside the current spec option. But the milestone files claim more than the design gives.
   - **Fix:** Add both to `reports/M10-PENDING-OWNER.md` as known residual risks, so the owner can accept them or pick the other AS-30 option.
   - **If we skip it:** The owner believes the ui is fully untrusted for decisions, and it is not.

9. **Evidence of the cycle is incomplete** (`reports/conformance/`, `reports/mutmut/`)
   - **What this is:** DoD-3 needs `reports/conformance/M10.json` (with `self_check`). DoD-4 needs a mutmut report for the Python modules the milestone adds to `only_mutate` (decider, admin, metrics, sweeper, notifier webhooks, kit webhooks).
   - **Problem:** `M10.json` is missing. There is no mutmut report for M10's Python. `reports/mutmut/M10-sql.txt` shows a run with 5 survivors, and the run that killed them is only a sentence ("Re-run … all killed"), with no output.
   - **Fix:** Commit `M10.json`, the mutmut output for the M10 modules, the full output of the SQL re-run, and the AC-M10-06/07 reports.
   - **If we skip it:** The milestone cannot close under DoD-3 and DoD-4.

10. **SEC-15 for webhook URLs relies on the network alone** (`approval/notifier/src/gxg_approval_notifier/webhooks.py:130`): the schema allows any `https://` host, including `169.254.169.254` or `postgres` (which the notifier can reach on `notifier_db`). The bundle is signed and comes from Git, and the real subscriber (Signals) will have a private address. So record the decision "allowlist = network membership" next to the other defaults, rather than add an IP check.
11. **Internal roles can forge signed webhook events** (`0001_core.py:516`): `gxg_api`, `gxg_sweeper` and `gxg_admin` keep `INSERT` on `outbox`. A row with `topic='webhook'` and any payload is signed by the notifier and sent to the tenant's subscriber. This goes to the backlog, together with the M9 item on outbox topics.
12. **`/metrics` on `api` has no authentication** (`approval/src/gxg_approval/api.py:243-247`): any process on the internal network, a connector of another tenant included, can read per-tenant counts and subscriber ids. These are aggregates only. Backlog.
13. **`stage_c.py` maps EX-01…EX-24, but the Conformance Matrix also lists EX-25 in stage C**: AC-M10-07 names only 01…24, so this is not a failure. Add it once, so the "stage C exit" report covers the whole stage.

## Ready-made solutions (ponytail «есть ли готовое решение»)

- WebAuthn checks: py_webauthn (already in the lock) is used for both ceremonies. Own code is only the challenge rows. OK.
- Webhook signature: stdlib `hmac`. `standardwebhooks` and Svix use a different header format than AS-22, so the custom code is justified (about 30 lines).
- Webhook delivery queue: own code on the existing outbox. Alternatives: Svix server (self-hosted), pgmq, procrastinate. All add a service or a format foreign to AS-22/23, and the outbox already exists. The custom code is justified.
- Metrics: `prometheus_client` with a custom collector. Alert checks: `promtool`. OK.
- Operator token: PyJWT `PyJWKClient`. OK.
- SQL mutations: own script (`scripts/ci/mutate_m7_sql.py`). There is no maintained mutation tool for PL/pgSQL (pgTAP tests, it does not mutate). Already accepted in M7. Justified.
- Stage C report: own 140-line mapper over pytest JUnit. A marker-based selection (`-m ex_20`) plus pytest's own JUnit would do the same with less mapping. It is small and reads pytest's output, so it is justified.
- Access log filter: stdlib `logging.Filter`. Dropping the access log altogether loses operations data. OK.

Rubric mapping: 1–7 are `major` (1–3 Must fix: bugs and an unmet AS-30 MUST; 4–7 Should fix: risky code without a test or a missing check). 8–13 are `minor`.

Verdict: fix 1, 2, 3 first, then 4–9.
Not checked: AC-M10-06 and AC-M10-07. `reports/conformance/M10-mc05.md` and `reports/conformance/stage-C.json` are not in the commit yet. I did not run `stand/run.sh` or `check-all.sh`, as instructed.
