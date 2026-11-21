# M5 cycle 1 — ponytail-review

Diff: `635caae..c01c5c3` (branch `claude/m5-readonly-poc`). Reviewer: independent critic, cycle 1.
Load assumed: one developer or one CI job runs the dev stand; four synthetic agents; one `demo` replica.

What I re-ran myself (not taken from the executor's report):
- `uv run pytest` (root): 797 passed. `connectors/demo` pytest: 63 passed. ruff check/format, mypy (stand + plugin, kit strict): clean.
- `stand/run.sh` on a **fresh** stand (old `.env`, `stand/secrets/` and both volumes removed first): fresh 28/28, after-restart 28/28, after-recreate 28/28, exit 0.
- mutmut 3.8.1: `gateway/contextforge` 133 mutants, 132 killed, 1 survivor (`x_args_hash__mutmut_5`, `ensure_ascii=None`, really equivalent). Kit: `cf_identity.py` — only `x_args_hash__mutmut_5` survives (equivalent); the other 10 kit survivors are the M1 ones.
- CI run 44924258932 on `c01c5c3`: every job green except `grants-widening` (red by M2 design on a push without PR).
- My own attacks on the running stand (results in the threat model of `M5-cycle-1.json`).

What this change does: it turns the root `compose.yaml` into a dev stand: ContextForge v1.1.1 (pinned digest) on PostgreSQL, the `demo` connector on an internal network, the built-in `unified_pdp` plugin (deny by default) and our `gxg_identity` plugin that signs an `X-GxG-Identity` JWS per call. The connector checks that JWS with a new kit class `ContextForgeVerifier`, bound to the tool name and a hash of the arguments. Scripts create secrets, configure teams, agents, tokens, gateways and virtual servers, run SC-01…SC-10 and MC-01…MC-04 through ContextForge three times (fresh, after restart, after recreate) and write the report; the roadmap records GO.

## Must fix

1. **All agent accounts share one password, and it opens a login on the agent port** (`stand/bootstrap.py:96-107`, `stand/init.py:122`, `compose.yaml:46-50`)
   - **What this is:** Bootstrap creates the four agent users in ContextForge. Each user gets the same password, `DEFAULT_USER_PASSWORD` from `.env`. Email login and the admin API are on the same `127.0.0.1:4444` the agents use.
   - **Problem:** One secret is a working credential for every agent of every tenant. I logged in as `reader@t-beta…` with it, got an interactive JWT, and minted a new 365-day API token for team `t-beta` (HTTP 201). That token is not the one the operator knows, so revoking the agent's token (MC-04) does not stop this access. SEC-02 forbids a shared credential for several agents.
   - **Fix:** Give each agent user a random password that is never stored (`secrets.token_urlsafe` per user, thrown away after creation), or block password login for agent users. Add a stand test: login of an agent email with `DEFAULT_USER_PASSWORD` fails.
   - **If we skip it:** Anyone who reads `.env` once becomes every agent and keeps a token that survives revocation.

2. **The stand dies one day after `stand/init.py`** (`stand/init.py:134-135`)
   - **What this is:** `init.py` builds the signed grants bundle only if it does not exist yet. `demo` refuses a bundle older than 24 h (GR-13).
   - **Problem:** A developer runs `stand/run.sh` on day 1 and again on day 2. The bundle is now 25 h old, `/readyz` is 503, `docker compose up --wait` fails on the `demo` healthcheck, and every test fails. Even `docker compose restart` (AC-M5-04, "без ручных действий") stops working after 24 h. CI does not see it because each job starts empty. GR-13a has CI re-sign every 12 h; the stand has no such step.
   - **Fix:** In `init.py`, rebuild the bundle on every run (same content, new `built_at`); it is one `subprocess` call, under a second. Then the README runbook row "старше 24 ч" can go.
   - **If we skip it:** The first reuse of the stand on the next day looks like a broken ContextForge.

## Should fix

3. **The "stale bundle" test passes for the wrong reason** (`stand/tests/test_sc.py:502-518`)
   - **What this is:** SC-10 must show that a correctly signed bundle older than 24 h makes `demo` not ready.
   - **Problem:** The test writes a bundle built 25 h ago, but `demo` keeps the newest `built_at` it ever loaded on its volume (GR-13a). The 25 h bundle is older than that, so it is rejected as a **rollback**. `demo`'s log during my run: `"bundle: older than a bundle loaded before (rollback)"`. Delete the 24 h age check and this test still passes.
   - **Fix:** Before the stale case, remove `/var/lib/demo/bundle-state.json` in the container (`docker compose exec demo rm …`) and restart, then assert the log reason contains `older than 24 h`. Restore the state at the end.
   - **If we skip it:** GR-13 on the live stand is claimed but not tested.

4. **The SSE call path is not in any test, but the GO says "all paths"** (`stand/tests/standlib.py:149-151`, `docs/specs/roadmap-v1.md` GO table, condition 4)
   - **What this is:** ContextForge v1.1.1 also serves MCP over SSE: `GET /sse` and `GET /servers/{id}/sse`, then `POST …/message?session_id=…`. It is a real call path: as `t-alpha:reader` I listed boards through `/servers/{id}/sse` and the call reached `demo`.
   - **Problem:** `paths()` covers `/rpc`, `/mcp/` and `/servers/{id}/mcp/` only. My probe shows the SSE path is closed today (as `t-alpha:nobody`: `notes.search`, a `t-beta` tool and `demo-message-execute` did not reach `demo`). But the reply is `{"result": {}}`, an empty success, not an error, and no test would catch a regression. GO condition 4 and ADR-0001 criterion 4 ask for every path.
   - **Fix:** Add `sse` and `virtual_server_sse` to `Stand.paths()` with a small SSE client (`mcp.client.sse.sse_client` is already in the lockfile), and run SC-01 deny/other-team and SC-03 over them. Write down in the GO note that a denied call over SSE returns an empty result.
   - **If we skip it:** A ContextForge update that lets one path skip the plugins goes unnoticed (MC-05).

5. **An unknown `kid` cannot raise an alert** (`connectors/demo/src/demo_connector/tools.py:94-96`)
   - **What this is:** SEC-03 says: key rotation by `kid`, alert on an unknown `kid`. `TokenError.reason` already says `kid`, `signature`, `replay` and so on.
   - **Problem:** The audit line keeps only `outcome: unauthenticated`. A forged JWS with `kid: attacker-1` (SC-04 case "unknown kid") gives the same line as a missing header. Nobody can alert on what is not logged.
   - **Fix:** For `TokenError`, add `"reason": exc.reason` to the audit entry (a fixed word, no secret). Assert it in SC-04 on the stand.
   - **If we skip it:** An attacker who tries keys from inside the connector network is invisible.

## Nice to have

6. **Timeout after sending is reported as `temporarily_unavailable`** (`connectors/demo/src/demo_connector/tools.py:87-91`). MCC-05 defines that code as "запрос **не** выполнен", but here the backend got the request. For a read this is harmless. Write the read case into MCC-05 (spec change) rather than leave code and spec apart.
7. **`bundle_version` of the stand is forty zeros** (`stand/init.py:58`, `stand/report.py:71`). GR-11 says it is the git SHA, and DoD-3 wants it in the report. Pass `git rev-parse HEAD`.
8. **The report is from another commit and has a hand-typed version** (`reports/conformance/M5.json` `commit` = `feb5de0`, reviewed `c01c5c3`; `stand/report.py:70` writes `fastmcp 4.1.8` as a literal). The delta is only `report.py`, so no result changes. Read the FastMCP version from `connectors/demo/uv.lock` and regenerate the report at the head commit.
9. **Kit mutmut is not enforced** (`packages/gxg-connector-kit/pyproject.toml:28`, `scripts/ci/mutmut.sh:19`). `cf_identity.py` was added to the kit's `only_mutate`, but the kit is not in the `mutmut.sh` loop, so CI never runs it. I ran it by hand: clean. Add the kit directory to the loop and its equivalents to `mutmut-equivalent.txt`.
10. **Bootstrap adds team membership only when it creates the user** (`stand/bootstrap.py:96-107`). If a run stops between "create user" and "add member", every later run skips the member step. The agent stays outside its team and its token is useless. Add the member if it is missing, every run.

Verdict: fix 1 and 2 first, then 3, 4, 5.
Lean: -0 lines possible (no dead code found; the two `args_hash` copies are needed because the plugin runs in the ContextForge image without the kit, and a test ties them together).
Not checked: behaviour after a real 24 h wait (finding 2 follows from code plus the SC-10 log); ContextForge with several workers under load.
