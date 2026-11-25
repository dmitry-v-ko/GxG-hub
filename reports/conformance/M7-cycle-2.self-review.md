# M7 cycle 2 — self-check ponytail-review (executor, not the critic)

Diff `dd4f060..c6bb52f`. Load assumed as in the cycle-1 review: a few tenants, ~1 400 proposes per tenant per day,
1-year history (RET-02), all proposes of a tenant serialized by the T1 advisory lock.

What this change does: it closes the six checklist items of the cycle-1 critic. New tests kill the SQL mutants
that survived: one principal ID in two tenants, an agent approver, dispatch with another intent's id, insert-guard
rows, policy decisions, rejections. A committed driver (`scripts/ci/mutate_m7_sql.py`, 79 one-line mutations) runs
those checks from `scripts/ci/mutmut.sh`. `api.py` and `mcp.py` are now under mutmut. The mcp facade accepts only a
UUID as `intent_id`, and the T1 count queries are bounded by indexes.

## Must fix

None found.

## Should fix (fixed in this cycle)

1. **CI mutmut job would run out of time** (`.github/workflows/ci.yml` job `mutmut`)
   - **What this is:** one job runs every mutmut section and now also the SQL mutation driver.
   - **Problem:** approval grew from ~1 400 to ~1 630 mutants (api, mcp), and the SQL driver adds ~80 runs of
     `approval/tests_db`. Locally that is ~45 min + ~25 min on 4 cores, on top of the other sections; the old limit was 120 min.
   - **Fix:** `timeout-minutes: 240` (done).
2. **mcp rebuilt the api's error and lost nothing only by luck** (`approval/src/gxg_approval/mcp.py`)
   - **What this is:** mcp turned the api's error JSON back into a `GxgError` and serialized it again.
   - **Problem:** five mutants of that rebuild survived: `retryable`/`retry_after_s` could be dropped and no
     test noticed. The rebuild had no purpose: the api already answers with an `error.schema.json` document.
   - **Fix:** pass the document through (`Refused`), and let FastMCP derive the text content from
     `structuredContent` (deleted the hand-made `TextContent`/`json.dumps`). Done.
3. **The summary line of mutmut.sh said OK when the SQL check failed** (`scripts/ci/mutmut.sh`). An extra line
   `SQL mutations: OK|FAILED` was added. The exit code was already correct.

## Nice to have (left)

4. **A non-JSON answer from api crashes the mcp tool** (`mcp.py` `call`): `r.json()` raises `ValueError`, which
   is not `httpx.HTTPError`, so FastMCP returns its generic tool error instead of `temporarily_unavailable`. This
   happens only if something between mcp and api answers with HTML (no proxy in the M7 compose). It was the same
   in cycle 1. Backlog candidate.
5. **`mutate_m7_sql.py` edits tracked files in place**: `finally` restores them on any exception, Ctrl-C
   included. Only SIGKILL leaves a mutated file, and `git status` shows it.

## Checked

- Callers of changed code: `intents._SQL_COUNTS`/`_SQL_AUTO_USED` (only `_counts`/`_auto_used`), `gxg_cancel`
  (only `intents.cancel`), `mcp._result`/`call` (only `run`), `api._responses` (route table).
- `gxg_cancel` without the live-execution check: T6 rows of the transition table are only from `pending_approval`
  and `approved`; an intent with a live execution is `executing`/`outcome_unknown` (T7 reserves and moves it in one
  transaction, T11-T14 move it out together with the execution state). The 216-pair AC-M7-02 test still passes.
- Index use: test `test_t1_counts_use_indexes_on_a_long_history` (100 000 rows, no `Seq Scan`) fails on the cycle-1
  queries; EXPLAIN ANALYZE at 100k and 500k rows in `reports/conformance/M7.json`.
- `uuid.UUID` accepts `{…}`, `urn:uuid:…`, upper case: the URL is built from the canonical form, so the api sees a
  plain UUID (test with an upper-case id).

Verdict: Ship (after the final mutmut + SQL runs on the final commit).
Not checked: CI run of the final commit (pushed; result goes into the next report commit).
