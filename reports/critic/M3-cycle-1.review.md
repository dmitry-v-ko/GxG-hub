# M3 cycle 1 — ponytail-review of `ed06707..4690309`

Reviewer: independent critic, cycle 1. Code commit `4690309`, executor report `f25f15b` (reports only).
Load: local reference connector, one process, test traffic only (M3 is "read-only, локально"). Scale is not a concern;
findings are judged against the spec, the threat model and the fact that `templates/python-fastmcp/` is cut from this code.

Re-run by the critic (clean clone of `4690309`, empty `UV_CACHE_DIR`, `/usr/bin/python3.12`, `dockerd` started by hand):

| Command | Result |
| --- | --- |
| `uv sync --frozen && uv run pytest` (root) | 722 passed, 0 skipped (50 of them are `connectors/demo`) |
| `cd connectors/demo && uv sync --frozen && uv run pytest` | 50 passed |
| `scripts/ci/mutmut.sh` | OK. `connectors/demo`: 481 mutants, 479 killed, 2 survivors, both listed as equivalent (`_scan` `>=`→`>`, `duration_ms*1001`) and both are equivalent on reading |
| `DOCKER_BUILD_FLAGS="--secret id=ca-bundle,src=…" connectors/demo/docker_smoke.sh` | OK: `/readyz` 200 with bundle, 503 without, 503 with a foreign key; `DEMO_TEST_CONTROLS=1` and `DEMO_TEST_ISSUER_KEY` → exit 2; no `testctl` in the image |
| Live container, `GXG_PROFILE=test`, `DEMO_PROVIDER_TOKEN=demo-secret-<random>`, 100 MCP calls over real uvicorn | token, its name, queries, note texts, `eyJ` — none in `docker logs` (205 lines) or in responses |
| CI run 44903221571 on `4690309` | every job green except `grants-widening` (red by M2 design outside a PR) and `ci-ok` (needs it). Not a finding |
| `ruff check connectors/demo` | clean |

## What this change does

Adds the `demo` connector, stage B: three read tools (`demo.boards.list`, `demo.notes.search`, `demo.note.get`) on
FastMCP 4.1.8 over Streamable HTTP, synthetic data for two tenants from a fixed seed, `/healthz` and `/readyz`, a test
control endpoint, and a Docker image. Identity comes from the kit's `StubVerifier` (test profile only; production has a
verifier that refuses every call), grants from the kit's `BundleStore`, bundles in tests are built by `gxg-policy build`.

## Checked and fine

- **Reuse of M1 and M2, not copies.** Identity: `gxg_connector_kit.identity.StubVerifier`/`StubIssuer`. Grants:
  `BundleStore` (`check`, `allowed`, signature, rollback floor, 24 h). Errors: `GxgError.to_dict()`. Logs: kit `redact`.
  Bundles: `gxg_policy.cli` `build` in tests and in `docker_smoke.sh`. No second verifier, resolver or bundle parser.
- **Filtering before search (GR-16).** `_readable()` decides the boards first; `Backend.search` gets only those board ids
  and never sees `a-secret` for `t-alpha:reader`. Every returned item is checked again with `check()`
  (`test_every_search_item_is_checked_again` feeds a sloppy backend). `scan`'s delay is split over the *readable* boards,
  so time does not count hidden boards. Adding matching notes to `a-secret` leaves all pages byte-equal (re-run).
  One gap for callers with no boards at all: finding 2.
- **`not_found` byte-equal (SC-05) when the bundle is loaded.** Hidden note, other tenant's note, missing note → the same
  `{"code":"not_found","message":"not found","retryable":false}` in `structuredContent` and `content`. Same for
  `board_ids` with a hidden, a mixed, another tenant's and a missing board. Not true when the bundle is not loaded:
  finding 1.
- **Forged identity ignored (SC-03).** The verifier gets only HTTP headers and reads only `x-gxg-test-identity`.
  `tenant_id`/`agent_id`/`approved`/`user` in arguments, `X-Tenant-Id`/`X-Forwarded-User*` headers and `_meta` give
  identical results (test compares the whole response).
- **Bad identity (SC-04).** None, foreign issuer, expired, replayed `jti`, wrong audience, principal of another tenant,
  garbage → `unauthenticated` before any backend call (counter 0). Production profile → `NoVerifier`, every call
  `unauthenticated`.
- **Fallback only on a technical error (MCC-06).** `index` → `scan` only for `temporarily_unavailable` /
  `timeout_before_start`; other index errors re-raise (`test_other_index_errors_do_not_scan`); a valid empty result
  does not scan (counter). Method is in the audit line.
- **`DEMO_PROVIDER_TOKEN`.** The connector never reads it. Not in responses or logs (test with every logger at INFO, and
  my live-container run above).
- **Test issuer off in production (AC-M3-11).** `Settings.from_env` refuses `DEMO_TEST_ISSUER_KEY` or
  `DEMO_TEST_CONTROLS=1` unless `GXG_PROFILE=test` exactly (`prod`, unset → refused); the kit's `StubVerifier`
  additionally refuses outside `GXG_PROFILE=test`; the image has no `testctl.py`. Checked on the real image.
- **`/readyz` (MCC-09).** Only `store.readiness()`; no Approval Service, no JWKS, no reasons in the body.
- **Cursor.** HMAC-SHA256 over tenant, principal, query, boards, limit and offset; `compare_digest`; another caller,
  another query, a forged offset → `invalid_input` (test).
- **Lockfile / pins.** `fastmcp==4.1.8`, images pinned by digest, `uv sync --frozen` works from an empty cache.

## Findings

### Must fix

1. **Without a valid bundle some calls still answer, and `not_found` stops being byte-equal** (`connectors/demo/src/demo_connector/tools.py:L84-L87, L124-L131, L185-L192`)
   - **What this is:** GR-12 says: without a valid bundle the service is not ready **and refuses calls**; SC-10 lists
     "невалидный или устаревший bundle → отказ".
   - **Problem:** `DemoTool.run` calls `refresh()` but never asks the store whether it is ready. The store is touched only
     when a handler reaches `grants.check/allowed`. Reproduced with the bundle directory removed (`/readyz` = 503),
     caller `agent:t-alpha:nobody`:
     `demo.note.get` with the id of an `a-secret` note → `temporarily_unavailable`; with a missing id → `not_found`.
     So during a bundle outage anyone in the tenant can tell which hidden note ids exist (SC-05 broken). Also a verified
     caller of a tenant without data (`agent:t-gamma:reader`) gets success `{"boards": []}` and an empty search instead of
     a refusal.
   - **Fix:** in `DemoTool.run`, right after `self.refresh()`, read `self.grants.current` (the kit property raises
     `temporarily_unavailable`) — pass the store into `DemoTool` or into `Connector` and call it once before the handler.
     Two lines. Test: no bundle → all three tools, for `nobody`, `reader` and an unknown tenant, existing and missing
     note ids, return the same `temporarily_unavailable` body and backend counter 0.
   - **If we skip it:** the guarantee "hidden and missing look the same" holds only while policy is healthy, and the
     template copies the gap into every connector.

### Should fix

2. **A caller with no readable boards still reaches the backend search; with `index` down it even runs `scan`** (`connectors/demo/src/demo_connector/tools.py:L146-L159`; test `connectors/demo/tests/test_demo.py:L302-L306`)
   - **What this is:** `_readable()` says "nothing granted, no backend call", and GR-16 says the backend query is limited
     to `allowed()`; MCC-06 forbids a costly search when the answer is already known to be empty.
   - **Problem:** `notes_search` calls `backend.search(tenant, [], …)` when the set is empty. Measured: `t-alpha:nobody`
     search → `calls == {"index": 1}`; `board_ids: []` → `index` 1; unknown tenant with `index` failed →
     `{"index": 1, "scan": 1}`. In `demo` the empty list happens to mean "nothing"; in a real backend an empty filter
     very often means "no filter", and this is the code the template is cut from. The test named
     `test_nobody_does_not_reach_the_backend` asserts only `calls["boards"] == 0`, so it passes while the backend is hit.
   - **Fix:** `if not boards: return <empty result>, {"grant_id": [], "method": None}` before `backend.search` (3 lines);
     in the test assert `sum(demo.backend.calls.values()) == 0` for `nobody`, for `board_ids: []`, and with
     `index_failure` set.
   - **If we skip it:** a zero-rights caller costs a backend query (and a slow scan), and a template-made connector over a
     real API can return everything for "no boards allowed".

3. **The manifest is copied into code** (`connectors/demo/src/demo_connector/tools.py:L103-L122`)
   - **What this is:** schema paths and timeouts are written in `tools()` and again in `gxg-manifest.yaml`.
   - **Problem:** two places must change together. A test catches drift today, so nothing is wrong now; the code is just
     longer than reading `gxg-manifest.yaml` (PyYAML is installed) and mapping `name → handler`.
   - **Fix:** load the manifest, take `input_schema`, `output_schema`, `limits.timeout_s` from it; keep only the
     handler map. About −6 lines, and the shared-schema path walk (`SEARCH_RESULT`) can resolve from the manifest path.
   - **If we skip it:** every new tool is declared twice.

### Nice to have

4. **An unexpected exception goes to the agent as raw text, without the MCC-05 envelope, and the audit line has no `outcome`** (`connectors/demo/src/demo_connector/tools.py:L88-L91`, `server.py:L117`)
   - **What this is:** only `GxgError` is caught. Any other exception is handled by FastMCP's default.
   - **Problem:** with `Backend.get_note` raising `RuntimeError("backend exploded: demo-secret-XYZ")` the reply is
     `isError: true`, `content: "Error calling tool 'demo.note.get': backend exploded: demo-secret-XYZ"`, no
     `structuredContent`; the audit line has no `outcome`. In `demo` I found no input that triggers it (backend is
     in-memory), so this is not reachable today; it becomes reachable as soon as the template talks to a real backend.
   - **Fix:** `FastMCP("demo", mask_error_details=True)` (FastMCP already has it) plus `except Exception:` →
     `GxgError("internal_error", "internal error")` envelope and `outcome: internal_error`. ~4 lines.
   - **If we skip it:** provider error text (URLs, ids, sometimes credentials) can reach the model in the derived connectors.

5. **Imports from the kit's private module** (`connectors/demo/src/demo_connector/server.py:L29`)
   - **What this is:** `from gxg_connector_kit._jwt import TokenError, parse_public_key`.
   - **Problem:** the reference connector (and the template made from it) depends on an underscore module of the kit;
     a kit refactor of `_jwt` breaks every connector. Concrete use: `DEMO_BUNDLE_KEYS` parsing and `NoVerifier`.
   - **Fix:** export `parse_public_key` and `TokenError` (or an `unauthenticated` helper) from a public kit module; the
     connector imports that. Kit-side change of one line each.
   - **If we skip it:** private API spreads into each new connector.

6. **`server.py` is outside mutmut; one config line is untested** (`connectors/demo/pyproject.toml:[tool.mutmut]`, `server.py:L60`)
   - **What this is:** mutmut covers `tools.py` and `backend.py`. I ran it on `server.py`/`testctl.py`/`data.py` too:
     the key conditions (profile check, verifier choice, `/readyz` status) are killed; the 87 survivors are log keys,
     messages, transport flags, defaults.
   - **Problem:** one is behaviour: `env.get("XXDEMO_STATE_FILEXX", …)` survives, so no test notices that
     `DEMO_STATE_FILE` is ignored (the GR-13a rollback floor would silently move to the default path).
   - **Fix:** one assertion that `Settings.from_env({... "DEMO_STATE_FILE": p})` keeps `p` / the floor file appears there.
   - **If we skip it:** an operator's state-file location can be lost without a red test.

7. **`testctl.FAILURES` repeats `backend.IndexFailure`** (`testctl.py:L14`) — `typing.get_args(IndexFailure)`; −1 line.

Mapping to the rubric: 1 → `major` (Must fix, "прочие баги": GR-12 MUST not met, but no data, count or other tenant's object leaks — the only signal is whether an id the caller already holds exists, and only during a policy outage; not `critical`); 2 → `major` (MUST of GR-16/MCC-06, test checks not what it names); 3 (Lean) and 4–7 → `minor`.

Verdict: fix 1 and 2 first.
Lean: -7 lines possible.
Not checked: behaviour behind a real ContextForge (M5), network isolation SC-06 (SEC-04, M5), tool-timeout path of
FastMCP (not reachable: search budget 15 s, scan 2 s, timeout 20 s).
