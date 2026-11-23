# M10 cycle 2 — plan (ponytail full)

Input: `reports/critic/M10-cycle-1.json` (score 7.5, cap major). Scope addition from the orchestrator (2026-11-23):
T12b / AS-12a / EX-25 (specs taken from `a2ae2ff`, commit `b5a53e7`).

## required_improvements of cycle 1 (closed checklist)

| # | Fix | Where | Ladder step |
| --- | --- | --- | --- |
| 1 | `gxg_executions_total` stays monotonic: count executions by the outcome they **reached** (an `outcome_unknown` row counts there forever, also after reconcile), and promtool case for a reconcile | `gxg_metrics()` in 0006, `rules.test.yaml` | one SQL change |
| 2 | A correctly signed duplicate `event_id` is acknowledged (2xx), not refused: kit `verify` returns `None` for a duplicate (signature and `t` still checked), test receiver answers 200 | kit `webhooks.py`, notifier tests | existing code |
| 3 | Runbook: revoke a passkey with `UPDATE … SET enrolled_by = NULL` (the decider refuses it, the key stays for later verification) | runbook §3 | text |
| 4 | Test of the executor's metrics registry (`gxg_reserve_conflicts_total`) | `cli._executor_metrics()` + test | split one function out |
| 5 | `/challenge` refuses `approve` when the canonical args do not match the hash (the challenge's decision is what is recorded) | ui `app.py` + test | 3 lines |
| 6 | An unreadable webhook secret is a failed try, not a crash (`OSError`) | notifier `webhooks.py` + test | 2 lines |
| 7 | Admin requires an https issuer (loopback allowed for tests, as the ui) | `cli._admin` + test | existing pattern |
| 8 | AS-30 residual risks (challenge swap by a compromised ui; invite through the ui) recorded for the owner | `M10-PENDING-OWNER.md` | text |
| 9 | Evidence: `reports/conformance/M10.json` with `self_check`, mutmut reports of the M10 packages, full SQL mutation output, `M10-mc05.md`, `stage-C.json`, green `check-all.sh` | `reports/` | runs |

Cheap minors taken too: webhook URL to an IP literal of a private/loopback/link-local range or `localhost` is refused (SEC-15); outbox rows with topic `webhook` only from the trigger (BEFORE INSERT guard: process roles with outbox INSERT cannot forge events). Left in backlog: `/metrics` without auth (internal network, aggregates only).

## T12b / EX-25

- `transitions.py`: `T12b complete_rate_limited executing/reserved → approved`.
- Migration `0006_t12b`:
  - `gxg_defer(execution, executor, retry_after_s)`: CAS on `execution_id AND state = 'reserved'` (otherwise ignored, audited); exec → `released`, `error_code = rate_limited`; intent → `approved`, attempts unchanged; outbox `execute` at `now + clamp(retry, 5, 3600) · (1 + random()·0.2)` unless that is ≥ `execution_deadline`.
  - `gxg_complete`, T12a: CAS on the execution (`refused_before_dispatch` on a non-`reserved` execution is ignored and audited).
  - T10: `error_code = reserve_lease_expired`.
  - T7 only from a due outbox task: `gxg_reserve` refuses without an undelivered `execute` row with `available_at ≤ now`.
  - `gxg_metrics()`: `gxg_execute_deferred_total{tenant,tool}`, `gxg_intents_expired_after_rate_limit{tenant}`; alert rules.
- executor: a 429 whose body is an error.schema document with `code = rate_limited` and `retry_after_s ≥ 1`, while the execution is `reserved` → `gxg_defer`; any other 429 → T12a; after dispatch → T13 (unchanged).
- demo: provider mode `execute_quota:<n>/<s>` — the connector answers 429 `rate_limited` with `retry_after_s` **before** dispatch when `<n>` sends already happened in the last `<s>` s.
- Tests EX-25: 429 then ok (attempts unchanged, released + error_code, next reserve not before clamp, succeeded); 429 always → `expired` at the deadline, 0 provider calls; 429 after dispatch → `outcome_unknown`; 429 without `retry_after_s` → T12a; late 429 after T10 → ignored. `stage_c.py` maps EX-25.
