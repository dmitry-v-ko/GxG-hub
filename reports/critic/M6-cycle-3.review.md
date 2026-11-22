# M6 cycle 3 — ponytail-review

Diff: `git diff d6161b7..93a049f` (cycle 3; the docs/ sync from the planning branch is not reviewed as M6 code).
Base of the milestone: `origin/claude/m5-readonly-poc`. Reviewer: independent critic, did not write this code.
Load assumed: a pilot with a few agents per tenant, one wrapper replica, the KB behind one HTTPS endpoint
(`kb.agents.gxg-labs.com/mcp`, sequential only per the audit), readiness probes every few seconds (compose: 3 s).

What this change does: the in-process fake KB is replaced with a real MCP client (`backend.py`) that talks to the
KB Access Layer with the tenant's read key, following the audit's contract (collection = entity type key, rows
checked against the connection's workspace, only published rows, KB errors mapped to MCC-05 codes). A fake KB
MCP server of the same contract (`fake_kb.py`) is used by the unit tests over ASGI and runs as its own service on
the dev stand. The cycle also closes N2-N4, adds 16 vendored KB migrations (unused) and the gap audit.

Checked by running code (throwaway probe tests in the scratchpad, not committed; they reuse the fixtures of
`connectors/kb/tests/test_kb.py`): P1 hung KB during `/readyz`, P2 `kb_get` tool error, P3 hidden drafts and the
`complete` flag, P4 `/readyz` on a 404 from the KB, P5 two tenants on one KB server, P6 HTTP requests per call,
P7 an odd entity type key.

## Must fix

1. **A hung KB freezes `/readyz` and then blocks every call of that tenant** (`connectors/kb/src/kb_connector/backend.py:86-90, 114-128, 147-162`; `server.py:137-143`)
   - **What this is:** `/readyz` asks every connection for `tools/list`. The HTTP client has a 5 s connect timeout and no read timeout (`Timeout(None, connect=5)`); tool calls are bounded by `asyncio.wait_for` in `tools.py`, the readiness probe is not bounded by anything.
   - **Problem:** If the KB accepts the connection but does not answer (stuck process, network black hole), `ready()` waits forever while holding the tenant's `Semaphore(1)`. Probe P1: KB delay 30 s, `ready()` started, then the KB "recovers" (delay 0): the next `kb.documents.search` of that tenant waits on the lock and returns `temporarily_unavailable` after the full 15 s manifest timeout, `ready()` still not done. With a real hang this lasts until the wrapper restarts; every new probe (compose: every 3 s, its client gives up after 3 s, the server handler is not cancelled) adds another waiter on the lock.
   - **Fix:** Bound the probe: `await asyncio.wait_for(self._session(tenant, names), CONNECT_TIMEOUT_S * 2)` (or a read timeout in `http_client`) and treat `TimeoutError` as not ready. One test: `fake.delay_s = 60` → `/readyz` 503 within the bound, then with `delay_s = 0` a search succeeds.
   - **If we skip it:** one stuck KB request turns a tenant's KB tools into timeouts until someone restarts the pod, and the readiness signal that should show it never answers.

2. **A KB that answers a missing id with a tool error becomes an existence oracle** (`backend.py:137-139, 201-209`; contract row «`kb_get` → `null` / tool error «not found» → `not_found`» in `docs/audits/kb-gap-audit.md`)
   - **What this is:** `kb.documents.get` must answer byte for byte the same for "missing" and "exists but not yours" (SC-05). The audit's contract says a `kb_get` tool error "not found" maps to `not_found`; the audit also records that the server's not-found form is unknown ("нет в коде"; the in-process code throws "KB entity was not found." for other reads).
   - **Problem:** The adapter maps every tool error to `ContractError` → `internal_error`. Probe P2: a KB that answers id 999 with a tool error → `{"code":"internal_error",...}`, while a document of another collection (104) or another workspace (201) → `{"code":"not_found",...}`. An agent can tell "exists in another collection or tenant" from "does not exist" by id. The test `test_kb_tool_errors_and_non_json_are_internal_errors` enshrines this for GET. The deviation is not in the audit's list of deviations.
   - **Fix:** In `get_document`, a `kb_get` tool error → `None` (so `not_found`), as the contract says (or match the not-found text inside the adapter and keep other tool errors as `internal_error`). Test: fake answers a missing id with a tool error → the same bytes as for 104.
   - **If we skip it:** against a KB that signals "not found" as an error, the wrapper leaks which ids exist in other tenants' workspaces.

## Should fix

3. **The `complete` flag counts hidden drafts** (`backend.py:221-225`; GR-16)
   - **What this is:** the KB has no status filter, so drafts and other-workspace rows come back in the 50-row window and are dropped in the wrapper (Q3, G6). `complete` is `false` when the window was full.
   - **Problem:** Probe P3: query "contract" → items 101, 102, `complete: true`; add 60 matching drafts in `sales` → the same two items, `complete: false, partial_reasons: ["truncated"]`. A reader learns that at least ~48 hidden drafts match. Drafts can also push published rows out of the window entirely (then the answer is empty but "truncated"). GR-16 forbids revealing hidden matches; here it is one bit, inside a collection the reader is granted.
   - **Fix:** smallest honest fix: record it as a known GR-16 limit of the Q3 default (README "Что сознательно не сделано", audit deviations) and ask for a status filter in `kb_search` (owner, with Q3). No code until the KB side is known.
   - **If we skip it:** a small, undocumented count leak of drafts.

4. **Unbounded KB response body; the audit says otherwise** (`backend.py:122-124`; audit «Отступления … лимит чтения тела HTTP-ответа (G12) — только `max_sse_event_size` SDK (1 МБ)»)
   - **Problem:** the SDK limit applies to SSE events only. A JSON response is read whole: `mcp/client/streamable_http.py:_handle_json_response` → `await response.aread()`. The fake (and the wrapper itself) answer with `json_response=True`, so no limit applies at all. A KB row with a 200 MB `body_md` is read into memory before `CONTENT_MAX` cuts it.
   - **Fix:** correct the sentence in the audit and README; either check `Content-Length` in the existing response hook and refuse above e.g. 2 MB (`ContractError`), or put it in the backlog with G12.
   - **If we skip it:** the evidence says a limit exists that does not.

5. **`/readyz` answers 500 instead of 503 on some KB errors** (`backend.py:155-159, 250-258`)
   - **Problem:** `_classify` returns the original exception when nothing matches; `ready()` catches only `GxgError` and `RuntimeError`. Probe P4: KB answers HTTP 404 → `ExceptionGroup(MCPError: Not Found)` escapes `ready()` → unhandled exception in `/readyz` (500, a traceback in the log). Still "not ready" for a probe, but the runbook row (`kb_not_ready` in the log) never appears.
   - **Fix:** `except Exception: return False` in `ready()` (cancellation is `BaseException`, it still passes). One test with `fake.http_status = 404`.

6. **Each KB call opens a new HTTP client and MCP session** (`backend.py:114-128`)
   - **Problem:** Probe P6: one `kb.documents.get` = 8 POSTs to the KB with a cold type cache, 4 with a warm one (initialize, initialized, tools/call …), each through a new `AsyncClient` (a new TCP + TLS handshake against the real HTTPS KB). Calls of a tenant are serialized by the semaphore, so per-tenant throughput is about 1 / (4 round trips + TLS). With ~100 ms to Railway that is ~2 calls/s per tenant; a burst of agent calls queues into the 10-15 s manifest timeouts.
   - **Fix:** keep one `AsyncClient` per connection (created in `__init__`, closed on shutdown); the session per call can stay. Backlog is fine for the pilot load.

7. **Vendored migrations are not used by anything** (`connectors/kb/vendor/kb-migrations/*.sql`, 1958 lines)
   - **Problem:** no test, script or image uses them (`SOURCE.md` says so: "не нужен, пока копии не используются тестами"; grep finds no reference). Q6's default is "copies only for tests/stand", but they are in no test; they are 16 unlicensed files of a private repository that every clone carries.
   - **Fix:** delete the `.sql` files, keep `SOURCE.md` with the commit and sha256 list; copy them back when G2 (base schema) lifts and a local KB is built.

8. **`httpx2` is imported directly but not declared** (`backend.py:24`, `connectors/kb/pyproject.toml:6-10`)
   - **Problem:** it arrives only through `mcp`/`fastmcp`. `pyyaml` was declared for exactly this reason; an `mcp` release that changes its HTTP stack breaks the adapter at import time.
   - **Fix:** add `"httpx2>=2.16,<3"` with the same comment.

## Nice to have

9. **G16 not handled** (`backend.py:221`): the audit's contract asks for an empty result for a query without a word of ≥3 characters (the real `kb_search` then returns arbitrary rows). Wrapper still narrows to granted collections, so it is relevance, not access. The stand test even uses `"e"` as "everything".
10. **N1 stays open** (`tools.py:91`): the bundle version in the audit line is read once, the checks read the current bundle. Executor's reason (needs a kit snapshot API, `BundleStore.check` reads `current`) holds; backlog.

Checked and fine: read key per tenant from a secret file (not in Git, `stand/secrets/kb` git-ignored), only `kb_search`/`kb_get`/`kb_list_entity_types` called, workspace check on every row (search and get), published-only, ids `^[1-9][0-9]{0,17}$` refused without a call, KB text not in logs or errors (tests with `kb-secret-text`), 429 + `Retry-After`, 5xx and transport errors → `temporarily_unavailable`, 401/403 → `internal_error` + not ready, `/readyz` refuses a key that lists a write tool, fake removed from the production image (`KEEP_FAKE_KB=0`), fake-kb on an internal network reachable only from the wrapper (stand test). Collection keys from the KB are not validated, but an odd key (P7, `a/../*`) fails closed (no item, no error). The window deviation (`limit: 50` always) is justified in the audit. N2, N3, N4 done.

Verdict: fix 1 and 2 first.
Lean: -1958 lines possible.
Not checked: the real KB (`kb_access` is not in `gxg_agents_v2`); the audit's facts about `gxg_agents_v2@f1c4d6d` (no access from this session).
