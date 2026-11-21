# M3 cycle 2 — ponytail-review of `18b3498..f54b9f1`

Reviewer: independent critic, cycle 2. Code commit `f54b9f1`; `19c6f14`, `fc8d17d` are executor reports only
(`git diff --stat f54b9f1 fc8d17d`: `reports/conformance/M3.json`, `reports/mutmut/M3.txt`).
Load: local reference connector, test traffic only. Findings are judged against the specs, the threat model and the fact
that `templates/python-fastmcp/` is cut from this code.

Re-run by the critic (clean clone of `f54b9f1`, empty `UV_CACHE_DIR`, `/usr/bin/python3.12` 3.12.3, `dockerd` started by hand):

| Command | Result |
| --- | --- |
| `uv sync --frozen && uv run pytest` (root) | 728 passed, 0 skipped (5 warnings are the M0.1 socket-block tests, intended) |
| `cd connectors/demo && uv sync --frozen && uv run pytest` | 56 passed |
| `ruff check` / `ruff format --check` on `connectors/demo` | clean |
| `scripts/ci/mutmut.sh` | exit 0. `connectors/demo`: 749 mutants, 710 killed, 39 survived, all 39 listed in `mutmut-equivalent.txt`. **6 of the 39 are not equivalent and no test kills them** (finding 1) |
| `DOCKER_BUILD_FLAGS="--secret id=ca-bundle,…" connectors/demo/docker_smoke.sh` | OK: `/readyz` 200 with bundle, 503 without, 503 foreign key; `DEMO_TEST_CONTROLS=1` and `DEMO_TEST_ISSUER_KEY` → exit 2; no `testctl` in the image |
| Live container (real image, `GXG_PROFILE=test`, `DEMO_PROVIDER_TOKEN=demo-secret-<random>`), raw JSON-RPC over HTTP | no bundle: 3 principals × 4 calls + 3 cursor calls → 15 identical `temporarily_unavailable` bodies; with bundle: expected results; 15 audit lines each run, no token, no token name |
| Same live probe with mutant `main__mutmut_1` (`basicConfig(level=None, …)`) built into the image | `docker_smoke.sh` OK, **0 log lines, 0 audit lines** in `docker logs` |
| CI run 44908551466 (head `19c6f14`, code = `f54b9f1`) | every job green except `grants-widening` (red outside a PR by M2 design) and `ci-ok` (needs it). Not a finding |

## What this change does

Closes the two cycle-1 majors and the minors: a readiness check before every handler (GR-12), no backend search for an
empty board set (GR-16), an `internal_error` envelope for unexpected exceptions, public kit exports for `TokenError` /
`parse_public_key`, tool definitions read from `gxg-manifest.yaml`, `typing.get_args(IndexFailure)`, `server.py` under
mutmut with a list of 39 survivors.

## Checked and fine

- **Cycle-1 major 1 (GR-12, SC-05, SC-10) is closed.** `Connector.tools()` wraps `refresh` in `ready()`, which reads
  `self.grants.version` → kit `BundleStore.current` → `temporarily_unavailable` before any handler. Order in
  `DemoTool.run`: identity → input schema → ready → handler. `test_no_bundle_refuses_every_call_alike`: nobody, reader,
  unknown tenant × boards.list, search, search on `a-secret`, note.get for a secret / public / missing id → one body,
  `sum(backend.calls) == 0`. Re-checked on the real image over HTTP (above). Invalid arguments without a bundle get
  `invalid_input` first; that discloses nothing.
- **Cycle-1 major 2 (GR-16, MCC-06) is closed.** `if not boards:` returns the empty result before `backend.search`.
  `test_nobody_does_not_reach_the_backend` now asserts `sum(calls) == 0` for nobody and an unknown tenant, with the index
  healthy and failed, and for `board_ids: []` only the `boards` listing is counted. Live: nobody, `board_ids: []`,
  unknown tenant → empty success.
- **Unexpected exception → `internal_error`.** `RuntimeError("…demo-secret-XYZ")` → `{"code":"internal_error",…}`,
  text nowhere in the reply or the log, `outcome: internal_error` (test). FastMCP `mask_error_details` not used, with
  the reason in the plan; the own `except` is needed anyway for the envelope and the audit outcome.
- **Manifest is the one source.** Names, schema paths, `timeout_s` come from `gxg-manifest.yaml` (PyYAML now a direct
  dependency, already in the lock via FastMCP). The image starts and lists 3 tools, so the manifest and the shared
  schema resolve in `/app` too.
- **Kit reuse.** `from gxg_connector_kit import TokenError, parse_public_key`; tests encode keys with `cryptography`.
- **`DEMO_STATE_FILE`** honoured (`test_state_file_comes_from_the_environment`), parents created.
- **6 of the 39 listed mutants are equivalent on reading:** `_scan` `>=`→`>` (same nanosecond), `duration_ms*1001`,
  `load_schema` keeping `..` (resolves to the same file from `connectors/demo`, `mutants/` and `/app`), `encoding`
  `"UTF-8"` / `None` (codec names are case-insensitive; UTF-8 locale in CI and in `python:3.12-slim`), `_seen` `None`
  vs `""`. Of the 27 serving-path mutants, the ones on the `:8080` server crash or move the port and are killed by
  `docker_smoke.sh` in the `demo-image` CI job; `log_level="WARNING"` is lowercased by uvicorn; `host=None` on the
  test server is uvicorn's default `127.0.0.1`. Those are acceptable as listed (wording aside, finding 2).

## Findings

### Must fix

1. **The production audit log is not tested, and its 6 mutants are listed as "equivalent" with a reason that is not true** (`connectors/demo/src/demo_connector/server.py:L156`, `scripts/ci/mutmut-equivalent.txt` entries `x_main__mutmut_1…6`)
   - **What this is:** `main()` calls `logging.basicConfig(level=logging.INFO, format="%(message)s")`. That line is the
     only thing that makes the JSON audit lines (spec «Логи и аудит», MCC-08, the method marker of AC-M3-08) reach
     stdout in the real process: they are logged at INFO, and Python's default is WARNING.
   - **Problem:** mutants `level=None`, no `level`, `format=None`, no `format`, `"XX%(message)sXX"`, `"%(MESSAGE)S"`
     survive. They are not equivalent: I built `level=None` into the image — `docker_smoke.sh` still says OK, and the
     live container writes **0** lines for 30 tool calls (normally 15 audit lines per run). The list says the log lines
     "are checked in docker logs by docker_smoke.sh / critic"; `docker_smoke.sh` greps only `startup_refused` (ERROR,
     printed even at the default level). So the mutmut gate in CI passes on a false entry. DoD-4: «выжившие
     неэквивалентные мутанты в этих модулях недопустимы»; `server.py` is in `only_mutate` by this cycle's own choice.
   - **Fix:** one automated check of the real process: in `docker_smoke.sh`, run the test-profile container (or the
     production one: a refused `tools/call` is logged too) and `grep` one JSON audit line with `"tool"` and `"outcome"`
     in `docker logs`; or a subprocess test that runs `python -m demo_connector` and asserts an INFO JSON line on stdout.
     Then remove `x_main__mutmut_1…6` from the list (or rewrite their reason as "killed by …" if the check is outside
     mutmut). About 5 lines.
   - **If we skip it:** a later edit of the logging setup silently removes the whole audit trail of every connector made
     from the template, with CI green.

### Should fix

2. **The test-controls server (`:8090`, "только `127.0.0.1`") has no test at all** (`server.py:L165-L167`; list entries `x_main__mutmut_35…45`)
   - **What this is:** the spec requires the test endpoint to listen only on `127.0.0.1` in the test profile.
   - **Problem:** port `8091`, port default `8000`, `"XX127.0.0.1XX"`, `Server(None)`, `test_app(None)` all survive;
     nothing starts `main()` with `DEMO_TEST_CONTROLS=1`. The list's reason is honest ("its app is tested via
     `testctl.create_app`"), but nothing pins the binding or that the server starts. Test profile only, production image
     has no `testctl.py`, so no production exposure. `origin: missed_earlier` (the line is from cycle 1).
   - **Fix:** factor the two `uvicorn.Config(...)` into a small function returning configs, assert
     `[(c.host, c.port) for c in configs(settings)]` in-process (no sockets needed); the serving `asyncio.run` stays
     listed. ~5 lines.
   - **If we skip it:** a binding change to `0.0.0.0` in a test deployment (M5 compose) is unnoticed.

3. **The empty-board-set shortcut skips cursor validation and duplicates the result literal** (`tools.py:L160-L168`)
   - **Problem:** `agent:t-alpha:reader` with `{"board_ids": [], "cursor": "garbage"}` → success `{"items": []…}`; with
     `{"board_ids": ["a-public"], "cursor": "garbage"}` → `invalid_input` (reproduced on the live image). The spec says
     the cursor is signed and bound to principal and query; here an unsigned one is accepted. No data is disclosed. The
     7-line literal repeats what the general path already builds from an empty note list.
   - **Fix:** keep the general path and skip only the backend call:
     `notes, method, timed_out = ([], None, False) if not boards else await self.backend.search(…)` after `_offset`.
     Cursor checked, literal gone (−7 lines).

### Nice to have

4. **`internal_error` leaves no diagnostic anywhere** (`tools.py:L89-L91`)
   - **Problem:** the `RuntimeError` of the test is swallowed: the audit line has `outcome: internal_error` and nothing
     else; no exception type, no traceback in any log. With a real backend an operator cannot tell a bug from an outage.
   - **Fix:** add `"error": type(exc).__name__` to the audit entry (the class name holds no provider text). 1 line.

Mapping to the rubric: 1 → `major` (Should fix: «тест не ловит мутацию», DoD-4 broken by a listed non-equivalent
survivor; spec-required audit output in the real process untested) and the DoD cap (max 6); 2–4 → `minor`.

Verdict: fix 1 (a smoke assertion and five list lines); 2 and 3 are small and can go in the same commit.
Lean: -7 lines possible.
Not checked: behaviour behind a real ContextForge (M5), network isolation SC-06 (SEC-04, M5), FastMCP tool-timeout path
(not reachable: search budget 15 s < timeout 20 s), the 24 h staleness boundary between `ready()` and `check()` inside one
call (microsecond window, no reproducible case).
