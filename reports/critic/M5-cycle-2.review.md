# M5 cycle 2 — ponytail-review

Diff: `4aaa25f..d39a1ff` (code `4aaa25f..9add475`, then only `reports/`). Branch `claude/m5-readonly-poc`. Reviewer: independent critic, cycle 2.
Load assumed: one developer or one CI job runs the dev stand; four synthetic agents; one `demo` replica.

What I re-ran myself (not taken from the executor's report):
- **Fresh stand.** I moved the old `.env` and `stand/secrets/` away and removed both volumes. Then I ran `DOCKER_BUILD_FLAGS=... KEEP=1 stand/run.sh`. Result: fresh 29/29, after-restart 29/29, after-recreate 29/29, exit 0. My report copy says `commit` d39a1ff, `commit_dirty` false, `bundle_version` = d39a1ff (the git SHA, F7) and `fastmcp (demo image)` 4.1.8 (read from `uv.lock`, F8). The committed `reports/conformance/M5.json` was left unchanged.
- **Unit tests and CI steps.** Root `uv run pytest`: 798 passed. `connectors/demo`: 64 passed. Kit: 360 passed, branch coverage ok. Also clean: ruff check/format, all four mypy runs from `check-all.sh`, `schema_lock`, `prove_schema_guard` (66/66), `bundle_smoke`, gitleaks (tree and full range), and gxg-policy validate/lint.
- **mutmut.** I ran `scripts/ci/mutmut.sh` (all five directories, the kit included). The result is in `M5-cycle-2.json` → `dod.4_mutmut`.
- **CI.** Run 44932350759 on `d39a1ff`: every job failed within 2–3 s with `runner_id 0`, no steps and no logs. The M7 branch run at the same minute failed the same way, so this is an account-wide runner/billing failure, not the code. The last CI run that executed jobs on cycle-2 code was 44926965184 on `de675bb`. In that run everything was green except `mutmut`, which failed on the renumbered equivalent `run__mutmut_130 → 133` that 9add475 fixes, and `grants-widening`, which is red by design. The run on `9add475` was cancelled.
- **Live checks of the checklist on my stand** (state restored afterwards):
  - F1: logging in as any of the 4 agents with `DEFAULT_USER_PASSWORD` gives 401.
  - F1 upgrade path: an admin `PATCH` set `reader@t-beta` back to `DEFAULT_USER_PASSWORD`, and login gave 200 (I logged out at once). `bootstrap.py` then reset the password, and the same login gave 401.
  - Account unlock: `/unlock` returns 200. The agent's API token still works.
  - F5: `demo` audit reasons. The stale and rollback cases are covered by the SC-10 not_ready reasons.
  - F4: alpha's token on t-beta's `/servers/{id}/sse` gives 403. An SSE `/message` POST with no token gives 401. With t-beta's token on alpha's session it gives 403 `Session access denied`. Only the owner's call reached `demo`.

What this change does: it closes the five major findings of cycle 1. Each agent now gets a random, never-stored password, reset on every bootstrap, and a stand test proves no agent can log in with a password. `stand/init.py` re-signs the grants bundle on every run, and `bundle_version` is now the commit. SC-10 tests invalid signature, rollback and older than 24 h separately, each by its own `not_ready` reason. SSE paths (`/sse`, `/servers/{id}/sse`) are in the SC-01/02/03 tests through the official `mcp` SDK client. `demo` writes the identity refusal reason (`kid`, `signature`, `replay`…) into its audit line. Smaller items: the kit joins the mutmut loop, the report reads FastMCP from `uv.lock`, and team membership is repaired when missing.

## Should fix

1. **Other team's virtual server over SSE is not in the other-team test** (`stand/tests/test_sc.py:62-75`, `stand/tests/standlib.py:150-186`)
   - **What this is:** `test_sc01_other_team_tool_is_refused_by_name_on_every_path` runs the agent's own paths and adds the other team's virtual server, but only its MCP URL (`/servers/{other}/mcp/`). It does not add the SSE URL (`/servers/{other}/sse`).
   - **Problem:** That one path of the GO condition 1 (Roadmap: "своём и чужом virtual server (MCP и SSE)") has no test. It is closed today: I got 403 on the SSE handshake with alpha's token on t-beta's server, and nothing reached `demo`. But a future ContextForge change there would not be caught. Also, `Stand._sse` cannot report a refused handshake: a 403 surfaces as an `HTTPStatusError` inside the SDK task group, not as a status. That is probably why the path was left out.
   - **Fix:** In `_sse`, catch the HTTP error from `sse_client` and return `{"_raw": "<status>"}` with that status. Then add `"other_virtual_server_sse": f"/servers/{other}/sse"` to the dict in the test. About 6 lines.
   - **If we skip it:** The Roadmap claims a path that no test checks.

## Nice to have

2. **The conformance report's CI block is from cycle 1** (`reports/conformance/M5.json` → `environment.ci`, `dod.1`)
   - **What this is:** The report names CI run 44920017134 on `e810df0` (cycle 1) as its CI evidence. `dod.1` says "CI green".
   - **Problem:** There is no CI run of the cycle-2 code that executed and passed. The `de675bb` run failed on mutmut (fixed in 9add475). The `9add475` run was cancelled, and the `d39a1ff` run never got a runner. The report itself says "see the critic's check", but `dod.1` still claims green.
   - **Fix:** Re-run CI on the head once runners are back, then put that run id in `environment.ci`. No code change.
   - **If we skip it:** A reader takes a cycle-1 run as proof for cycle-2 code.

3. **README still says `init.py` builds the bundle once** (`stand/README.md:32`)
   - **What this is:** The step-by-step block comments `stand/init.py` as "секреты, ключи, bundle (git-ignored, один раз)".
   - **Problem:** Since F2, `init.py` re-signs the bundle on every run, and that is what keeps the stand alive after 24 h. The runbook row below says so, but the comment says "один раз". A developer may skip rerunning `init.py` the next day.
   - **Fix:** Change the comment to "bundle переподписывается при каждом запуске". One line.
   - **If we skip it:** Someone reuses a stand after 24 h without `init.py` and finds `demo` not ready.

Verdict: Ship (findings 1–3 are minor; finding 1 is a test gap on a path verified closed by hand).
Not checked: a real 24 h wait (F2 verified from code plus SC-10's 25 h-old bundle); a green CI run of the head (runners unavailable for the account at review time).
