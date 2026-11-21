# M8 cycle 1 — self-review (ponytail-review of `3c2f0b0..HEAD`, by the milestone owner)

What this change does: adds the `executor` process (outbox → reserve → execution token → connector `execute` →
complete; outcome_unknown → `reconcile` with backoff), key rotation (`gxg-approval keys`, AS-10 release), a JWKS
that `api` re-reads on change; in `demo` the `demo.message.propose` tool, the internal `execute`/`reconcile` port
and the fake provider; EX-01 on the M5 stand without a permanent `*.execute` registration. M7 close items:
idempotency across requesters, the T2 window mutant.

Load assumed: a handful of tenants, tens of intents per minute, a few executor instances (README «Развёртывание»).

## Must fix

None found.

## Should fix (fixed in the cycle)

1. **Worker dies on any non-DB error** (`executor.py`, `run`) — a bug on one row stopped the thread for good. Fixed:
   the loop logs and continues; a reconcile row is rolled back and retried, an execute row was already delivered and
   its reservation is released by the sweeper (T10). Test `test_run_survives_an_error_and_stops`.
2. **Replay of a token always 401 or always 409** (`effects.py`): AC-M8-03 (replay in the local cache → 401) and
   AC-M8-04 (replay after success → 409) both hold only if the connector remembers which `jti` it dispatched.
   Fixed with a dict pruned at `exp` + 5 s. Tests in both suites.
3. **Dead or redundant checks** found by mutation testing, deleted: `set(args)` check (hash already binds args),
   `startswith(prefix)` (board ids are exact keys), the `provider_operation_id` branch of reconcile (never set for an
   outcome_unknown execution), `status < 500` in the refusal mapping, the provider credential argument (unused),
   WAL pragma, explicit httpx timeouts equal to the defaults, try/except around `gxg_complete`/`gxg_reconcile`
   (a refusal now propagates and the row is rolled back).
4. **Executor log lines carried no fields to test** — unified into one JSON event (`executed`, `not_completed`)
   with ids, tenant, tool, outcome, error; asserted in tests.

## Nice to have (left)

5. `demo` `/readyz` fetches the JWKS synchronously inside the async handler (≤ 5 s block while `api` is down; only
   on a due refetch). Fix with `anyio.to_thread` if readiness probes get frequent.
6. `executor` has no HTTP probe (like `sweeper`, M7 backlog → M10/M14); it refuses to start without key, DB, bundle.

Verdict: Ship.
Lean: no further deletion found.
Not checked: GitHub CI (runners), the effect path through ContextForge (the stand has no Approval Service; decision 7
of the plan).
