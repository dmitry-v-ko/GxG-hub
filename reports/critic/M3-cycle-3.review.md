# M3 cycle 3 — ponytail-review

Diff `f54b9f1..3bcd176` (code) plus the connected code: `server.py` (`main`, `configs`, `serve`, `build`),
`tools.py` (`DemoTool.run`, `Connector.search`), `tests/test_demo.py`, `docker_smoke.sh`,
`scripts/ci/mutmut-equivalent.txt`. Expected load: one local connector process per tenant stand, a handful of agents
(spec demo-connector-v1); logs read by `docker logs` / a line-based collector.

Re-run by the critic (Python 3.13.18, uv, `dockerd` 29.10.0 started by hand with `--registry-mirror=https://mirror.gcr.io`):

| Command | Result |
| --- | --- |
| root `uv run pytest` | 730 passed, 148 subtests |
| `connectors/demo`: `uv run pytest` | 58 passed |
| `ruff check .`, `ruff format --check .`, `mypy` (CI invocation from root) | clean |
| `connectors/demo`: `mutmut run` (fresh `mutants/`) | 771 mutants, 762 killed, 7 survived, 2 no tests; the 9 equal the 9 demo lines in `mutmut-equivalent.txt` |
| `x_main__mutmut_1..6` (new `main`: handlers `None`, filters, `basicConfig` level/handlers) | **killed** |
| `DOCKER_BUILD_FLAGS="--secret id=ca-bundle,…" connectors/demo/docker_smoke.sh` | OK: `/readyz` 200/503/503, `tools/call` → `unauthenticated`, JSON audit line on container stdout, test switches → exit 2, no `testctl` |
| Same, with `basicConfig(level=None, …)` built into the image | **FAIL: no JSON audit line on stdout** |
| Same, with the stdout handler replaced by stderr (`StreamHandler()`) | **FAIL** in `docker_smoke.sh`; `test_real_process_writes_audit_to_stdout_and_frames_to_stderr` **FAILED** |
| CI run 44913137254 (head `3bcd176`) | all jobs green except `grants-widening` (red outside a PR by M2 design) and `ci-ok` (needs it) |

What this change does: it splits `main()` into `configs()` (which servers on which host/port) and `serve()` (run
them), sends JSON log lines to stdout and internal-error stack frames to stderr, and adds two real-process checks of
the audit log: a subprocess test of the real `main()` and a `tools/call` + `docker logs` grep in `docker_smoke.sh`. It
also checks the cursor before the empty-board shortcut and adds the exception class to the `internal_error` audit line.

The cycle-2 requirement is met. The audit line is now checked in a real process twice, and both checks fail when the
audit is switched off or moved off stdout (I tried both). The six `basicConfig` entries are gone from the list, and
their mutants are killed. The remaining listed mutants are true: `configs__mutmut_19` drops `host=` and uvicorn 0.54's
default `host` is `"127.0.0.1"`, so it is the same binding. `serve__mutmut_1/2` (`Server(None)`, `asyncio.run(None)`)
crash at start, so `docker_smoke.sh` `wait_up` fails in the `demo-image` job. That is "killed outside mutmut", and the
reason line says so. The cycle-2 minors 2–4 are done too: `configs()` asserted for both profiles, cursor checked with
an empty board set (new test cases), and `error: <class>` in the audit.

## Nice to have

1. **Stack frames are a multi-line text record, not JSON** (`connectors/demo/src/demo_connector/tools.py:91-94`)
   - **What this is:** On an unexpected exception, the `demo_connector.errors` logger writes
     `internal_error <request_id> <Class>` and then one `file:line in func` line per frame, to stderr.
   - **Problem:** The spec says «JSON-логи». A line-based collector (`docker logs`, Loki, CloudWatch) turns one error
     into N separate unparsed records, and the frame lines no longer carry the `request_id`. Case: the
     `x-boom` call in the subprocess test gives one header line plus one line per frame on stderr, none of them JSON.
   - **Fix:** One JSON line: `errors.error(json.dumps({"event": "internal_error", "request_id": ..., "error": ...,
     "frames": [f"{f.filename}:{f.lineno} in {f.name}" ...]}))`. Same content, one record.
   - **If we skip it:** Errors are still found by `request_id` in stderr, just harder to search. No data leak: frames
     have no message text and no locals.

2. **Third-party plain-text lines on stdout next to the JSON audit** (`connectors/demo/src/demo_connector/server.py:175-181`)
   - **What this is:** The root logger is at INFO, so FastMCP/MCP SDK write lines such as
     `StreamableHTTP session manager started` and `Terminating session: None` to the same stdout.
   - **Problem:** stdout is not "JSON lines only"; a collector that parses each line as JSON gets errors on those.
     The new test itself skips them (`if line.startswith("{")`). This was so before the cycle (then on stderr), so it is
     missed earlier, not new.
   - **Fix:** Set `logging.getLogger("demo_connector").setLevel(INFO)` and keep the root at WARNING, or filter `out` to
     records whose name starts with `demo_connector`. One or two lines.
   - **If we skip it:** Log pipelines need a "parse if JSON" rule. No secret: those lines carry no arguments
     (checked by `test_no_secret_or_note_text_in_logs_and_responses` with every logger at INFO).

Verdict: Ship.
Not checked: behaviour with a real ContextForge identity (no production verifier exists before M5, ADR-0001).
