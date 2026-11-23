# M10 cycle 3: review of the diff (critic)

Commit `7da6e46` (branch `claude/m10-ops`; code at `3e5541c`), diff `47b7643..3e5541c` (cycle 3), base `40cec84`.
Lighter process (owner decision 2026-11-23): no ponytail-review is required in this cycle. This file is the short review of the cycle-3 diff. A finding needs a concrete case.

## Critic runs

| Command | Result |
| --- | --- |
| `DOCKER_BUILD_FLAGS='--network host --secret id=ca-bundle,src=/root/.ccr/ca-bundle.crt' uv run pytest approval/tests approval/tests_db approval/notifier approval/ui approval/deploy/tests packages/gxg-connector-kit/tests connectors/demo/tests -q -p no:cacheprovider` | 1485 passed, 0 skipped, 0 failed (239 s). Includes `test_network_policy.py` and `test_alert_rules.py` (promtool) |
| `uv run ruff check .`, `ruff format --check .`, all mypy steps of `scripts/ci/check-all.sh` (lines 12–25) | clean; the root suite in that block 1172 passed |
| manual mutation `metrics.py`: skip of unknown non-latency rows → `if False:` | killed by `test_unknown_database_figure_is_skipped`; reverted |
| manual mutation 0005 `gxg_metrics()`: drop `i.terminal_at > now() - interval '1 hour'` | killed by `test_scrape_after_a_deferral_and_an_expiry_by_rate_limit`; reverted |
| manual mutation `executor._rate_limited`: error.schema check → the cycle-2 hand check (dict, code, 1 ≤ retry ≤ 86400) | killed by `test_ex25_rate_limited_body_bounds` (missing `message`/`retryable`, extra key); reverted |
| manual mutation `cli._admin`: drop `require_https(jwks_uri, …)` | killed by `test_admin_refuses_a_plain_http_jwks_uri`; reverted |
| manual mutation `metrics.LABELS`: rename `gxg_execute_deferred_total` | 3 tests of `test_metrics.py` fail; reverted |
| `schemas/error.schema.json` vs kit copy | byte-equal (md5), guarded by kit `test_copy_equals_repository_schema` / `test_no_extra_copies` |
| stand evidence `reports/conformance/M10-stand/*.xml` | 54 tests, 0 failures/errors/skips each; timestamps 13:00, 13:15, 13:30 UTC, after the `3e5541c` commit (12:59); stand code untouched in cycle 3, so not re-run |

## Cycle 2 checklist (required_improvements 1–6)

| # | Status | Evidence |
| --- | --- | --- |
| 1 N1 | done | `metrics.py`: both names in `HELP`/`LABELS` (deferred is a counter); only `gxg_approval_latency_seconds_*` rows reach the histogram, any other unknown name is skipped. `test_scrape_after_a_deferral_and_an_expiry_by_rate_limit` scrapes the real `/metrics` after T12b and after T8 of a rate-limited intent (200, both series); `test_every_metric_is_exposed` has `gxg_execute_deferred_total` in `SPEC` and checks the latency series carries only the human-approved tool |
| 2 N2 | done | `gxg_intents_expired_after_rate_limit` counts only `terminal_at` in the last hour; DB test: `terminal_at` −61 min → series gone; promtool case at 10 m: `exp_alerts: []` |
| 3 N3 | done | `cli._admin` `require_https(jwks_uri, …)`; test with an http `jwks_uri` in a mocked discovery; mutation killed |
| 4 N4 | done | `reports/mutmut/M10-cycle-3.txt`: policy 376/376, metrics 143/146, sweeper 82/84, intents 818/818, api 143/149, transitions 258/258, executor `_rate_limited` 16/16; every survivor is in `scripts/ci/mutmut-equivalent.txt` with a reason (SQL keyword case, `auto_describe=None`, api ones from M7). Reasons checked: valid |
| 5 N5 | done | `_rate_limited` validates with `_schema.validator("error")`; error.schema copied into the kit (drift test) |
| 6 evidence | done | `stage-C.json` and `M10.json` at `3e5541c` (code commit; `7da6e46` adds reports only); EX-01…EX-25, MC-05 passed; AC-M10-05 lists `test_metrics.py` |

## Findings (all minor)

### N10 minor (missed_earlier): no runbook step for the AS-12a alerts
`docs/runbooks/approval-ops.md` covers `GxgSwitchChanged`, `GxgAdminAction`, `GxgOutcomeUnknownOverAnHour`, `GxgWebhookDead`, `GxgBundleStale`. `GxgExecuteDeferred` and `GxgExpiredAfterRateLimit` (cycle 2) have no section (nor do `GxgPendingNearTenantLimit`, `GxgReserveConflictsSpike`, `GxgTokenVerifyFailures`, `GxgQuotaRepeated`, `GxgClockSkew`). Case: the on-call gets `GxgExpiredAfterRateLimit` for tenant t-alpha. Nothing tells them to check the connector's send quota / flood-wait and that the agent has to propose again. Backlog.

### N11 minor (new_regression): the 1-hour window can hide an expiry
The N2 fix bounds the gauge to `terminal_at > now() - 1 h`. Case: Prometheus (or `api`) is down for more than an hour when an intent expires after 429. No scrape ever sees the series, so the alert never fires. Also, the alert summary does not say "in the last hour", while the HELP text does. Low risk: the deferrals are still in the monotonic `gxg_execute_deferred_total` and `GxgExecuteDeferred`. Backlog: say "in the last hour" in the summary, or expose a monotonic count of expiries after 429.

## Not findings
- `policy.py`: the deny `Outcome` no longer passes `attempts`. A denied intent is not stored, so `max_attempts` is never read on that path.
- `executor._rate_limited` `int(doc["retry_after_s"])`: error.schema allows `5.0` as an integer, so `int()` is correct. bool is not an integer in jsonschema. The 5…3600 clamp stays in `gxg_defer`.
- `0005_ops.py` edited in place: the migration is not on integration yet, so no deployed database has it.

## lean_lines: 15
`test_unknown_database_figure_is_skipped` builds a fake pool from three nested classes (~20 lines). `unittest.mock.MagicMock` with `fetchall.return_value` does the same in about 5 lines. Nothing else.
