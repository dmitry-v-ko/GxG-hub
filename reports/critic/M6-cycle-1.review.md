# M6 cycle 1: ponytail-review of `origin/claude/m5-readonly-poc..6b75b6b`

Critic: independent, first cycle. Load assumed: the dev stand, one replica of each connector, one test process making calls in sequence (`stand/README.md`, `compose.yaml`). In production: several agents at once against one wrapper.

What I re-ran myself: `connectors/kb` pytest (40 passed), root `uv run pytest` (838 passed), ruff, ruff format, mypy, `uv lock --check`, `uv run mutmut run` in `connectors/kb` (670 mutants, 665 killed, 5 survived, all 5 listed as equivalent), mutmut on `server.py` in a throwaway worktree (234 mutants, 91 survived), a throwaway test with an oversized KB document, and `stand/run.sh` end to end (results in `reports/critic/M6-cycle-1.json`).

**What this change does:** It adds a KB wrapper connector `connectors/kb` with three read tools (`kb.documents.search`, `kb.documents.get`, `kb.context.get`). They check identity, then the grants bundle, then the collection grants, and only then call a fake KB that lives in the same process. It plugs the wrapper into the M5 dev stand (gateway, virtual server, PDP rules, merged grants bundle) and adds stand tests through ContextForge. The real KB (`gxg_agents_v2`) was never accessed, so the gap audit is a template and three of four AC are not done.

## Must fix

1. **Three of four milestone criteria are not done** (`docs/audits/kb-gap-audit.md`, `reports/M6-ESCALATION.md`, `reports/conformance/M6.json` `acceptance`)
   - **What this is:** M6 has to audit the existing KB MCP, connect it (directly or through a wrapper), show that the KB code was not changed, and show that the wrapper's service key cannot write.
   - **Problem:** AC-M6-01 is a template with "_не известно_" rows. AC-M6-03 and AC-M6-04 are `blocked_no_access`. AC-M6-02 passes only against our own fake KB. The report says this honestly, but the criteria are still not met.
   - **Fix:** Get read access to `dmitry-v-ko/gxg_agents_v2`. Then fill in the audit, replace `backend.py` with a client of the real KB (a local copy), and add the AC-M6-03 and AC-M6-04 tests.
   - **If we skip it:** The milestone does not prove what the intent asks for: "the existing knowledge base is available only in the allowed scope".

2. **`kb.documents.get` returns a result that breaks its own output schema when a document is large** (`connectors/kb/src/kb_connector/tools.py:216-226`, `schemas/document-get.output.json`)
   - **What this is:** The output schema says `content` is at most 20000 characters and `title` at most 300. Search items have the same title limit.
   - **Problem:** The code copies `doc.content` and `doc.title` as they come from the KB. I put a 25000-character document with a 400-character title into the backend. The MCP client then failed with `RuntimeError: Invalid structured content returned by tool kb.documents.get: '...' is too long`. The agent gets no document and no MCC-05 error. The fake KB has only short texts, so no test sees this. The real KB will have long documents.
   - **Fix:** Bound the result in the wrapper: cut `content` and `title` to the schema limits. For `get`, add `complete`/`truncated` to the output schema, or refuse with `invalid_input`/a documented error. One test with a long document.
   - **If we skip it:** The first long document in the real KB makes `get` and `search` fail for that query (MCC-04 "ограниченный размер", MCC-08 response size limits).

3. **The audit line has no `bundle_version`** (`connectors/kb/src/kb_connector/tools.py:83-112`)
   - **What this is:** One JSON audit line per call: tenant, principal, `grant_id`, outcome, `kb_calls`.
   - **Problem:** GR-14 says `bundle_version` is written to the audit. The line names the grant that allowed a read, but not the bundle version that held that grant. After a grant change, you cannot tell from the audit which policy allowed an old read. (The `demo` connector has the same gap; this new connector copies it.)
   - **Fix:** Add `"bundle_version": self.grants.version` to the entry once the bundle is checked. One line, plus an assertion in `test_audit_lines_exactly`.
   - **If we skip it:** The audit of a KB read cannot be tied to the policy version (GR-14, GR-07).

## Should fix

4. **`server.py` is not mutation-tested, and the reason given is wrong** (`connectors/kb/pyproject.toml:35-42`, `scripts/ci/mutmut.sh:7`)
   - **What this is:** `server.py` picks the identity verifier (`NoVerifier`, test issuer, ContextForge), guards the test profile, and loads the bundle.
   - **Problem:** `pyproject.toml` leaves it out of `only_mutate` because "demo's suite kills" the mutants of demo's copy. Demo's tests never import kb's copy. I ran mutmut on it: 234 mutants, 91 survived. For example, `verifier = None` on the production path (`x_build__mutmut_33`) and `ContextForgeVerifier` with its arguments dropped (`x_build__mutmut_36`) both survive. The unit tests only use the test issuer, and only the stand covers the production path. Also, `scripts/ci/mutmut.sh:7` says kb mutates `server.py`, which is not true.
   - **Fix:** Either move `NoVerifier`, `parse_keys` and `BundleFiles` into `gxg-connector-kit` (killed there once), or mutate `server.py` and add one unit test that builds the app with `KB_IDENTITY_KEYS` and checks a real `ContextForgeVerifier` token. Fix the comment.
   - **If we skip it:** A broken production verifier wiring is caught only by the 25-minute stand run, not by CI's unit tests.

5. **The KB port cannot carry the real KB without a rewrite** (`connectors/kb/src/kb_connector/backend.py:52-69`, `tools.py:164-175`)
   - **What this is:** `Backend.search(tenant, collections, query) -> list[Document]` is synchronous and returns every match with full content. `documents_search` builds the whole list again for every page and then slices it.
   - **Problem:** A real KB client behind this port does a network call. Because the port is synchronous, `asyncio.wait_for` in `KbTool.run` cannot stop it: a hung KB blocks the event loop for all calls, and the `timeout_s` from the manifest never fires. A query that matches 10,000 documents pulls all 10,000 full texts on every page. The README says "after the audit only `backend.py` changes", but the port shape and `documents_search` will have to change too.
   - **Fix:** Make the port `async`, and give `search` a `limit` and cursor (or offset) that the adapter passes to the KB. Keep the per-item `check`.
   - **If we skip it:** After access, the "tools stay as they are" plan fails, and the timeout and size limits (MCC-08) do not hold.

6. **M5's report now depends on M6's tests** (`stand/run.sh:32-34`, `stand/tests/test_sc.py:55`, `reports/conformance/M5.json`)
   - **What this is:** `run.sh` writes `M5.json` and `M6.json` from the same three JUnit files. `test_sc01` now expects the kb tools in every team's list.
   - **Problem:** One failing kb test makes `M5.json` `all_passed: false`. `M5.json` in this commit was rewritten with 35 tests (29 M5 + 6 M6) at an M6 commit, so it is no longer the report of the M5 milestone. The `|| true` after the M6 report hides an M6 failure whenever M5 happens to pass. The CI job still uploads only `M5.json` (`ci.yml:205-209`), so M6 evidence never reaches CI artifacts.
   - **Fix:** Upload `M6.json` in the `m5-stand` job too. Drop `|| true`, and let one exit code cover both reports. Do not rewrite `M5.json` in M6 commits, or say in M6.json that M5.json is regenerated.
   - **If we skip it:** The evidence of a closed milestone (M5) changes every time a later milestone runs, and an M6 failure can go green.

7. **The CI stand job may run past its 30-minute limit** (`.github/workflows/ci.yml:197`)
   - **What this is:** `m5-stand` has `timeout-minutes: 30`.
   - **Problem:** The suite now takes about 445 s per run instead of 336 s (the executor's M6.json and my rerun show about 115 s more per run, from the kb tests). Three runs come to about 22 min of tests, plus two image builds, three `up --wait`, a bootstrap and a ContextForge restart. My local run took the time recorded in the JSON report. On a cold GitHub runner this is close to the limit.
   - **Fix:** Raise the limit to 45 min, or make the kb stand tests cheaper: they call `docker compose logs` on every audit read (about 30-45 s per test).
   - **If we skip it:** `all-green` becomes flaky on timeouts.

8. **`merged_grants` silently overwrites groups with the same name** (`stand/init.py:51-67`)
   - **What this is:** It merges the grants files of demo and kb per tenant for the stand bundle.
   - **Problem:** `groups = {k: v for d in docs ...}`: if demo and kb both define `group:readers` with different members, kb's list replaces demo's. Demo's grants then go to kb's members, with no error. Duplicate connection ids are concatenated and left to the linter. Today no fixture has groups, so nothing breaks yet. The function has no unit test.
   - **Fix:** Raise an error on a group or connection id defined twice. One small test.
   - **If we skip it:** A later fixture quietly changes who gets which grant on the stand.

## Nice to have

9. **Copies of demo code** (`connectors/kb/src/kb_connector/tools.py:65-112`, `server.py:69-123`). `KbTool.run`, `NoVerifier`, `parse_keys` and `BundleFiles` are about 100 lines copied from demo, marked `shortcut`. A security fix now has to be made in two places. Move them into `gxg-connector-kit` before the third connector (M11). This is recorded as a decision, so it is not a blocking finding.
10. **`kb_calls` is a process-wide counter** (`tools.py:72-75`). With two calls at once, each audit line counts the other's KB calls. It is marked `shortcut` and holds at the stand's load. Use a `contextvars` counter with the real client.
11. **The cursor key is per process** (`tools.py:132`). After a restart, or with a second replica, page 2 returns `invalid_input`. This is fine at one replica. Note it as a `shortcut`, like the `jti` cache.
12. **`/readyz` does not check the KB** (`server.py:136-145`). MCC-09 says "backend доступен". With the fake KB this is trivially true. The real client needs a check.
13. **Hand-rolled argument parsing in `stand/report.py:63-65`.** `argparse` from the stdlib does `--milestone` in two lines and refuses typos. As written, `--milestone` without a value takes the first `label=path` as the milestone.
14. **A broken table row in `docs/backlog.md:27`**: five cells under a four-column header ("final" lands in "Источник").
15. **`kb.context.get` is our own operation, not the KB's.** The spec says to publish only operations that really exist and are verified. Keep it `pending_owner_decision` and settle it in the audit.

## Ready-made solutions check

- FastMCP, `gxg-connector-kit` (identity, grants, errors, redact), `jsonschema` and stdlib `hmac` are reused. Correct.
- The runner and server plumbing are copied instead of reused (finding 9). The ready-made place is `gxg-connector-kit`.
- The fake KB is in-memory. No ready-made alternative exists without the KB's schema. A local Supabase copy is the target after access.
- `merged_grants`: `gxg-policy` takes one file per tenant, and no installed tool merges them. Custom code is justified, but it needs the duplicate check (finding 8).
- `report.py` argument parsing: use `argparse` (finding 13).

Verdict: fix 1, 2, 3 first; then 4-8.
Lean: -100 lines possible.
Not checked: the real KB (no access); a green GitHub CI run on this head (runners not checked).
