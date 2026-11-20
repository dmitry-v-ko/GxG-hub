# M4 cycle 1 — ponytail-review

Diff: `6b3fe48..0130d78` (spike code, evidence, ADR-0001, VERIFY edits, digest pin). The report commit `9df9cde` only adds `reports/conformance/M4.json`.
Load assumed: a local spike run by one person. For the plugin and verifier I also judge the production shape they prototype: one ContextForge, a few connectors with more than one replica each, up to ~10 tenants (target-platform-v1).

What this change does: it stands up ContextForge v1.1.1 (pinned by digest) with PostgreSQL and an echo MCP server, then runs the ADR-0001 protocol. Variant A (ContextForge's own HMAC identity headers) is tested and rejected. Variant C (our `tool_pre_invoke` plugin that adds an Ed25519 JWS `X-GxG-Identity`) is tested and accepted. The ADR becomes Accepted (C), the image is pinned in `.env.example`, and the VERIFY marks in SEC-02, SEC-03, TEN-01 and the GR example become concrete text.

## What I re-ran (this container, dockerd started by hand, 2026-11-20 19:05–19:15 UTC)

- `spikes/m4-contextforge/run_spike.sh` with fresh secrets: exit 0. The results match the committed evidence:
  - A: `signatures_valid 14/14`, `distinct_signatures_per_user` = 1 for each user, `signature_still_valid_after_changing_teams_and_admin: true`, the platform admin reaches the tenant tool with `Admin: true`.
  - C: 10/10 accepted via ContextForge. Rejected: `bypass_no_identity` (missing), `replay_immediate` (replayed jti), `replay_tampered_args` (hash mismatch), `forged_jws_attacker_key` (bad signature), `replay_after_65s` (expired). Admin is refused with `GXG_IDENTITY_DENIED`. Cross-team calls do not reach echo on any path.
  - Upgrade: 16/16 accepted, the same 3 alembic migrations. The `mutate_verify_jws.py` log is byte-identical to the committed one.
  - I restored the committed `evidence/` afterwards, so nothing changed in the repo.
- Variant A config is not the cause of the failure. In the pinned image, `mcpgateway/utils/identity_propagation.py` signs only `f"{user_id}:{email}"` when `sign_claims` is on. No setting adds teams, time or nonce. `IDENTITY_CLAIMS_SECRET` was set and used: `signature_matches_JWT_SECRET_KEY…: false`.
- Digest and advisories, today:
  - The ghcr index digest for `v1.1.1` and for `latest` is `sha256:16c7a898…56a5`. There is no tag newer than v1.1.1. The image label revision is `dbc643be`.
  - OSV for PyPI `mcp-contextforge-gateway==1.1.1` returns `{}`.
  - The GHSA list has 19 entries, the newest from 2026-09-07. The IBM bulletin 7296490 says v1.0.0–v1.1.0 are affected and v1.1.1 is fixed.
  - The ADR is correct on all of this.
- My own probes, on the raw signed log and on the live stand. They are listed under the findings below.
- Independent run `claude/m4-live` (`65eabd2`, owner's Mac, arm64, variant A only): same mechanism (HMAC over `user_id:email`, one static signature per user, tenant not signed). It also rejects A. Two differences, see "Comparison with claude/m4-live" at the end.

## Must fix

1. **`fail_on_plugin_error: true` in plugins.yaml is ignored. A broken plugin is silently skipped** (`spikes/m4-contextforge/spike.py:382-383`, `docs/adr/0001-identity-propagation.md:106`)
   - **What this is:** The ADR says the plugin runs with `fail_on_plugin_error: true`. That setting is meant to stop ContextForge from serving calls without the identity plugin.
   - **Problem:** In the cpex version shipped in v1.1.1, `Config.plugin_settings` is ignored ("ignored; runtime settings are read from env vars", `cpex/framework/models.py`). The flag is read only from the env var `PLUGINS_FAIL_ON_PLUGIN_ERROR`, which defaults to `false`.
     - Checked live: I pointed `private_key_file` at a missing file. ContextForge logged `Skipping plugin GxgIdentity because fail_on_plugin_error is disabled` and `initialized with 0 plugins`, and `/health` answered `healthy`.
     - An agent call then reached the connector with no `X-GxG-Identity`.
     - With `PLUGINS_FAIL_ON_PLUGIN_ERROR=true` in the env, the same broken key stops startup (`Plugin initialization failed`), which is the behaviour the ADR claims. `PLUGINS_ENABLED=false` gives the same silent result as the broken key.
     - The connector still refuses these calls, so no identity can be forged. But the "fail closed in ContextForge" layer the ADR describes does not exist as configured.
   - **Fix:**
     - Set `PLUGINS_FAIL_ON_PLUGIN_ERROR: "true"` in `compose.variant-c.yaml` and drop `plugin_settings` from the generated YAML.
     - Correct ADR line 106 and SEC-03: name the env var, and state that with plugins off the only guard is the connector.
     - Add one stage to `run_spike.sh`: a missing key file means ContextForge must not become healthy.
   - **If we skip it:** M5 copies this config. A bad key mount or a typo leaves a "healthy" gateway that sends every tool call without identity. The outage only shows up at the connectors, and the gateway's health check never reports it.

## Should fix

2. **The JWS is not bound to the tool name, and `jti` is one-time only per replica** (`spikes/m4-contextforge/plugin/gxg_identity/plugin.py:84-96`, `spikes/m4-contextforge/verify_jws.py:32-59`, `docs/specs/security-v1.md:39`)
   - **What this is:** `ahs` and `jti` are what stop a captured identity from being reused. These are the same attacks the spike runs for criteria 2 and 3.
   - **Problem:**
     - The claims hash only `arguments`. `verify()` never sees the tool name or the JSON-RPC method.
     - Probe on the raw log: the captured `echo` JWS with the same arguments passed `verify()` for any tool name on that connector. For example, a captured `board_read {"id": 5}` becomes `board_delete {"id": 5}` within 60 s.
     - The `jti` cache is a per-process set. Two-replica probe: replica A rejects the replay (`replayed jti`), replica B accepts it.
     - SEC-03 lists the checks the connector must make, but not the tool name and not a shared `jti` store. The shared store is mentioned only in the ADR's "Что дальше", so M1 can be built "to spec" and still have both gaps.
   - **Fix:**
     - Add a `tool` claim: the name sent upstream, not the alias. The plugin sees `payload.name`, so check which one it is on the alias path.
     - Add `method`, or bind `ahs` to `{"name", "arguments"}`. The verifier compares them.
     - In SEC-03, add "tool name is signed and checked" and "`jti` store shared by all replicas of a connector, kept until `exp` + skew".
     - Also name the canonical JSON form. Today it is Python `json.dumps(sort_keys, separators, ensure_ascii=False)`, which writes `1e-07` where JCS writes `1e-7`.
   - **If we skip it:** Anyone who can read traffic between ContextForge and a connector can turn one read into a different tool call as the same agent. With two replicas, they can also replay the same call once per replica.

3. **The mutation self-check does not cover `aud` (or `iss`, `kid`, max TTL, `sub` in tenant)** (`spikes/m4-contextforge/mutate_verify_jws.py:19-24`, `verify_jws.py:46-52`)
   - **What this is:** DoD-4 is reported as passed because `mutate_verify_jws.py` drops each check and expects an attack to get through.
   - **Problem:**
     - Only signature, `exp`, `ahs` and `jti` are mutated. No spike attack sends a JWS to the wrong connector.
     - I removed `raise ValueError("bad aud")`. The captured t-alpha JWS sent to the t-beta connector (`gxg-connector:echo-t-beta`) was then ACCEPTED, and no spike step turned red.
     - In production one connector serves every tenant. `aud` is then the only thing that stops a KB-connector JWS from being used on the Telegram connector within 60 s.
     - The same holds for `exp − iat ≤ 60`: the plugin always signs 60 s, so removing that check is never caught.
   - **Fix:**
     - Add a `replay_other_connector` step to `spike.py replay`: send the captured headers to `/mcp/<other tenant>`.
     - Add `('raise ValueError("bad aud")',)` to `MUTATIONS`.
     - Optionally add one forged-but-validly-signed case for max TTL and for `sub` outside the tenant, by signing with the spike key in the mutation script.
     - In M4.json DoD-4, state which checks are covered.
   - **If we skip it:** `IdentityVerifier` in M1 copies `verify_jws.py`. A refactor that loses the `aud` comparison passes every check we have.

4. **TEN-01 treats the `/mcp` refusal as team isolation, but `/mcp` refuses the agent's own team too** (`docs/specs/target-platform-v1.md:65`)
   - **What this is:** The new TEN-01 text says a call to another team's tool "is refused on `/rpc`, `/mcp` and `/servers/{id}/mcp`".
   - **Problem:** In `variant-c.observations.jsonl`, all 4 agents' calls to their own team's tool on `direct:/mcp` also fail with `Access denied` (RBAC `tools.execute`). The ADR says this too (line 134). So the `/mcp` result says nothing about isolation, yet the spec now states it as verified.
   - **Fix:** In TEN-01, keep `/rpc` and `/servers/{id}/mcp`, and say that the global `/mcp` refuses team-scoped agent tokens for every tool on v1.1.1. One sentence.
   - **If we skip it:** If a later ContextForge version opens `/mcp` to team tokens, nobody re-checks isolation there, because the spec says it was already checked.

5. **"All paths" leaves out paths that skip the plugin: `direct_proxy` gateways, `resources/read`, `prompts/get`** (`docs/adr/0001-identity-propagation.md:134`, SEC-03)
   - **What this is:** Criterion 4 is marked passed for "all paths".
   - **Problem:** In v1.1.1, a gateway with `gateway_mode=direct_proxy`, enabled by `MCPGATEWAY_DIRECT_PROXY_ENABLED`, "bypasses all gateway processing (caching, plugins…)" (`tool_service.py`, `invoke_tool_direct`). `tool_pre_invoke` also does not run for resources or prompts. These calls reach the connector without identity. The connector then refuses them (fail closed), but the ADR does not say these paths exist or that they must stay off. `claude/m4-live` lists them as not checked.
   - **Fix:** One line each in the ADR and SEC-03: `MCPGATEWAY_DIRECT_PROXY_ENABLED=false`, and connectors expose identity-dependent data only as tools (or the adapter also hooks the resource and prompt pre-hooks).
   - **If we skip it:** Someone enables direct_proxy for speed, or a connector adds a resource, and it breaks in a way the ADR says cannot happen.

6. **The passthrough-misconfiguration stage proves nothing and keeps a claim it cannot support** (`spikes/m4-contextforge/run_spike.sh:41-44`, `compose.passthrough-misconfig.yaml`, ADR line 123)
   - **What this is:** A stage that turns on header passthrough for every identity header. It should show that the plugin overwrites a forged `X-GxG-Identity`.
   - **Problem:**
     - I checked the live stand. The global allowlist holds all 9 headers (`GET /v1/admin/config/passthrough-headers`), and both gateways have `passthroughHeaders: null`, so the global list applies.
     - Even so, a plain call with `X-Tenant-Id` and `X-Trace-Id` reached echo without either header. So in v1.1.1 these headers are not passed through on this MCP path at all, and the stage never puts a forged header next to the plugin's.
     - ADR line 123 still says "Поддельный `X-GxG-Identity` перезаписан". That was never observed. What really protects is the signature.
   - **Fix:** Delete the stage, the overlay and its two evidence files, or replace them with a unit-level check of the plugin's header filter. In the ADR, say "not reachable on v1.1.1; the signature is the guard".
   - **If we skip it:** About 10 lines of setup plus evidence that readers will take as proof of something.

7. **The preliminary GO is worded stronger than the roadmap allows** (`reports/conformance/M4.json:154`)
   - **What this is:** `go_no_go_preliminary.verdict` = "GO for ContextForge v1.1.1 with variant C".
   - **Problem:** The roadmap gives GO only if all 4 conditions are confirmed. Condition 4 (`unified_pdp`) is `not_run`. Condition 3 is observed only for cross-team tools, not for hidden tools. The spike gives no reason for NO-GO, but it does not confirm GO either.
   - **Fix:** Change the verdict to "no NO-GO trigger found: 1 and 2 confirmed, 3 partial, 4 not run (M5); GO/NO-GO at end of stage B".
   - **If we skip it:** Stage C planning may treat GO as decided while half its conditions are still open.

## Nice to have

8. **The adapter key is as strong as the ContextForge process** (ADR lines 52, 72; `run_spike.sh` `chmod 644`)
   - **What this is:** The ADR and SEC-03 say "the signing key is held only by the adapter".
   - **Problem:** The adapter runs inside the ContextForge process. Any code execution there (the GHSA list includes critical sandbox and RCE bugs, for example GHSA-xm98) can read the key and mint a JWS for any tenant.
   - **Fix:** Say this in "Минусы" of C. Production: key from a secret store, short `kid` rotation, and alerting on unknown `kid`.
   - **If we skip it:** Readers think C is safer against a ContextForge compromise than it is. Variant A is no better here.

9. **The principal is the email local part, so it can be reused** (`plugin.py:80-88`, GR example)
   - **What this is:** `sub` = `agent:<tenant>:<local part of email>`. ContextForge only gives the email (`UserContext.user_id = user.email`), not its UUID. `claude/m4-live` notes this too.
   - **Problem:** If `hermes@t-alpha…` is deleted and a new agent is created with the same email, the new agent gets the same principal and inherits its grants. SEC-01 asks for an immutable ID.
   - **Fix:** One line in the GR example or the runbook: agent emails are never reused, or the registry keeps a tombstone.
   - **If we skip it:** Grants can carry over to an unrelated agent after the operator recreates a user.

10. **SEC-22 still calls the tag a candidate** (`docs/specs/security-v1.md:92`)
    - **What this is:** The spec line says the tag in `.env.example` is only a candidate. The executor listed this in `known_limitations` as something for the owner to decide.
    - **Problem:** The line now contradicts the pinned digest in `.env.example`.
    - **Fix:** Owner decision. One line.
    - **If we skip it:** Two documents disagree about which image is approved.

Verdict: fix 1, 2 and 3 first (4–7 are one-line text fixes; do them in the same cycle).
Lean: -10 lines possible.
Not checked: the amd64-vs-arm64 behaviour beyond the index digest (I ran amd64, `claude/m4-live` ran arm64, same index); a timeout of the plugin at runtime (per-plugin `on_error` defaults to `fail` in cpex, read in code, not run).

## Comparison with `claude/m4-live`

| Point | Executor (`0130d78`) | m4-live (`65eabd2`) | Critic |
| --- | --- | --- | --- |
| A, criterion 1 | failed (teams/admin not signed; admin reaches tenant tool) | passed, for the path through ContextForge; the unsigned tenant is counted under 2 | A difference in how the cases are counted, not in the facts. Both reject A |
| A, criteria 2, 3 | failed | failed | same |
| A, criterion 5 | passed (headers present after upgrade) | not run | fine as a rehearsal; it does not matter for the decision |
| Variant C | built, passed 1–5 | not tested | reproduced here |
| Digest | OCI index `16c7a898…`, amd64 child `88479e8d…` | same index; report calls it "the linux/arm64 image" | it is the multi-arch index (registry `docker-content-digest` for the index media type); the m4-live note is inaccurate, the pin is right |
| Not covered | — | direct_proxy, resources/prompts, multi-worker | carried over into findings 2 and 5 |
