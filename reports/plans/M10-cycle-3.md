# M10 cycle 3 (last allowed), ponytail full

Input: critic cycle 2, `reports/critic/M10-cycle-2.json` (5.0: AC-M10-05 failed, 2 major).

| # | Finding | Change (smallest step) |
| --- | --- | --- |
| 1 | N1: `/metrics` 500 on `gxg_intents_expired_after_rate_limit`, `gxg_execute_deferred_total` lost | `metrics.py`: both names in `HELP`/`LABELS`; the histogram takes only `gxg_approval_latency_seconds_*` rows, any other unknown name is skipped. Test: scrape `/metrics` after a T12b deferral and after T8 of a rate-limited intent; `gxg_execute_deferred_total` in `SPEC` |
| 2 | N2: `GxgExpiredAfterRateLimit` never clears | `gxg_metrics()` (0005): count only intents with `terminal_at` in the last hour (a gauge with a window, purge-safe — a counter would drop on purge and `increase()` would fire on that). Rule unchanged (`> 0`). promtool case: the alert clears when the series goes back to 0. Alert doc: "in the last hour" |
| 3 | N3: `jwks_uri` not checked for https | `cli._admin`: `require_https(jwks_uri, "jwks_uri")`; test with a loopback discovery answering an http `jwks_uri` |
| 4 | N5: 429 body not checked against error.schema | `executor._rate_limited`: `_schema.validator("error").is_valid(doc)` first; test case: no `retryable` → None |
| 5 | N4: mutmut on changed files of `policy.py`, `metrics.py`, `sweeper.py`, `intents.py`, `api.py`, `transitions.py` | one run per file, commit each result |
| 6 | evidence | re-run stage-C (tests + the stand JUnit of cycle 2: the stand code is unchanged in cycle 3 — metrics/executor/admin only, demo and ContextForge untouched), regenerate `stage-C.json` and `M10.json` on the final commit |

Not done (minor, to `docs/backlog.md` at closing): N6 release reason under AS-10, N7 MC-05 re-run before cycle 2 code, N8 dead `EXECUTIONS` counter, N9 `require_https` vs `require_tls`.
