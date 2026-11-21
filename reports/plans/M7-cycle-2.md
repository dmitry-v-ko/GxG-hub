# M7 cycle 2 — plan (ponytail full)

Input: `reports/critic/M7-cycle-1.json` (score 6.0) — `required_improvements` 1–6 are the closed checklist of this
cycle; `threat_model` of the same report is the attack-path list. Process: `docs/implementation-plan-v1.md`
«Ускорение процесса (вторая редакция)» — owner thread, self-check in `reports/conformance/M7.json` (`self_check`),
critic subagent with fresh context. Threshold 9. Specs are not changed in this cycle.

Base: `dd4f060` (cycle 1 + critic report) + fresh `docs/` and `reports/GOAL.md` from
`origin/claude/admiring-planck-vzys0x` (three-way merge of the four changed files, no conflict).

## Checklist → change (rung of the ladder)

| # | Checklist item | Change | Rung |
| --- | --- | --- | --- |
| 1 | 8 SQL survivors | tests only: one shared principal `agent:shared` registered in both tenants (GET and cancel, byte-identical 404, row unchanged — via API and MCP); `agent:` approver that is not the requester → `permission_denied`; `gxg_dispatch` with another intent's id → `not_found`; two insert-guard rows that break only status↔mode; `gxg_record_auto_decision` on a `not_required` intent → `conflict`; `received_approvals` of an auto intent = 0 | existing fixtures |
| 2 | committed SQL mutation check | `scripts/ci/mutate_m7_sql.py`: list of one-line mutations of the key conditions in `migrations/versions/0001_core.py` and of the SQL constants of `intents.py`, one at a time, `pytest -x approval/tests_db`, file restored; equivalents listed in the script with the reason; called by `scripts/ci/mutmut.sh` (the PostgreSQL it already starts); results in `reports/mutmut/M7.txt`. `api.py`, `mcp.py` → `[tool.mutmut] only_mutate`. Unreachable live-execution check in `gxg_cancel` deleted | copy of the M2 pattern (`mutate_m2.py`) |
| 3 | mcp `intent_id` | `uuid.UUID(intent_id)` in `run()`, URL built from the parsed canonical form; failure → `GxgError("not_found", "intent not found")` (same bytes as api's 404); test with `../../readyz`, `../../.well-known/gxg-exec-jwks.json`, `../../healthz` | stdlib |
| 4 | T1 counts on indexes | `_SQL_COUNTS` = two sub-selects: per-hour window `tenant_id, requester_principal, created_at > now()-1h` (existing index), pending `tenant_id, status = 'pending_approval'` (existing index); `_SQL_AUTO_USED` gets `created_at > now()-1 day` on new index `(tenant_id, created_at)`; test: 100 000 history rows, `ANALYZE`, `EXPLAIN (FORMAT JSON)` of the three queries has no `Seq Scan`; EXPLAIN ANALYZE output in `reports/conformance/M7.json` | SQL + index |
| 5 | DoD-6 OpenAPI | row in the table below | — |
| 6 | evidence on the final commit | mutmut + SQL mutation check re-run on the final code commit; that commit named in `reports/mutmut/M7.txt` and `M7.json` | — |

Minor findings of cycle 1, cheap ones only: `ID_PATTERN` deleted; `psycopg_pool.PoolTimeout` → 503
`temporarily_unavailable` (one handler + test). The rest → `docs/backlog.md`: sweeper/mcp probes (M10/M14), request
body cap (no 413 code in `error.schema.json`; needs a streaming limit, not a header check), `signing_key`/mcp compose
service until M5 (the mcp process is wired, only its identity verifier is missing; deleting and re-adding costs more
than the line it saves).

## DoD-6: ready-made solutions (components touched or added in this cycle)

| Component | Taken | Considered and not taken | Why |
| --- | --- | --- | --- |
| OpenAPI comparison (AC-M7-01), `approval/tests/test_openapi.py` | own containment test (~40 lines, PyYAML + FastAPI `app.openapi()`) | **oasdiff** (Go, OpenAPI diff and breaking-change rules) | AC-M7-01 is a containment check in one direction: every path, method, parameter, body and response code of the contract exists in the generated document with the same `$ref`. oasdiff answers another question (what breaks a client between two versions of one API): the generated document has extra FastAPI parts (`422`, `HTTPValidationError` components) that oasdiff reports as differences, the contract's `$ref`s point at sibling JSON Schema files that both documents would have to resolve the same way, and it is a Go binary outside the uv toolchain (one more pinned download in CI). Re-evaluate when the contract is versioned (breaking-change gate between releases is exactly oasdiff's job) |
| SQL / PL/pgSQL mutation check | own driver `scripts/ci/mutate_m7_sql.py` (DoD-4: own check for non-Python artefacts, critic confirmed in cycle 1 that no mature tool exists) | mutmut (Python AST only, leaves string constants alone); pgTAP (assertions, no mutation); SQLancer (finds DBMS bugs, does not mutate application SQL) | none of them mutates application SQL |
| T1 counts | two index-bounded sub-selects + index `(tenant_id, created_at)` | counter rows per requester/rule updated in T1 | counters need decrement on every status move out of `pending_approval` (8 transitions) and sliding windows; the indexes answer the same with no new write path |
| UUID check in mcp | `uuid.UUID` (stdlib) | Pydantic `UUID4` type on the tool argument | a schema error would be a FastMCP validation error with other bytes than `not_found` (AC-M7-09) |

## Not done, on purpose

- Spec text, AC, rubric, threshold: unchanged.
- `_gxg_move` version CAS stays (equivalent mutant: every caller passes a row it locked `FOR UPDATE`); listed as
  equivalent in the SQL mutation list with that reason; kept as a guard for a future caller that passes a stale row.
- Owner question from cycle 1 (compromised `ui` forging a human decision, AS-27 vs `gxg_decide`) — M9, recorded in
  `reports/progress.md` «Ждёт владельца» by the orchestrator; not scored for M7.

## Risks

- Adding `api.py`/`mcp.py` to mutmut may surface survivors → kill with tests in this cycle (DoD-4).
- The 100k-row test adds ~2 s to `tests_db`.
