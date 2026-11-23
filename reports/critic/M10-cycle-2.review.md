# M10 cycle 2: review of the diff (critic)

Commit `1043549` (branch `claude/m10-ops`), diff against `b3ab372` (cycle 1) and `40cec84` (base).
Lighter process (owner decision 2026-11-23): no ponytail-review is required in this cycle. This file is the short review of the diff. Every finding below has a concrete case.

## Critic runs

| Command | Result |
| --- | --- |
| `uv run pytest approval/tests approval/tests_db approval/notifier approval/ui packages/gxg-connector-kit/tests/test_webhooks.py connectors/demo/tests approval/deploy/tests -q -p no:cacheprovider` | 1095 passed, 6 errors. All 6 are in `test_network_policy.py`: the docker build has no proxy CA in this sandbox |
| the same `test_network_policy.py` with `DOCKER_BUILD_FLAGS='--network host --secret id=ca-bundle,…'` | 6 passed |
| `uv run pytest approval/deploy/tests/test_alert_rules.py` | 3 passed |
| `uv run ruff check .`, `ruff format --check .` | clean |
| the 6 `mypy` steps of `check-all.sh` (strict) | no issues |
| critic probe (temporary test, deleted): T12b deferral → `GET /metrics`; then T8 expiry → `GET /metrics` | first scrape 200, but no `gxg_execute_deferred_total` and a bogus empty `gxg_approval_latency_seconds{tool="demo.note.propose"}`; **second scrape HTTP 500** |
| manual mutation `policy.py` `min(` → `max(` (max_attempts) | killed by `test_max_attempts_default_defaults_and_rules`; reverted |
| manual mutation 0006 `_gxg_execution_guard` without `available_at <= now()` | killed by `test_ex25_reserve_needs_a_due_task`; reverted |
| manual mutation `executor._defer`: defer also from `dispatching` | killed by `test_ex25_429_after_dispatch_*` (2); reverted |

## Cycle 1 checklist (required_improvements 1–9)

| # | Status | Evidence |
| --- | --- | --- |
| 1 F1 | done | `gxg_metrics()` counts an execution under every outcome it reached (0005, LATERAL unnest). `test_executions_total_never_goes_down`. promtool case: a reconcile does not fire the alert |
| 2 F2 | done | kit `verify` returns None for a valid repeat (signature and `t` are still checked first). `test_a_lost_reply_does_not_block_the_subscriber` |
| 3 F3 | done | runbook §3: `UPDATE … SET enrolled_by = NULL`, the row is kept |
| 4 F4 | done | `cli.executor_metrics()`, `test_executor_metrics_registry_has_reserve_conflicts` |
| 5 F5 | done | `/challenge` refuses approve when the envelope does not match. The POST check was dropped (the challenge holds the decision), test in `test_edges.py` |
| 6 F6 | done | `_secret` catches `OSError`, `test_unreadable_secret_is_a_failed_try` |
| 7 F7 | **partly** | the issuer must be https. **`jwks_uri` from discovery is not checked**, although the item asked for "issuer and jwks_uri" |
| 8 F8 | done | `reports/M10-PENDING-OWNER.md` R1, R2 |
| 9 F9 | **partly** | M10.json, mc05, stage-C, SQL and kit/notifier/decider/admin/executor mutmut are present. **`metrics.py` and `sweeper.py`** (named in F9) and the changed `policy.py`, `intents.py`, `api.py`, `transitions.py` were not mutation-tested. Lighter process rule 1 asks for the changed files of approval |

## Findings

### N1 major (AC-M10-05 fails): the new T12b metrics never reach `/metrics`, and the scrape breaks with 500
`approval/src/gxg_approval/metrics.py:25-44, 60-68`. `gxg_metrics()` (0005) now returns `gxg_execute_deferred_total{tenant,tool}` and `gxg_intents_expired_after_rate_limit{tenant}`. `DatabaseCollector` knows only the names in `LABELS`. Every other row is treated as part of the latency histogram:
- `gxg_execute_deferred_total` is dropped. It turns into an empty `gxg_approval_latency_seconds{tenant,tool}` series, so even an auto-approve tool shows up as "approved by a human with count 0";
- `gxg_intents_expired_after_rate_limit` has no `tool` label, so `labels["tool"]` raises KeyError and `/metrics` returns **500**. The probe showed this.

Concrete case: one intent expires after a 429. From then on every scrape of the api fails, and the expired intent stays in the table. All database metrics and their alerts (outcome_unknown, pending, switch, webhook dead) go blind for good. `GxgExecuteDeferred` and `GxgExpiredAfterRateLimit` can never fire. The metric `gxg_execute_deferred_total` is in the approval-service-v1 table, so AC-M10-05 ("все метрики из таблицы … доступны на `/metrics`") is not met. The tests check the SQL function (`test_ex25_*` reads `gxg_metrics()`) and promtool with synthetic series. No test scrapes `/metrics` with these rows, and `SPEC` in `test_metrics.py` lacks the new name.

### N2 major: `GxgExpiredAfterRateLimit` can never resolve
`0005_ops.py` `gxg_intents_expired_after_rate_limit` (count of all `expired` intents whose last exec is `rate_limited`, with no time bound) and `rules.yaml` `> 0`. Intents are not deleted (RET-02 is out of M10), so after the first such expiry the warning fires forever. The operator silences it and loses the signal. This is the same class of defect as cycle 1 F1. The promtool case only checks that the alert fires, never that it clears.

### N3 minor: F7 is not fully closed
`approval/src/gxg_approval/cli.py:201-212`. `jwks_uri` from discovery goes to `PyJWKClient` without the https check (loopback excepted) that the issuer gets. RFC 8414 requires https, so a compliant IdP passes. A proxied IdP that emits `http://` URLs would give anyone on the path a key that mints `platform_admin` tokens. The fix is one line plus a test case.

### N4 minor: mutation scope of the changed approval files
`policy.py` (max_attempts), `metrics.py`, `sweeper.py`, `intents.py`, `api.py` and `transitions.py` changed in M10 and are in `only_mutate`, but `reports/mutmut/M10-approval-security.txt` covers only decider, admin and executor. My manual `min`→`max` mutation in `policy.py` was killed. Missing branches in `metrics.py` (N1) could not be caught by mutation either way, but the checklist item stays open.

### N5 minor: a 429 body is not validated against error.schema
`executor._rate_limited` checks only `code` and `retry_after_s`. A body `{"code":"rate_limited","retry_after_s":30}` without `message`/`retryable` does not match error.schema, so by AS-12a it is T12a, but it goes to T12b. The validator `_schema.validator("error")` already exists.

### N6 minor: the AS-10 release is recorded as `reserve_lease_expired`
`0006` trigger `executions_release_reason` stamps every reserved→released without a code, including `gxg_release_reserved` (executor key removed, AS-10). The audit row says "key removed", but `executions.error_code` says lease expired. The fix is to set the code in `gxg_release_reserved`.

### N7 minor: MC-05 re-run predates the cycle-2 code
`reports/conformance/M10-mc05.md` was run at `5826391`, before T12b, `execute_quota` in the demo connector and EX-25. The criterion does not depend on FastMCP for these paths, so AC-M10-06 stays passed. A re-run of `connectors/demo` and EX-25 on fastmcp 4.2.4 would make the evidence current.

### N8 minor (missed_earlier, backlog): dead `EXECUTIONS` counter in the executor
`executor.py:38, 267` `Counter("gxg_executions_total")` is incremented but registered on no exposed registry (`executor_metrics()` has only reserve conflicts). Its name matches the database metric. Delete it.

### N9 minor: duplicated https rule
`approval/src/gxg_approval/cli.py:154-163` `require_https` duplicates `approval/ui/src/gxg_approval_ui/config.py:25` `require_tls`.

## Existing solutions
- Webhook signature: AS-22 sets its own header format. standardwebhooks/svix use another one, so stdlib hmac is justified.
- WebAuthn: py_webauthn is used.
- Metrics: prometheus_client Collector is used. N1 is a bug in using it, not a reason to replace it.
- Purge: the existing sweeper loop is used (pg_cron would be a new extension).
- Admin auth: PyJWT PyJWKClient is used. Discovery is fetched by hand with urllib. authlib, already in the ui, could load the metadata, but the hand-written code is 3 lines.

## lean_lines
About 15: `require_https` (≈9, reuse the ui rule or a kit helper), `EXECUTIONS` (2), `_rate_limited` hand checks replaced by the existing schema validator (≈4).
