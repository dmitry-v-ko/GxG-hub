# M4 cycle 2 — ponytail-review

Diff `77d04f6..d2b62b0` (code `98f7ce1`, evidence `d2b62b0`), report `e90d7ea`. Critic, independent thread.
Load assumed: a spike run by one person; the verifier and the plugin are templates for M1 (`IdentityVerifier`) and the production adapter, so their defects carry over.

What this change does: it closes the 8 items from cycle 1. ContextForge now stops at startup if the identity plugin cannot load (`PLUGINS_FAIL_ON_PLUGIN_ERROR=true`). The JWS gets a signed `tool` claim that the verifier compares with `params.name`. Two new direct attacks were added (other connector, other tool), and the mutation script now covers 10 checks. The docs (ADR, SEC-03, TEN-01) and the GO/NO-GO wording were corrected, and the passthrough stage was removed.

## How I checked

- `dockerd` started by hand, then `spikes/m4-contextforge/run_spike.sh` with a fresh `.env` and a fresh adapter key. Results: A as in the committed evidence. C: 10/10 calls through ContextForge accepted with the right `sub`/`tenant_id`/`tool=echo`. `bypass_no_identity`, `replay_immediate`, `replay_tampered_args`, `replay_other_connector` (`bad aud`), `replay_other_tool` (`bad tool`), `forged_jws_attacker_key` and `replay_after_65s` were rejected with the same reasons as `evidence/variant-c.verify_jws.jsonl`: 17/17 rows match. `mutate_verify_jws.py`: 10/10, byte-identical to the evidence. Missing key with `PLUGINS_FAIL_ON_PLUGIN_ERROR=true`: ContextForge is never healthy and the call never reaches echo. Control run with `false`: healthy, and the call reaches echo without `X-GxG-Identity`. Upgrade: see the JSON.
- **mutmut 3.8.1** on `verify_jws.py` (184 mutants). The harness is mine (pytest, outside the repo). It replays the signed echo log of my own run, plus the 4 forged rows that `mutate_verify_jws.forged_rows` builds, through `verify()` with the same loop as `__main__`.
- Read the pinned v1.1.1 image: `/app/plugins/*`, `mcpgateway/services/tool_service.py` (token exchange), `cpex/framework/hooks/tools.py`, and the PyJWT version in the image.
- `scripts/ci/check-all.sh`: OK. Upgrade v1.1.0 → v1.1.1: 16/16 accepted, the same 3 alembic migrations. Variant A: 16/16 signatures valid, one signature per user. run_spike.sh exit 0. The committed `evidence/` was restored afterwards.
- `claude/m4-live` @ `b6918a5` (option C on the owner's Mac): compared.

## Must fix

1. **The mutation check uses a home-made script, and mutmut shows that the tests miss real mutants** (`spikes/m4-contextforge/mutate_verify_jws.py`, `verify_jws.py:31-79`, `run_spike.sh:27-31`)
   - **What this is:** DoD-4 now requires mutmut for Python and bans home-made mutation scripts. `mutate_verify_jws.py` deletes one `raise` at a time and only checks that "no attack is accepted".
   - **Problem:** mutmut on `verify_jws.py` makes 184 mutants. With the executor's own pass/fail rule (no attack accepted), 155 survive. Nothing checks that real calls are accepted. For example, the mutant `if c["iss"] == ISS: raise` rejects every call, yet `mutate_verify_jws.py` and `run_spike.sh` still exit 0. With the extra rule "every call through ContextForge is accepted", 67 mutants still survive. 27 of them are not equivalent:
     - 19 mutants change the returned identity (`tenant_id`, `principal`, `tool`, … renamed or altered). No test reads what `verify()` returns, which is exactly what the connector will use.
     - 7 mutants change `canonical()` (`sort_keys=False`, `ensure_ascii=True`). All recorded arguments are `{"text": <ASCII>}`, so the canonical JSON that SEC-03 defines is never tested. Concrete case: the argument `{"text": "привет"}` with `ensure_ascii=True` in the verifier gets a different hash, and a real call is rejected.
     - 1 mutant changes `exp + SKEW` to `exp - SKEW`. The 5 s clock-skew allowance is never tested.

     The other 38 mutants only change the text of a rejection reason. The ADR cites these reasons as evidence, but no test checks them.
   - **Fix:** a pytest test for `verify()` with its own Ed25519 key generated in the test. It needs no network, so it runs in CI. It asserts the accepted identity and the exact reason for each case: non-ASCII and multi-key arguments, skew boundaries (`exp+4` accepted, `exp+6` rejected), `iat` in the future, malformed header. Configure `[tool.mutmut]` for `verify_jws.py` (and for the plugin's `canonical`/claims code if it is kept), and commit the mutmut report: 0 non-equivalent survivors, equivalent ones listed with the reason. Delete `mutate_verify_jws.py`. In `run_spike.sh`, add one check: every `via-contextforge` row is accepted and every attack has its expected reason.
   - **If we skip it:** M1 copies a verifier whose tests do not notice when it returns the wrong tenant or rejects every non-ASCII call.

2. **A malformed, unsigned header crashes the verifier instead of being rejected** (`spikes/m4-contextforge/verify_jws.py:38-42`)
   - **What this is:** the header is parsed by hand before the signature is checked.
   - **Problem:** `X-GxG-Identity: W10.e30.AA` (the header is the JSON `[]`) gives `AttributeError: 'list' object has no attribute 'get'`. A header with `"kid": ["x"]` gives `TypeError: unhashable type`. Neither error is caught by `except (ValueError, KeyError)`. One such direct request to echo crashes `verify_jws.py`, `run_spike.sh` (`set -e`) stops, and no verdict is produced for any call. In a connector built on this pattern, it becomes a 500 error on input the attacker controls. I reproduced both cases.
   - **Fix:** see 3 (PyJWT rejects both with `DecodeError`/`InvalidTokenError`). Otherwise check `isinstance(head, dict)` and `isinstance(kid, str)`. Either way, add a test.
   - **If we skip it:** the M1 verifier inherits a crash on unauthenticated input.

## Should fix

3. **The JWS is signed and verified by hand although PyJWT is already there** (`plugin/gxg_identity/plugin.py:33-38,102-104`, `verify_jws.py:26-50`)
   - **What this is:** the compact JWS (base64url, header, Ed25519, `alg`/`kid`/`iss`/`aud`/`exp`/`iat`, required claims) is built and parsed by hand.
   - **Problem:** the pinned ContextForge image already ships PyJWT 2.15.1 with `cryptography`. `jwt.encode(..., algorithm="EdDSA", headers={"kid": ...})` in the plugin and `jwt.decode(token, key, algorithms=["EdDSA"], audience=aud, issuer=ISS, leeway=5, options={"require": [...]})` in the verifier do the same. The owner's replication on `claude/m4-live@b6918a5` (`echo/gxg_verify.py`, `adapter/adapter.py`) does exactly this. Only `tool`, `ahs`, `exp-iat≤60`, `sub` in tenant and `jti` stay custom. The cycle-2 plan does not name the alternatives considered (DoD-6), and finding 2 is a bug that the library does not have.
   - **Fix:** use PyJWT on both sides (or joserfc in M1, if the kit chooses it), and keep only the 5 custom checks. In the plan, add a DoD-6 table: plugin, verifier, mutation tool, and the alternatives considered.
   - **If we skip it:** about 20 lines of security-critical parsing code that the library has already tested.

4. **The plugin writes headers through a field that is deprecated in v1.1.1** (`plugin/gxg_identity/plugin.py:105-111`)
   - **What this is:** the plugin replaces `ToolPreInvokePayload.headers`.
   - **Problem:** cpex 0.1.4 in the pinned image (`cpex/framework/hooks/tools.py:74-86`) says: "ToolPreInvokePayload.headers is deprecated; use extensions.http.headers instead. This field will be removed in a future release." Criterion 5 ("survives an upgrade") passed for v1.1.0 → v1.1.1. Once the field is removed, `model_copy(update={"headers": ...})` no longer reaches the upstream: every call arrives without identity and is refused (fail closed, but it is an outage). The ADR does not mention this, and m4-live does.
   - **Fix:** one line in the ADR (criterion 5 and «Что дальше»), and MC-05 must check that `X-GxG-Identity` arrives. Moving to `extensions.http.headers` is the production adapter's job.
   - **If we skip it:** the next ContextForge upgrade silently turns identity off, and only the connector's refusals reveal it.

## Nice to have

5. **The verifier runs on the log, not inside echo** (`verify_jws.py:__main__`, `evidence/variant-c.replay.log`): echo answers 200 to every attack and runs the tool. Rejections are computed afterwards, using the arrival time. This is equivalent for the five criteria, but "the connector refuses" is not observed live. m4-live verifies inside echo and returns `-32001`. Not a finding for a spike, just a note for M5.
6. `evidence/variant-c-nokey.log`: the `true` line prints `Plugin initialization failed` twice, because `grep -o -m1` counts per line, not per match. Cosmetic.

## Existing solutions (ponytail ladder, steps 2–4)

| Component | Alternative | Verdict |
| --- | --- | --- |
| `verify_jws.py` (JWS parse/verify) | PyJWT 2.15 (`jwt.decode`, EdDSA). It is in the v1.1.1 image and used on m4-live. joserfc is not installed. | use_existing (custom only: tool, ahs, ttl, sub∈tenant, jti) |
| Plugin JWS signing (`b64`/`canonical`/`sign`) | PyJWT `jwt.encode` (in the image) | use_existing |
| `gxg_identity` plugin itself | Built-ins in v1.1.1: identity propagation (A, rejected on evidence). `jwt_claims_extraction` only reads incoming token claims (`http_auth_resolve_user`). `header_injector` adds static headers on `resource_pre_fetch`. `vault` injects stored provider tokens. RFC 8693 token exchange (`tool_service._resolve_token_exchange_header`) needs an external authorization server, and the exchanged token is cached per (gateway, user, audience), so it has no per-call `jti` or argument hash. None of them gives criteria 2 and 3. | justified_custom (the justification is missing from the ADR and plan, see 3) |
| `mutate_verify_jws.py` | mutmut 3.x (DoD-4) | use_existing |
| `redact.py`, `run_spike.sh`, `echo_mcp.py` | Nothing ready-made fits for a one-off spike stand | justified_custom |

## Cycle-1 items

1 fail closed: closed (reproduced, with a control run). 2 tool binding: closed (`tool` = `original_name`, checked). The shared `jti` store and canonical JSON are in SEC-03. 3 mutations: aud/tool covered by the script, but DoD-4 now requires mutmut, see 1. 4 TEN-01: closed. 5 direct_proxy/resources/prompts: documented, env set. 6 passthrough: removed. 7 verdict: reworded correctly. 8 re-run: done and reproduced.

Verdict: fix 1, 2 and 3 first.
Lean: -70 lines possible (`mutate_verify_jws.py` replaced by a pytest test, about -50; PyJWT in the verifier and the plugin, about -20).
Not checked: a runtime exception inside the plugin with the flag `true` (m4-live observed `Plugin Error` and the call was not sent); several ContextForge workers.
