# M8 cycle 2 — plan (ponytail full)

Input: `reports/critic/M8-cycle-1.json` (score 7.0, caps: major) — its `required_improvements` 1–8 are the closed
checklist of this cycle. New scope (owner decisions relayed by the orchestrator 2026-11-21, docs taken from
`origin/claude/admiring-planck-vzys0x@643ea3e`): EFF-08 / DDL idempotency per `(tenant_id, requester_principal,
idempotency_key)` as a migration instead of the cycle-1 requester check; DoD-1 `ci_local` while GitHub Actions has no
minutes.

## Checklist

| # | Item | Change | Rung |
| --- | --- | --- | --- |
| 1 | propose output schema vs idempotent repeat | `message-propose.output.json` status = the 11 statuses of `intent-status.schema.json`; e2e: propose → succeeded → same propose → 200, same intent, `succeeded` through the MCP client | one line + test |
| 2 | no active signing key | `take()` checks `SigningKeys.current()` before reserving; no key → outbox row deferred like the kill switch (backoff, no attempt); test; README compromise row | existing defer path |
| 3 | key file not visible after rotation | compose: executor key file from a directory bind mount (`./exec-keys:/run/gxg/exec-keys:ro`), not a single-file secret; `test_deploy` check; README says where each `keys` command runs; one release function (`keys.release_reserved(conn)`) used by the CLI and `Executor`; CLI test against the DB | config |
| 4 | temp file mode | `os.open(tmp, O_WRONLY|O_CREAT|O_TRUNC, mode)` | stdlib |
| 5 | stale `internal_url` | client cache keyed by `(id, internal_url)` | one line |
| 6 | `CONTINUE WHEN` labelled equivalent | deterministic test: a second session holds the intent lock while the release loop waits, moves the execution to `dispatching`, commits → release returns 0, no error; driver row no longer `equivalent` | test |
| 7 | evidence | re-run mutmut (executor, keys, demo effects/provider, intents for EFF-08) and the **full** SQL driver; counts and survivor ids in `reports/mutmut/M8.txt`; DoD-6 rows for `JsonFile`, `SigningKeys`, `_dispatched` (below) | — |
| 8 | minors | 7 (NFD sent): the provider gets the normalized args; 9 (constants duplicated with tools.py) → backlog (import cycle tools↔effects, no behaviour); 8 (executor probe), 12 (propose through ContextForge on the stand) → `docs/backlog.md` | — |
| N1 | EFF-08 per requester (owner) | migration `0003`: drop `UNIQUE (tenant_id, idempotency_key)`, add `UNIQUE (tenant_id, requester_principal, idempotency_key)`; `_SQL_BY_KEY` by the three columns; the cycle-1 requester branch removed; test: two agents of one tenant, same key → each gets its own intent, neither sees the other's; T1 lock unchanged | DDL |
| N2 | DoD-1 `ci_local` | `scripts/ci/check-all.sh` on the checked commit, output summarised in `reports/conformance/M8.json` (`ci_local`) | — |

## DoD-6 additions (cycle-1 critic finding 11)

| Component | Taken | Considered | Why |
| --- | --- | --- | --- |
| `config.JsonFile` (reload on mtime) | stdlib `stat` + `json` | `watchdog`/inotify; reload thread like `bundle_store` | one stat per read is cheaper than a watcher thread and has no missed-event case; no new dependency |
| `executor.SigningKeys` (newest key past `not_before`) | 10 lines over the kit (`_jwt`, `cryptography`) | `jwcrypto` JWK sets; PyJWT `PyJWKSet` | neither has a «not before» choice for signing keys; both would still need the selection code |
| `Effects._dispatched` (dispatched `jti` memory) | dict pruned at `exp` + 5 s | kit `JtiCache` | `JtiCache.add` inserts on lookup, so it cannot answer «was this one dispatched?» without marking it |
