# M4 cycle 3 — ponytail-review

Diff `e90d7ea..27c676a` (code `a283c8b` + `b4b8656`, ADR `466164f`, evidence `27c676a`), report `140dbd3`. Critic, independent thread.
Load assumed: a spike run by one person. The verifier and the plugin are templates for M1 (`IdentityVerifier`) and the production adapter, so their defects carry over. `check_verdicts.py` is named in the ADR as the model for MC-05.

What this change does: it closes the 6 items from cycle 2. The plugin signs and the verifier checks the JWS with PyJWT 2.15.1 from the ContextForge image. The verifier now runs inside echo when a request arrives. The home-made mutation script is gone: there are 30 pytest tests and a mutmut run (132 mutants, 131 killed, 1 equivalent). `check_verdicts.py` makes `run_spike.sh` fail on a wrong verdict. The ADR records the `ToolPreInvokePayload.headers` deprecation. The diff also carries the merge of `claude/integration` (plan with DoD-4 = mutmut and DoD-6, schemas, specs). I did not review that merge here, only checked that `check-all.sh` passes on it.

## How I checked

- Started `dockerd` by hand, then ran `spikes/m4-contextforge/run_spike.sh` on `27c676a` with a fresh `.env` and a fresh adapter key: **exit 0**. `check_verdicts c: OK`, `check_verdicts c-upgrade: OK`. Fail closed without the key: `healthy=no`, `reached_connector: false`. Control run (`false`): healthy, the call arrives without `X-GxG-Identity`. 3 alembic migrations on the upgrade.
- My verdicts against the committed `evidence/*.verify_jws.jsonl`, grouped by (source, verdict): variant C 17/17 match (10 accepted, 4 agents; 7 attacks with the same reasons), upgrade 16/16 match.
- `uv run pytest spikes/m4-contextforge/tests`: 30 passed. `scripts/ci/check-all.sh`: OK.
- `mutmut.sh` in a clean copy of the tree: 132 mutants, 131 killed, 1 survivor `x_canonical__mutmut_4` (`ensure_ascii=None`). It is equivalent: `json` only tests whether `ensure_ascii` is truthy. My report matches `reports/mutmut/M4.txt` line for line. Negative checks: a failing test makes `mutmut.sh` exit 1, and dropping the `ttl=61` assertion leaves 3 survivors and exit 1.
- Malformed headers: 12 extra inputs on top of the tests (`crit`, `alg: none`, `b64: false`, `alg` as a list, `kid` as an object, a payload that is not UTF-8, `...`, 10 000 characters, a non-ASCII token…). All 12 were rejected with a `PyJWTError` reason and none crashed.
- `check_verdicts.py`: I changed the reason of one attack (`replay_other_tool` → `arguments hash mismatch`). The script prints `FAIL` and exits 1.
- Pinned image v1.1.1: `cpex/framework/hooks/tools.py:76-83` emits the `headers` deprecation warning. `tool_service.py:5894-5899` reads only `modified_payload.headers`, and nothing in it reads `modified_extensions`. `header_injector` only hooks `resource_pre_fetch`. No built-in plugin signs an outgoing identity. The ContextForge logs of the run contain no JWS (`eyJ`: 0).
- Compared with `claude/m4-live@b6918a5` (`echo/gxg_verify.py`, `adapter/adapter.py`).

## Nice to have

1. **`run_spike.sh` stays green when a call path stops delivering** (`spikes/m4-contextforge/check_verdicts.py:38-58`)
   - **What this is:** `check_verdicts.py` is the pass/fail gate of the run. The ADR (criterion 5) gives it to MC-05 as the check "identity arrived and was accepted".
   - **Problem:** it only judges calls that reached echo. A call that ContextForge answers with an error other than `GXG_IDENTITY_DENIED` (`Unknown tool`, `Tool not found`, 5xx) is not a failure, as long as the agent has one accepted call on some other path. Concrete case: in my upgrade run I replaced both `t-beta:hermes virtual_server` calls with `Unknown tool: echo-t-beta-echo` and removed their echo rows. `check_verdicts.py c-upgrade` still printed `OK`, exit 0. Criterion 4 says "valid JWS on every path for all 4 agents", and nothing in the run guards it. `attack_cross_team` (`reached_echo`) is not checked either. The same gap makes the M4.json DoD-2 note ("exits 1 if … any ContextForge call not accepted") claim too much. The criterion 4 result itself holds: I reproduced it path by path.
   - **Fix:** one table `{(agent, path): "echo" | "rbac_denied" | "not_found"}` in `check_verdicts.py`, built from the 5 paths in `spike.py paths()` (alpha: alias on `/rpc` and on the virtual server; beta: `/rpc` and the virtual server; `/mcp` refused for everyone). Fail on any other outcome, and fail on any `attack_cross_team` with `reached_echo: true`. That is about 10 lines.
   - **If we skip it:** an upgrade that breaks one path (for example the virtual server) passes MC-05, if MC-05 is copied from this script.

Verdict: Ship.
Not checked: the content of the `claude/integration` merge (schemas, specs, plan) beyond `check-all.sh`; the plugin's claim code has no mutation test (a decision in the plan: `cpex` exists only in the image, and every run checks the plugin's output through `check_verdicts.py`).
