# M8 cycle 1 — plan (ponytail ultra)

Input: `docs/implementation-plan-v1.md` (M8, AC-M8-01…14, DoD-1…6, process «вторая редакция»),
`effects-and-approval-v1.md` (EFF-01…33), `approval-service-v1.md` (T1–T18 + T12a, AS-05, AS-09…AS-14, AS-25…AS-29),
`mcp-connector-contract-v1.md` (MCC-05, MCC-07, MCC-09), `demo-connector-v1.md` (propose, internal HTTP, fake provider),
`security-v1.md` (SEC-10, SEC-14, SEC-20), `conformance-matrix-v1.md` (EX-01…EX-24).
Base: `origin/claude/integration` `3c2f0b0` (M0…M5, M7). Branch `claude/m8-executor`.
New scope from the M7 close (`reports/critic/M7-final.json`): finding 2 (idempotency across requesters),
finding 3 (T2 window mutant).

## What already exists (rung 2: in the repository)

M7 already built the database half of M8: T7 `gxg_reserve`, T9 `gxg_dispatch` (+ `POST /v1/executions/{id}/dispatch`
in `api`, verifying the execution token with the kit), T11–T13/T12a `gxg_complete`, T15–T17 `gxg_reconcile`
(with a delayed outbox row for T17), T10/T14 in the sweeper, `GET /.well-known/gxg-exec-jwks.json` (static file),
roles and grants of `gxg_executor` (AS-27), sealed-box `Decryptor('args')`. The kit has `ExecTokenVerifier` +
`ExecutorJwks` (10-min cache, fail closed, rotation tested in M1), `svc_jwt`, `canonical`, `grants`, `errors`.
`stand/unified_pdp.rules.json` already has `deny:agents-never-execute` (M5). M8 adds only what is missing.

## DoD-6: ready-made solutions first

| Component | Taken | Considered and not taken | Why |
| --- | --- | --- | --- |
| Executor work queue | **the existing `outbox` table + `FOR UPDATE SKIP LOCKED`** (spec: «координация только через БД») | Celery/RQ/Dramatiq; PgQueuer/procrastinate; `LISTEN/NOTIFY` | spec fixes the outbox and the DB as the only coordinator; a broker adds a second source of truth; PgQueuer/procrastinate bring their own tables and job model, and the outbox rows are written by M7's definer functions in the same transaction as the move (AS-01) |
| Reservation / dispatch CAS | **M7 functions** `gxg_reserve`, `gxg_dispatch`, `gxg_complete`, `gxg_reconcile` | — | already there and tested (AC-M7-02) |
| Execution token signing | **kit `_jwt.sign` (PyJWT)**, new 15-line `exec_token.sign()` next to the verifier | `joserfc`/`authlib` | PyJWT already signs service JWTs; one place for `typ`/`kid`/ttl rules |
| JWKS document | **kit**: `exec_token.public_jwk()` (RFC 8037 OKP JWK, `kid` = RFC 7638 thumbprint, kit `_jwt.thumbprint`) | `jwcrypto` | 6 lines over what the kit has; no new dependency |
| JWKS cache in the connector | **kit `ExecutorJwks`** (M1) | — | AS-09 10-min cache already implemented and tested |
| HTTP: executor → connector | **httpx** (installed, used by `mcp`) | `requests`, urllib | installed; timeouts per call (AS-13), Unix-socket transport for tests |
| HTTP: connector → Approval Service | **httpx2** (installed with fastmcp 4 in the connector's lock) | adding httpx to the demo lock | already in the connector's dependency tree |
| Internal HTTP of the connector | **Starlette** (installed with FastMCP) + uvicorn | FastAPI | two routes, no OpenAPI needed; FastAPI is not in the demo lock |
| Fake provider journal | **stdlib `sqlite3`** (spec: «SQLite в volume») | — | spec-given |
| Backoff of reconcile (T17) | **SQL arithmetic** on the delayed outbox row of M7 `gxg_reconcile(…, retry_after_s)`; delay = time since dispatch clamped to [5 s, 1 h], stop after 24 h | `tenacity`, `backoff` | the retry is a DB row, not an in-process loop; the library would only compute one number |
| Key rotation CLI | **stdlib** (`argparse`, `json`) in `gxg-approval keys rotate|remove` | `step`/`jose` CLIs | spec names the command; it edits two JSON files |
| Real concurrency in tests | **uvicorn on Unix sockets in threads**, the connector in a **subprocess** for the kill test, real PostgreSQL (tests_db) | mocks, TCP | spec: «Гонки — реальным параллелизмом против одной БД»; the repo forbids TCP in tests (AC-M0-01) |

## Scope (every place the change reaches)

| Part | Files | Rung |
| --- | --- | --- |
| Kit: token signing, JWK, schema copies (`execute-request`, `reconcile-result`) | `packages/gxg-connector-kit/src/gxg_connector_kit/{exec_token,_schema}.py`, `schemas/` copies, kit tests | small additions |
| Idempotency across requesters (new_scope 1) | `approval/.../intents.py` (`_SQL_BY_KEY` + requester check), `tests_db/test_propose.py` (two agents) | one condition |
| T2 window (new_scope 2) | `tests_db/test_state_machine.py` (window 10 m), `scripts/ci/mutate_m7_sql.py` (+ the mutant) | test + driver line |
| AS-09/AS-10 in `api`: JWKS re-read on change; dispatch verifier always current | `approval/.../api.py`, `config.py` (`JsonFile`), `cli.py` | minimal |
| AS-10 compromise: release every `reserved` execution (T10 move, no lease wait) | `migrations/versions/0002_executor.py` (`gxg_release_reserved`, partial index on pending outbox), grant to `gxg_executor` | definer function, same as T10 |
| Executor process | `approval/.../executor.py`, `cli.py` (`executor`), `config.py` (key files) | minimal code over M7 functions |
| Key rotation | `approval/.../keys.py`, `cli.py` (`keys rotate|remove`) | stdlib |
| Deploy | `approval/deploy/compose.yaml` (executor + its secrets, AS-28), `approval/tests/test_deploy.py` | — |
| demo: `demo.message.propose` | `connectors/demo/gxg-manifest.yaml`, `schemas/message-propose.{input,output}.json`, `src/demo_connector/effects.py`, `tools.py` (registration) | kit + httpx2 |
| demo: internal `execute`/`reconcile` (port 8081) | `src/demo_connector/effects.py`, `server.py` (config, second port, readiness with JWKS, MCC-09) | Starlette |
| demo: fake provider (all modes, SQLite) + test controls | `src/demo_connector/provider.py`, `testctl.py` (`PUT /_test/provider/mode`, `GET /_test/provider/calls`) | sqlite3 |
| demo image | `connectors/demo/Dockerfile` (port 8081), `docker_smoke.sh` unchanged | — |
| demo tests | `connectors/demo/tests/test_effects.py` (propose, provider modes, execute checks without DB) | — |
| End-to-end ACs on real PostgreSQL | `approval/tests_db/effects_world.py` (bundle from the demo fixtures, servers on Unix sockets), `test_executor.py` (AC-M8-01…12, 14) | — |
| EX-01 through ContextForge (AC-M8-13) | `stand/bootstrap.py` (no permanent `*.execute` registration, EFF-02), `stand/tests/test_ex.py`, `test_sc.py` (lists without the decoy) | — |
| Fixtures | `connectors/demo/fixtures/grants/t-beta.yaml` (+ `agent:t-beta:actor` with `propose` on `b-public`, for AC-M8-10) | data |
| Mutation testing | `approval/pyproject.toml` (+ `executor.py`, `keys.py`), `connectors/demo/pyproject.toml` (+ `effects.py`, `provider.py`), SQL driver (+ M8 SQL) | mutmut |
| Docs, report | `approval/README.md`, `connectors/demo/README.md`, `stand/README.md`, `reports/conformance/M8.json`, `reports/mutmut/M8.txt` | — |

## Decisions where the spec leaves room (no spec change)

1. **Executor reading of `outbox`.** Rows `execute` and `reconcile` are claimed with `FOR UPDATE OF outbox SKIP LOCKED`.
   `execute`: reserve (T7) and `delivered_at` in one transaction, commit, then call the connector. A refused reserve
   because of the kill switch (423) leaves the row undelivered with `attempts++`; the claim waits
   `5 s · 2^(attempts-1)` (≤ 5 min) after `available_at` — the role may not change `available_at` (AS-27).
   Any other refusal (status moved on) delivers the row. `reconcile`: the row stays locked during the read-only
   reconcile call, so a crashed executor leaves it for another one.
2. **Outcome of `execute` (AS-12, AS-13).** 200 + valid effect-result → as reported. Anything else (non-200,
   invalid body, timeout, broken connection): the executor reads the execution state — still `reserved` → T12a
   (`refused_before_dispatch`, the attempt counts); `dispatching` → T13 (`outcome_unknown`). The dispatch CAS makes
   this safe if the connector is still running: a late `dispatch` of a completed execution is refused.
3. **Connector answers of `execute`.** 401 service JWT or execution token; **409** for a replayed `jti` (AC-M8-04 asks
   409; the local cache reason is `replay`) and for a refused `dispatch`; 422 args hash or args schema; 423 kill
   switch (from `dispatch`); 403 when the token's tool/connection/resource does not match the connector, or the
   requester's `propose` grant is gone in the current bundle (EX-14); 503 when `dispatch` cannot be reached (EX-16).
   After a successful `dispatch` every error is `outcome_unknown` in a 200 effect-result.
4. **Provider idempotency.** The fake provider dedupes by `(tenant, idempotency_key)` (one account per tenant
   connection); `no_dedupe` turns it off. Reconcile looks up by `intent_id`, which the connector passes as the
   provider's client reference: the reconcile body (AS «Контракт коннектора») carries no tenant.
5. **Fake provider modes.** `PUT /_test/provider/mode {"mode": "<m>[,no_dedupe][,slow:<ms>]", "count": n?}`: the mode
   applies to the next `n` calls (default: until changed), then `ok`. Journal row per call
   `(tenant, intent_id, execution_id, idempotency_key, recipient, text_hash, attempt, outcome, at)`; effect =
   outcome `ok` or `commit_then_drop`; a deduplicated call is logged as `duplicate`.
6. **Key files.** Executor: `GXG_EXEC_SIGNING_KEYS_FILE` = `[{"seed", "not_before"}]`, signs with the newest key whose
   `not_before` has passed (AS-10: published ≥ 15 min before first use). `keys rotate` adds a key with
   `not_before = now + 15 min` to the key file and its public JWK to the JWKS file; `keys remove --kid K [--compromised]`
   removes it from both, and with `--compromised` releases every `reserved` execution (`gxg_release_reserved`, the
   T10 move without the lease wait, AS-10) as the executor role. `api` re-reads the JWKS file on change; its dispatch
   check uses the current file (a removed `kid` is refused at the next dispatch). Connectors: kit cache ≤ 10 min (AS-09).
7. **No Approval Service on the M5 stand.** `demo` starts read-only (no `propose`, no port 8081) when
   `DEMO_APPROVAL_URL` is not set, which is the stand. AC-M8-13 runs there: `execute` is in no `tools/list` on any
   path; the `unified_pdp` deny rule is proven on a probe tool registered for the duration of the test only
   (EFF-02: `execute` is never registered in ContextForge). The bootstrap no longer registers the M5 decoy.
8. **`max_attempts` per tool** («Политика tool») has no field in `approval-policy.schema.json`; the DDL default 3 is
   used; the AC-M8-08 `max_attempts = 2` case sets the column in test setup. `pending_owner_decision`.
9. **Worker probes.** `executor` has no HTTP port; it refuses to start without its DB role, keys and a valid bundle.
   `/healthz`/`/readyz` of workers stays in the M7 backlog item (sweeper, mcp → M10/M14).

## Deliberately not done

- Webhooks (`webhook` outbox topic) — M10; UI/manual T18 — M9; admin API for switches — M10 (tests use `gxg_admin`).
- TLS on internal URLs is configuration (httpx verifies by default); no mTLS (AS-07 SHOULD).
- Approval Service on the M5 stand (decision 7).
