# M7 cycle 1 — plan (ponytail ultra)

Input: `docs/implementation-plan-v1.md` (M7, AC-M7-01…13, DoD-1…6), `docs/specs/approval-service-v1.md`
(model, T1–T18 + T12a, AS-01…AS-29, API), `approval-policy-v1.md` (AP-01…AP-07), `effects-and-approval-v1.md`
(EFF-03/05/08/11/22), `resource-grants-v1.md` (GR-02, GR-11a/13a), `security-v1.md` (SEC-17/18/20/23/24),
schemas `approval-service.openapi.yaml`, `intent-create`, `intent-status`, `error`, `preview`.
Base: `origin/claude/integration` (M0, M0.1, M1, M2, M4) + fresh specs/plan from `claude/admiring-planck-vzys0x`.

## DoD-6: ready-made solutions first

| Component | Taken | Considered and not taken | Why |
| --- | --- | --- | --- |
| Migrations | **Alembic** 1.20 (raw SQL in `op.execute`, programmatic `Config`, no `alembic.ini`) | hand-rolled `schema_version` table; `yoyo` | Alembic is the standard; the DDL is spec-given SQL, so no autogenerate/ORM models |
| DB access | **psycopg 3** (+ `psycopg_pool`) | SQLAlchemy ORM / Core | Every consistency-critical write is a `SECURITY DEFINER` function (AS-27); an ORM would only wrap `SELECT fn(...)`. SQLAlchemy is present only as Alembic's engine |
| HTTP API | **FastAPI** 0.165 (OpenAPI out of the box, spec stack) | Starlette alone | spec stack; AC-M7-01 compares FastAPI's OpenAPI |
| Body validation | **jsonschema** against the repo `schemas/` (copies in the kit, drift test) | Pydantic models mirroring the schemas | one source of truth; Pydantic copies would drift |
| JWT / service auth / bundle / grants / canonical hash | **gxg-connector-kit** (`svc_jwt`, `exec_token`, `grants.BundleStore`, `canonical`, `errors`) | — | M1; nothing re-implemented |
| Payload encryption | **PyNaCl** `SealedBox` (X25519 + XSalsa20-Poly1305, anonymous encrypt-only) | `cryptography` AES-GCM (symmetric: `api` could decrypt, breaks AS-28 «только encrypt»); `cryptography` HPKE (not in 46.x); own ECIES (own crypto composition) | the only ready primitive that gives encrypt-only `api` and separate args/preview readers |
| MCP facade | **FastMCP** 2.14 (spec toolkit choice) | official `mcp` low-level server | spec names FastMCP; `isError` + `structuredContent` via a 10-line `ToolResult` subclass (FastMCP has no error-with-structure result) |
| HTTP client mcp → api | **httpx** (FastMCP dependency) | urllib | already installed |
| Sweeper scheduler | **stdlib loop** (`while: sweep(); stop.wait(interval)`) + `FOR UPDATE SKIP LOCKED` | APScheduler; `pg_cron` | one fixed-interval job; APScheduler adds a dependency for one `sleep`; `pg_cron` needs a server extension and superuser |
| Concurrency of T1 (AP-03, AP-05) | **`pg_advisory_xact_lock(tenant)`** | per-rule counter table + `SELECT … FOR UPDATE` | spec allows «или эквивалент»; one lock covers idempotency, quotas and auto limits |
| Metrics (AP-07) | **prometheus_client** Counter | own counter | standard; already in the lock |
| Real PostgreSQL in tests | **Docker SDK** (`docker` 7.x): `postgres:16.6-alpine` with `network_mode=none`, PostgreSQL over a **Unix socket** (bind-mounted dir) | testcontainers (tried first: its start-up resolves and probes TCP, which pytest-socket blocks repo-wide, AC-M0-01); mocks; local `initdb` | real server as required; no TCP anywhere in the tests |
| Mutation testing | **mutmut** 3.8 (`approval/pyproject.toml [tool.mutmut]`) | — | DoD-4 |

## Scope (every place the change reaches)

| Part | Files | Rung |
| --- | --- | --- |
| DDL (spec model), roles AS-27, grants, transition functions | `approval/src/gxg_approval/migrations/` (`env.py`, `versions/0001_core.py`), `deploy/roles.sql` (cluster roles, DBA once) | Alembic + SQL |
| Transition table T1–T18 + T12a (single source, installed into the DB) | `gxg_approval/transitions.py` | minimal code; the DB guard reads the installed table |
| Policy engine AP-01…AP-04 | `gxg_approval/policy.py` | minimal pure code |
| Quotas AP-05…AP-07 | `gxg_approval/quotas.py` | minimal pure code + counts in SQL |
| T1 / status / cancel / dispatch service | `gxg_approval/intents.py` | kit + psycopg |
| `api` process | `gxg_approval/api.py` | FastAPI |
| `mcp` process (`effects.status`, `effects.cancel`) | `gxg_approval/mcp.py` | FastMCP + httpx |
| `sweeper` (T5, T8; also T10, T14 — same role row of AS-27) | `gxg_approval/sweeper.py` | stdlib loop |
| Payload encryption | `gxg_approval/crypto.py` | PyNaCl |
| Per-process config, only own secrets (AS-28) | `gxg_approval/config.py`, `approval/deploy/compose.yaml` (Docker secrets per service) | env + files |
| Kit additions | `grants.py` (`TenantGrants.groups`, `.roles`), `svc_jwt.py` (`Service.tools`, AS-26), `_schema.py` (+ intent schemas) | small additions to M1 code, kit tests + kit mutmut |
| Tests: unit | `approval/tests/` (policy 12 scenarios, OpenAPI diff, crypto, compose secrets, config) | — |
| Tests: real PostgreSQL | `approval/tests_db/` (state machine, races, quotas, roles matrix, sweeper, API, MCP) | testcontainers |
| CI | `.github/workflows/ci.yml` job `approval-db` (in `ci-ok`); `scripts/ci/mutmut.sh` (+ `approval`, PostgreSQL container for the run); `check-all.sh` | — |
| Report | `reports/conformance/M7.json`, `reports/mutmut/M7.txt` | — |

## Decisions where the spec leaves room (no spec change)

1. **AS-27 «UPDATE только … для Tn» = EXECUTE on `SECURITY DEFINER` functions, no direct UPDATE.** Direct column
   UPDATE on `status` would let `gxg_api` write any status, so «переходы, которые процессу не принадлежат, ему
   физически недоступны» needs functions. Each role gets EXECUTE only on its transitions' functions; the
   functions touch only the columns of its matrix row. Table privileges granted are the INSERT/SELECT cells of the
   matrix; `PUBLIC` loses EXECUTE on functions.
2. **EFF-03 vs `gxg_api` × `decisions` = «—».** The auto-approval decision row (`decided_by = policy:<rule>@<ver>`)
   is written by `gxg_record_auto_decision()` (definer, EXECUTE for `gxg_api`), which only accepts an intent whose own
   `approval_mode = 'auto'` and that has no decision yet. `gxg_api` has no table right on `decisions`.
3. **T9 runs in `api`, but `gxg_api` × `executions` = «—».** `gxg_api` gets EXECUTE on `gxg_dispatch()` only.
4. **`gxg_mcp` reads «через api-функции».** The `mcp` process calls the `api` HTTP endpoints (`GET`, `cancel`,
   kind `approval_mcp`, AS-06, AS-11a); its DB role has only `INSERT` on `audit_events`.
5. **Execution window.** AP-04 freezes the policy result at T1, T2 needs `now + window`, the DDL has no column for
   the window → extra column `execution_window_s integer NOT NULL` (all spec names and types kept).
   `dispatch_timeout_s` is read from the bundle at dispatch (AS-26), not stored.
6. **T17** goes through the same checked move as every transition (status stays `outcome_unknown`, `version` +1,
   audit row) and queues the next `reconcile` with the executor's backoff (`NULL` = stop, manual review).
   *(Amended during the cycle: the first draft had no version bump; one rule for all moves is simpler.)*
7. **AC-M7-02, T1 × status.** `propose` on an existing row (same tenant + key): same hash → 200, row unchanged;
   other hash → 409, row unchanged. Every other (status, event) pair outside the table → `conflict`, no change.
8. **AC-M7-01 lists `dispatch` and JWKS** (M8 «Делаем»). Implemented as thin routes: JWKS from the configured
   executor public keys, `dispatch` = service JWT + `ExecTokenVerifier` + `gxg_dispatch()` (T9). Key rotation
   (AS-10) and the executor itself stay in M8.
9. **T2 role checks.** «Роль approver по bundle» is the UI's check (M9, it has the bundle); the DB function checks
   everything it can see: status, version, `args_hash`, `expires_at`, requester ≠ approver, `allowed_approvers`,
   step-up, quorum, one vote per human.
10. **Quotas.** An idempotent repeat (same key, same hash) is not a new propose and is not counted. `retry_after_s`:
    per-hour quota → until the oldest counted intent leaves the window; pending caps → 60 s
    (`shortcut: no natural deadline for a pending cap, refine when M9 shows real decision latency`).
11. **AS-08 assertion.** The kit has no production `IdentityVerifier` yet (ADR-0001 variant C lands with M5), so no
    key to check `requester.assertion` exists in config → `identity_mode = connector_attested` in the audit.
12. **GR-02 at T1.** `connection_id` must be a connection of the tenant and equal the RID's connection
    (`invalid_input` otherwise): cheap defence in depth against a multi-tenant connector mixing tenants.

13. **Terminal statuses** are not a second list: a status with no outgoing row in the transition table is terminal.
14. **AP-07 alert** is the Prometheus rule `increase(gxg_quota_rejections_total[1h]) > 3` on the spec's metric
    (approval README), not code that reads the audit log (`gxg_api` has no SELECT on `audit_events`).
15. **An effect tool without `executes_via` in the bundle** is refused at T1 (`permission_denied`): nothing could
    execute it.

## Not done, on purpose

- `ui`, `executor`, `notifier`, `admin` processes, decisions UI, reconcile loop, webhooks, key rotation — M8–M10.
  Their DB roles, grants and transition functions are in M7 (AC-M7-02, AC-M7-13 need them).
- KMS / master-key wrapping of tenant keys: keys come from per-process secret files
  (`shortcut: no KMS, add envelope wrapping when a KMS is chosen`). Crypto-shredding = delete the tenant's private key.
- RET-01/02 purge jobs, Prometheus metrics other than AP-07, `/metrics` endpoint auth — M10.
- AS-29 separate images and network policies: `ui`/`notifier` do not exist yet; compose keeps `api`, `mcp`,
  `sweeper` on an internal network without egress.
