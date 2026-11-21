# M6 cycle 2 — ponytail-review

Scope: `git diff 8dcb14a..80e3f20` (cycle 2), read together with the code it touches: `connectors/kb/src/kb_connector/{tools,backend,server}.py`, `connectors/kb/tests/test_kb.py`, `connectors/kb/schemas/*`, `schemas/search-result.schema.json`, `packages/gxg-connector-kit/.../grants.py` (BundleStore), `stand/{run.sh,report.py,init.py,bootstrap.py}`, `.github/workflows/ci.yml`, `scripts/ci/mutmut*.{sh,txt}`.
Load assumed: the dev stand and a pilot, one wrapper replica, a handful of agents calling at once (README: one replica, `shortcut` for kb_calls and cursor key).

What this change does: the KB port (`Backend`) becomes async and paged: `search(..., offset, limit)` returns one page and a "more" flag, so the manifest timeout can now cancel a slow KB call, and the wrapper no longer fetches every match per page. KB titles and texts are cut to the output-schema limits (`truncated` added to `kb.documents.get`), every audit line carries `bundle_version`, `server.py` is now under mutmut with a production-verifier test, `/readyz` gets a KB hook, and the stand scripts write M5.json from M5 tests only, fail on any red run, and refresh ContextForge's stored tool schemas on every bootstrap.

## Must fix

None found in the cycle 2 diff.

## Should fix

1. **A long KB document id breaks `kb.context.get`** (`connectors/kb/src/kb_connector/tools.py:256-258`, `schemas/context-get.output.json:17`)
   - **What this is:** context.get lists the documents it used, with their KB ids.
   - **Problem:** ids are taken from the KB as they are. The search result allows ids up to 256 chars, the context output only 64. Probe: a sales document with a 100-char id: `kb.documents.search` returns it, `kb.context.get` for the same query fails in the MCP client with `RuntimeError: Invalid structured content returned by tool kb.context.get: ... is too long`. The agent gets neither context nor an MCC-05 error. Same class as cycle 1 F2, which this cycle fixed for titles and texts only. `collection` (64) and `source_ref` (512) are also passed through unchecked.
   - **Fix:** one limit for KB ids in all three output schemas (or skip/refuse an item whose id or collection does not fit, as a KB-data error), plus one test with a long id. An id must not be cut: a cut id points at nothing.
   - **If we skip it:** depends on the real KB's id format (unknown until the audit). UUIDs (36) fit. Existed in cycle 1 (`missed_earlier`), so backlog.

2. **The audit's `bundle_version` can differ from the bundle that made the decision** (`tools.py:91`, `tools.py:159-164`, `tools.py:219-225`)
   - **What this is:** each call records the bundle version once, before the handler, then the handler checks grants against `self.grants`, which reads the store's current bundle on every check.
   - **Problem:** the port is now async, so another call can run while this one waits on the KB. Case: call A records `bundle_version v1`, awaits `get_document`; call B's `ready()` loads v2 (a grant narrowed); A's final `check` uses v2. The audit says v1 allowed/denied what v2 decided. Before this cycle the fake was synchronous and nothing could interleave. Decisions stay correct (the final check always uses the newest bundle); only the GR-14 trace is off.
   - **Fix:** take the bundle once per call (`bundle = store.current`) and check against it, and record its version. Or record the version after the last check.
   - **If we skip it:** a rare, wrong trace during a bundle swap under concurrent calls.

3. **`Backend.ready()` is synchronous** (`backend.py:50-52`, `server.py:137`)
   - **What this is:** the new /readyz hook for the KB probe (MCC-09).
   - **Problem:** the README says the port "is already what the network client must be", but a real probe is network I/O. Case: a KB client implements `ready()` with a blocking HTTP call; /readyz then blocks the event loop of every tool call for the probe's timeout. Making it async later changes `server.py` too.
   - **Fix:** `async def ready()` and `await connector.backend.ready()` now (two lines).
   - **If we skip it:** small rework after access.

4. **The duplicate-id check in `merged_grants` has no test** (`stand/init.py:57-60`)
   - **What this is:** cycle 1 F8 asked to refuse a group or connection id defined by two connectors, with one test.
   - **Problem:** the check is there, the test is not. Case: a later edit that drops the connection part of `names` passes every test.
   - **Fix:** one test with two tiny fixture files sharing a group name.

## Nice to have

5. **CI artifact name** (`.github/workflows/ci.yml:209`): the artifact is still called `m5-conformance` but holds M6.json too. Rename to `stand-conformance`.

6. **`test_backend_is_fast_by_default`** (`connectors/kb/tests/test_kb.py`): a timing test (< 0.5 s) to pin `delay_s == 0`; the first assertion already does it. The timing part can go (~10 lines).

Checked and fine: timeout now really cancels a slow KB call (async sleep, test with delay 5 s and timeout 0.2 s); search sends `offset/limit` to the KB and the cursor is still HMAC-bound; per-item re-check stays; `truncated` computed on the full text; titles cut in all three tools; `bundle_version` asserted in `test_audit_lines_exactly` and absent before the bundle check; production `ContextForgeVerifier` test (aud, tool, args, unknown key, test issuer refused); `/readyz` reasons equal "not ready" in the kit (`grants.py:248-254`); run.sh no longer hides a red M6 run; M5.json is unchanged against the milestone base; mutmut kb 923 mutants, 6 survivors, all listed and each equivalence holds.

Verdict: Ship the cycle 2 diff as code (no Must fix). The milestone is still not closable: AC-M6-01/03/04 need KB access.
Lean: -10 lines possible.
Not checked: the real KB (no access); GitHub CI (every job refused by GitHub billing, not by the code).

Rubric mapping (reports/critic/M6-cycle-2.json): 1 -> B1 (missed_earlier, backlog); 2 -> N1; 3 -> N2; 4 -> N3; 5, 6 -> N4 (all minor new_regression).
