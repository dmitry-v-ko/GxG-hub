# M6 cycle 4 — ponytail-review

Diff: `git diff 40c7f4c..34ceb14` (cycle 4; code is `ef87e9a` + `af3b5b9`, then only `reports/` and `docs/backlog.md`).
Base of the milestone: `origin/claude/m5-readonly-poc`. Reviewer: independent critic, did not write this code.
Load assumed: a pilot with a few agents per tenant, one wrapper replica, the KB behind one HTTPS endpoint
(sequential only per connection, audit G13), readiness probes every few seconds, manifest tool timeouts 10-15 s.

What this change does: it closes the two cycle 3 majors in the KB adapter (`connectors/kb/src/kb_connector/backend.py`):
the readiness probe is now bounded per connection (`asyncio.wait_for`, 5 s) and every `kb_get` tool error is a
miss (`not_found`). It also refuses a KB answer whose `Content-Length` is above 2 MB (G12), returns an empty search
without a KB call for a query with no word of 3+ characters (G16), makes `/readyz` 503 on any KB failure, declares
`httpx2`, documents the draft-count bit and the client-per-call shortcut, and bounds the stand's SSE helper (60 s).

Checked by running code (throwaway probe tests in the scratchpad, not committed; they reuse the fixtures of
`connectors/kb/tests/test_kb.py`; the 80 committed tests passed in the same run): P1 a large document of a
collection without a grant vs a missing id, with the size bound; P2 `/readyz` while one search of the tenant is in
flight; P3 a search whose 50-row window has 45 kB bodies; P4 G16 queries (`a_b`, `12 3`, `ёжи`, `о к`).

## Must fix

1. **The new size limit brings back the existence oracle for large documents** (`connectors/kb/src/kb_connector/backend.py:124-127, 215-220, 242-245`)
   - **What this is:** `kb.documents.get` must answer byte for byte the same for "missing" and "exists but not yours" (SC-05, GR-17). The wrapper learns the collection of a document only from `kb_get`, so it always asks the KB first and drops the row after. Cycle 4 added a response hook that raises `ContractError` when the KB's answer is above `MAX_RESPONSE_BYTES` (2 MB).
   - **Problem:** `get_document` turns only `ToolFailed` into a miss; the size refusal is a plain `ContractError` and becomes `internal_error`. Probe P1 (limit lowered to 3000 bytes, document 104 of `hr` given a 5000-char body): 104, no grant → `{"code":"internal_error",...}`; 999, missing → `{"code":"not_found",...}`. With the real limit the same happens for any document whose `kb_get` answer is above 2 MB. `kb_get` returns the whole row (`body_md`, `search_vector`, taxonomy, relations; audit table) as JSON text inside JSON-RPC, so a body of roughly 1 MB is enough. An agent can walk ids and learn which ids are large documents in other collections and tenants. The test `test_an_oversized_kb_answer_is_refused` checks only a granted document, so it does not see this.
   - **Fix:** in `get_document`, treat the size refusal like a tool error: raise a `TooBig(ContractError)` from the hook and catch `(ToolFailed, TooBig)` there (one class, one word in the `except`). Test: a large no-grant document and a missing id give the same bytes. Note the change in the audit's cycle 3/4 deviations.
   - **If we skip it:** the M2 fix of this cycle is undone for large documents: the wrapper leaks the existence of large documents in other tenants' workspaces.

## Should fix

2. **One 2 MB limit for a 50-row search window with bodies** (`backend.py:54, 235`)
   - **What this is:** `search` always asks `kb_search` for 50 rows with `include_body: true` (G9), and the same 2 MB limit applies to that answer.
   - **Problem:** Probe P3: 50 published `sales` documents with 45 kB bodies → `kb.documents.search {"query":"contract"}` = `internal_error` (not retryable), while `kb.documents.get` of each of them works. Any collection whose matching documents average more than about 40 kB (after JSON escaping, less) cannot be searched, and `kb.context.get` (built on search) fails the same way. The wrapper cuts content to `CONTENT_MAX` afterwards anyway, so the limit protects memory, not the answer.
   - **Fix:** size the search bound to the window (for example 50 × 2 × `CONTENT_MAX` plus headroom, or simply 16 MB for `kb_search`), keep 2 MB for `kb_get`/`tools/list`; one test. Or record it as a known limit in the audit and README.
   - **If we skip it:** searches over long documents fail with a generic error and nobody sees why.

3. **`/readyz` says "not ready" when the KB is only busy** (`backend.py:129, 164-168`)
   - **What this is:** the probe takes the tenant's `Semaphore(1)` like any call, and the new 5 s bound counts the time spent waiting for that lock.
   - **Problem:** Probe P2 (bound 0.5 s, KB answering each HTTP request in 0.4 s): one search in flight → `/readyz` 503 after 0.5 s, the search itself succeeds. In production: one tool call takes 4-8 HTTP round trips (cycle 3 P6), calls of a tenant queue on the lock, so a small burst keeps the lock busy longer than 5 s and the wrapper reports not ready while the KB is fine. An orchestrator then takes the only replica out of service during load. This existed before (an unbounded probe behind the same lock also failed the probe's own timeout), so it is `missed_earlier`.
   - **Fix:** do not count a busy lock against readiness: if the lock is taken, skip that connection (a call in progress is itself a check), or probe without the lock. Backlog is fine for the pilot.

4. **The evidence note about the handed commit is not exact** (`reports/conformance/M6.json` `ci_local.note`)
   - **Problem:** it says the commit handed to the critic "adds only reports/" on top of `af3b5b9`; `34ceb14` also changes `docs/backlog.md`. No effect on the result (docs only, and the critic rerun is on `34ceb14`), but B6 asked for `ci_local` on the handed commit.
   - **Fix:** name the files or record `ci_local` on the handed commit.

5. **Vendored KB migrations are still unused** (`connectors/kb/vendor/kb-migrations/*.sql`, 1958 lines)
   - **Problem:** unchanged since cycle 3 m5: no test, script or image reads them. The plan keeps them for a local KB copy after G2 and a backlog row exists, so this is a recorded decision; it still costs every clone 1958 lines of a private repository.
   - **Fix:** as in cycle 3: keep `SOURCE.md` (commit and sha256), copy the files back when G2 lifts.

## Nice to have

6. **Two tests check the same size bound** (`connectors/kb/tests/test_kb.py` `test_an_oversized_kb_answer_is_refused`, `test_response_size_bound`)
   - **Problem:** the unit test of `too_big` exists for the mutants; it is fine, but once finding 1 is fixed the end-to-end test should compare a no-grant document with a missing one, which also kills the same mutants.

## Checked and fine

- M1: `asyncio.wait_for` cancels the probe and releases the semaphore; `test_a_hung_kb_makes_readyz_fail_in_time_and_frees_the_lock` would hang without the bound (outer `wait_for` 2 s); `except Exception` keeps cancellation (`BaseException`) passing.
- M2: `ToolFailed` is raised after `_session`, so only `kb_get` tool errors become a miss; `kb_list_entity_types` errors stay `internal_error`; the test compares the full bytes of a missing id with a no-grant document.
- m4: 404, 400, 418 from the KB → `/readyz` 503 `{"status":"not_ready"}`.
- m6 (G16): `\w{3}` is Unicode-aware (`ёжи` reaches the KB, `о к` does not); `kb.context.get` goes through the same `search`.
- m7: `httpx2` declared in `pyproject.toml` and both lockfiles.
- m1, m3: recorded in README and the audit (m1) and the backlog (m3).
- Stand SSE helper: a stall now fails the test after 60 s instead of hanging the run.

Verdict: fix 1 first.
Lean: -1958 lines possible.
Not checked: the real KB (`kb_access`): its not-found form, real response sizes and its tokenizer for G16 (`a_b` reaches the KB; whether the KB finds terms in it is unknown); why the SSE session to ContextForge stalled for 38 min (only the bound was added).
