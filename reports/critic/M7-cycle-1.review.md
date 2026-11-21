# M7 cycle 1 — ponytail-review

Diff `6a75c62..7c68ff8` (base `claude/integration`), executor report `b752d31`. Critic, independent thread.
Specs: the branch's `docs/specs/*` and `schemas/approval-service.openapi.yaml` equal those of `origin/claude/admiring-planck-vzys0x` (AS-27…29 present); only `docs/backlog.md` and `docs/setup/*` differ there, not relevant here.
Load assumed: a few tenants, each with a handful of agents limited to 30 proposes per hour each (AP-06), intents kept for 1 year (RET-02). That is up to some thousands of proposes per tenant per day and a few hundred thousand rows per tenant in `intents` after a year. All `propose` calls of a tenant run one after another (per-tenant advisory lock).

What this change does: it adds the Approval Service core. The PostgreSQL schema comes from the spec, with one DB role per process. Every status change except T1 is a `SECURITY DEFINER` function that checks the move against a single transition table, increments `version` and writes `audit_events`. The `api` process (FastAPI) handles `propose` (T1: bundle checks, hash, idempotency, kill switch, quotas, policy, auto limits under one per-tenant lock), read, cancel and dispatch. The change also adds a pure policy engine (AP-01…AP-04), quotas, an `mcp` facade (`effects.status` / `effects.cancel` through `api`), a `sweeper`, and sealed-box encryption of args and preview per tenant.

## How I checked

- Local PostgreSQL 16.15 (Ubuntu package, `initdb`, Unix socket only, `GXG_TEST_PG_SOCKET_DIR`): `uv run pytest approval/tests approval/tests_db` → **363 passed**, 0 skipped, 22 s. `ruff check`, `ruff format --check`, `mypy --strict approval/tests_db`: clean. CI run 44914149094 on `b752d31`: lint, types, tests, approval-db, schema-guard, secrets, policy green; `grants-widening` red by M2 design (push without a PR); `mutmut` was still running when I finished.
- **mutmut** (`approval [tool.mutmut]`, my own full run on 7c68ff8): 1393 mutants, 1392 killed, 1 timeout (`transitions.x_rules__mutmut_174`, host loaded by my parallel runs). I re-ran it alone and it was killed. So **1393/1393 killed** for `policy.py`, `transitions.py`, `quotas.py`, `intents.py`. The executor's report says this for 2123f57 plus a re-run of 5 mutants, and I confirm the number on 7c68ff8.
- **Manual SQL mutations.** mutmut does not touch the PL/pgSQL in `migrations/versions/0001_core.py`. It does not touch the SQL string constants in `intents.py` either: the module comment says they were made constants for exactly that reason. These hold most of the key conditions of the milestone. I know of no mature mutation tool for PL/pgSQL or for SQL text. So I applied 41 one-line textual mutations to the migration and 3 to the SQL constants in a throw-away worktree, one at a time, and ran `approval/tests_db` each time (script kept outside the repo). I re-ran the kills that overlapped another run one by one. Result: 33 killed, 2 equivalent, **8 survivors that are not equivalent** (finding 1).
- **Python checks outside `only_mutate`.** 9 hand mutations in `api.py` (kind allowlist, `serves(tenant)` on GET and cancel, `jti == execution_id`, token `aud`) and `intents.dispatch` (`serves`). All 9 were killed (one more was meaningless and is not counted).
- **AC-M7-02** by reading `test_state_machine.py`: 12 states (executing split by execution state) × 18 events. A pair the table forbids must return `conflict`, or `not_found` when it is an execution event and no execution exists. Status, version, attempts, executions, audit count and decisions count must stay the same. An allowed pair must give version+1 and exactly one audit row with from/to status. I compared the expected table with T1–T18 + T12a of the spec cell by cell, and it matches.
- **AS-27**: compared the grants in the migration with the spec matrix cell by cell. `test_roles.py` checks every table × column × privilege × role in the catalogue, every function × role, and real statements per role. **AS-28**: `compose.yaml` against the table (M7 processes only).
- **Load**: an owner-role database with 500 000 history rows of one tenant. `EXPLAIN ANALYZE` of the two T1 count queries (finding 3).
- **MCP input**: `effects.status` and `effects.cancel` with `intent_id = "../../readyz"` and similar values, through FastMCP → api (finding 2).

## Must fix

None. I found no wrong result in the current code under the expected load.

## Should fix

1. **Key SQL conditions can break without any test failing** (`approval/src/gxg_approval/intents.py:58`, `migrations/versions/0001_core.py:279-301,372,239-246,260,272`)
   - **What this is:** most rules of M7 live in SQL: the tenant and requester filter of read and cancel (AS-11a), the guards of the T2 decision function, the T1 insert guard. mutmut does not mutate SQL, and no mutation check covers it.
   - **Problem:** I removed one condition at a time, and the whole suite stayed green in 8 cases:
     - `_SQL_OWN`: `tenant_id = %s` → `(tenant_id = %s OR true)`. All 363 tests pass. Principal IDs are not tied to a tenant by the schema (`^(agent|oidc):\S+$`). So the same `agent:signals` can be registered in t-alpha and t-beta. With this mutant, as the t-beta identity it reads the t-alpha intent through `effects.status`. That is the "другой tenant" case of AC-M7-09, and its test does not catch the break: every cross-tenant case in the tests also uses a different principal.
     - `gxg_cancel`: dropping `AND tenant_id = p_tenant` survives too, for the same reason.
     - `gxg_decide`: dropping `OR NOT p_approver LIKE 'oidc:%'` survives. An agent principal that is not the requester can then approve. The only test uses the requester itself, and the self-approval check catches that first.
     - `gxg_dispatch`: dropping `(x.r).intent_id <> p_intent` survives (token `sub` is not compared with the execution's intent).
     - T1 insert guard: dropping `(status = 'pending_approval') <> (approval_mode = 'human')` survives.
     - `gxg_record_auto_decision`: dropping `approval_mode <> 'auto'` survives.
     - `gxg_received_approvals`: dropping `decided_by NOT LIKE 'policy:%'` survives. `received_approvals` of an auto intent then shows 1 instead of 0.
     - Equivalent, not findings: the live-execution check in `gxg_cancel` (T6 is allowed only from `pending_approval`/`approved`, and those never have a live execution, because T7 moves the intent to `executing` in the same transaction). The `version` CAS in `_gxg_move`: every caller has already locked the row and compared the version.
   - **Fix:** one test per survivor, each a few lines: the same principal ID in two tenants for GET and cancel (byte-identical 404), an `agent:` approver that is not the requester, a token whose `sub` is another intent, two insert-guard rows, and `received_approvals` of an auto intent. Then a committed SQL-mutation check, because no ready tool exists: a list of one-line mutations of the key SQL conditions run against `tests_db`, with results in `reports/mutmut/M7.txt`. Also add `api.py` and `mcp.py` to `only_mutate`: they hold the AS-06/AS-11a checks, and my 9 hand mutants there were all killed, so this is cheap. Delete the unreachable live-execution check in `gxg_cancel`, or write down why it stays.
   - **If we skip it:** a later change to one of these lines (M8–M10 will touch these functions) can open cross-tenant reads or let agents approve, and CI stays green.

2. **`effects.status` takes any text as a URL path** (`approval/src/gxg_approval/mcp.py:59,62`)
   - **What this is:** the MCP facade puts the agent's `intent_id` into `f"/v1/intents/{intent_id}"` and calls `api` with its own service JWT.
   - **Problem:** `intent_id` is never checked to be a UUID, so `../` reaches other paths. As `agent:t-alpha:bot`, `effects.status("../../readyz")` returned `{"ready": true, "reasons": []}`, and `"../../.well-known/gxg-exec-jwks.json"` returned the JWKS. That is not an intent-status document, and the readiness reasons describe the bundle state. `effects.cancel("../../healthz")` crashes with `KeyError: 'version'`. Today the only reachable GETs are harmless. But every GET that `api` gains for `approval_mcp` later becomes reachable for any agent with any path.
   - **Fix:** in `run()`, `uuid.UUID(intent_id)`, and return `not_found` (same bytes) when it fails; a test with `"../../readyz"`. 3 lines.
   - **If we skip it:** the agent-facing tool has an input that is not validated and does not keep its output contract.

3. **Every propose scans the tenant's whole history while holding the tenant lock** (`approval/src/gxg_approval/intents.py:45-55`)
   - **What this is:** `_SQL_COUNTS` (quotas) and `_SQL_AUTO_USED` (auto limits) run under `pg_advisory_xact_lock(tenant)`, so all proposes of a tenant wait for them one by one.
   - **Problem:** `_SQL_COUNTS` filters only `WHERE tenant_id = …`, with all other conditions inside `FILTER`. `_SQL_AUTO_USED` has no index that fits. Both become sequential scans. With 500 000 history rows of one tenant (one year at about 1 400 proposes a day), the two queries take 85 ms + 57 ms (measured, `Parallel Seq Scan on intents`). That caps a tenant at about 7 proposes per second, and the cost keeps growing until the RET-02 purge. Everything that waits on the lock waits that long too.
   - **Fix:** split the counts so that the existing indexes are used: per-hour and oldest age with `requester_principal = $r AND created_at > now() - 1h` (index `(tenant_id, requester_principal, created_at)`), pending counts with `status = 'pending_approval'` (index `(tenant_id, status, expires_at)`), and auto usage with `created_at > now() - 1 day` (one index `(tenant_id, created_at)`). No new code paths.
   - **If we skip it:** propose latency grows with history. A busy tenant hits HTTP timeouts at the connector while waiting for the lock.

4. **The OpenAPI comparison is custom code with no alternative written down** (`approval/tests/test_openapi.py`, `reports/plans/M7-cycle-1.md` DoD-6 table)
   - **What this is:** AC-M7-01 compares the FastAPI OpenAPI with `schemas/approval-service.openapi.yaml` in a hand-written test, about 40 lines.
   - **Problem:** a mature tool exists for this, `oasdiff` (OpenAPI diff with breaking-change rules). The DoD-6 table does not mention the comparison at all. DoD-6 requires the considered alternatives for every component the executor wrote.
   - **Fix:** add a row to the plan, for example: "oasdiff: a Go binary outside the uv toolchain; the generated document uses external `$ref`s it would have to resolve; the check is a containment check (subset), not a breaking-change diff". Or use oasdiff. One table row is enough if the reason holds.
   - **If we skip it:** DoD-6 stays open for this component.

## Nice to have

5. **Code that cannot run yet** (`approval/src/gxg_approval/api.py:39`, `config.py:46-48`, `deploy/compose.yaml` service `mcp` + secret `svc_key_mcp`, `config.py` docs for `GXG_API_URL`/`GXG_API_AUDIENCE`). `ID_PATTERN` is unused. `signing_key()` and the two env vars are used only by an `mcp` process that always exits (no production `IdentityVerifier` before M5). Compose still mounts a private key into that container. Delete them, or leave one `shortcut:` line that names M5. About -20 lines.
6. **`sweeper` and `mcp` have no `/healthz` and `/readyz`** (spec «Развёртывание»: "у каждого процесса"). The sweeper is a loop with no probe. A file heartbeat or a tiny HTTP probe is enough. M10 or M14 can take it, but it should be in the backlog.
7. **Pool exhaustion is a 500** (`api.py:126-129`). `psycopg_pool.PoolTimeout` under load becomes `internal_error` 500. The spec's retryable code is 503 `temporarily_unavailable`. One `except PoolTimeout` handler.
8. **No limit on request body size** (`api.py:139-146`, `intent-create.schema.json` `canonical_args` has no size bound). A registered connector can send a body of hundreds of MB, and `api` reads all of it into memory before validating. Internal and authenticated, so low risk. A `Content-Length` cap (for example 1 MiB) fixes it.
9. **Evidence**: `reports/mutmut/M7.txt` reports the full run on 2123f57, not on the reviewed commit. My run on 7c68ff8 gives the same 1393/1393. Next time, run it on the final commit.

## For the orchestrator (spec, not scored here)

- AS-27 says that a compromised `ui` must not be able to "подделать решение человека". The matrix of the same section gives `ui` T2–T4. `gxg_decide(p_approver, …, p_step_up)` takes the human ID and the step-up flag from the `ui` role as plain arguments. So a compromised `ui` can approve as any `oidc:` approver, including two of them for a quorum. M7 follows the matrix. Closing the gap needs a design decision for M9, for example verifying the WebAuthn or OIDC assertion outside `ui`. The owner should decide before M9.
- AS-08: a `requester.assertion` that is present is ignored today (`identity_mode = connector_attested`), because no verification mechanism is configured before M5 (ADR-0001 variant C). This is recorded in plan decision 11. M5 has to switch it to MUST-verify.

Verdict: fix 1, 2, 3 and 4 first.
Lean: -20 lines possible.
Not checked: AS-29 (no `ui`/`notifier` images yet); AS-28 beyond `compose.yaml` (no Kubernetes manifests in the repo); the CI `mutmut` job result (still running).
