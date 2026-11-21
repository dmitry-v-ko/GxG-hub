# M3 cycle 3 — plan

Critic cycle 2: score 6.0 (DoD-4). Required: a real-process check of the audit log; minors 2–4.

1. **Audit log in a real process (DoD-4, MCC-08).** Split `server.main` into `configs()` (uvicorn configs) and
   `serve()` (blocking). New test starts `python -c` with the real `main()` in a child process, `serve` replaced by one
   raw JSON-RPC `tools/call` through the app; asserts a JSON audit line (`tool`, `outcome`) on the child's **stdout**.
   The child imports the same copy as the test (`PYTHONPATH`) and inherits `MUTANT_UNDER_TEST`, so mutmut's active
   mutant runs in it; the test also calls `main()` in-process, so mutmut maps it to `main`. Drop the 6 `basicConfig`
   entries from `mutmut-equivalent.txt`. Logging: JSON lines to stdout (was stderr by `basicConfig` default).
2. **Empty board set:** validate the cursor first; skip only the backend call (−7 lines).
3. **`internal_error`:** audit gets `"error": <exception class>`; stack frames (no message text, no arguments) go to
   the `demo_connector.errors` logger, which `main()` routes to stderr only.
4. **Test controls server:** `configs()` asserted in-process for both profiles (hosts, ports, log level, the
   test-controls app works). Only `serve()` stays listed (needs sockets).
5. Re-run tests, ruff, mypy, mutmut, docker_smoke, CI; update `reports/conformance/M3.json`, `reports/mutmut/M3.txt`.
