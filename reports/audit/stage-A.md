# Stage A audit — ponytail-audit

Branch audited: `claude/m0-foundation` at `6b3fe48` (M0 closed at 9.5). The newest specs were read from `claude/admiring-planck-vzys0x` at `43bc9be` (read only). Scope: the whole repo: `docs/specs/*`, `docs/adr/`, the plan, `schemas/*`, the OpenAPI file, the test vectors, `policies/*`, `tests/*`, `conftest.py`, `.github/`, `scripts/ci/*`, the packaging and `compose.yaml`/`.env.example`.

Run by hand: `scripts/ci/check-all.sh` → OK (ruff, mypy, pytest, schema lock, `prove_schema_guard.py` 59/59, secret scan). All 6 canonical-hash vectors were recomputed by a separate script and match. Every schema claim below was checked with `jsonschema` against the real files.

What this repo does: it is the spec and contract base of GxG Hub. GxG Hub is a multi-tenant MCP platform where agents read data through connectors and can only *propose* effects; a person approves them and a separate Executor runs each one at most once. Today the repo has normative specs, JSON Schemas, an internal OpenAPI file, example policies, contract tests, a CI with a schema guard and a secret scan, and empty package skeletons. There is no service code yet.

Load assumed: one maintainer, a few agent-executors working on parallel branches, CI on every push and PR. For the specs: the planned production load of up to ~10 tenants, several executor and sweeper processes against one PostgreSQL, bundles re-signed every 12 h.

## Must fix

1. **A failed `execute` before `dispatch` has no way out; the Executor loops for an hour** (`docs/specs/approval-service-v1.md:154,156,213`)
   - **What this is:** AS-12 says the connector answers `execute` with 401/409/422/423 *before* `dispatch` and calls this `failed_before_effect`. The transition table (T1–T18) must hold every allowed move (AS-03).
   - **Problem:** T12 (`failed_before_effect` → `approved`/`failed`) starts only from an execution in `dispatching`. A 401/409/422/423 comes while the execution is still `reserved`, and no row handles that. The only way out is T10 (lease expired → `approved`), and T10 does not count the attempt. Case: the args hash does not match (AC-M8-05 expects 422 every time). Every 60 s the Executor reserves again, gets 422 again, and `attempts` stays 0, so `max_attempts` never stops it. After 1 h T8 marks it `expired`, not `failed`, and about 60 useless reservations are made.
   - **Fix:** Add one row: `executing` (exec `reserved`) + "complete: rejected before dispatch" → `approved`/`failed` by the same rule as T12, and count it as an attempt. Or say that 401/422 are final and go straight to `failed`. A one-row change to the table and to AC-M7-02.
   - **If we skip it:** M7's state-machine test (AC-M7-02) pins the wrong table, and M8 either breaks the table or loops. Blocks M7 and M8.

2. **The Approval Service has no trusted source for `effect_class`, `executes_via` and `dispatch_timeout_s`** (`schemas/intent-create.schema.json`, `schemas/platform-registry.schema.json`, `docs/specs/approval-service-v1.md:49,52`)
   - **What this is:** The `intents` table needs `effect_class`, `execute_tool` ("`executes_via` from the manifest") and the tool's `dispatch_timeout_s` for policy (AP-01), the execute URL and the dispatch lease. These live in the connector's `gxg-manifest.yaml`.
   - **Problem:** `POST /v1/intents` (intent-create) does not carry them. The signed bundle (GR-11a: grants, approval policy, platform registry) does not carry manifests. The registry has only `id`, `kind`, `public_key`, `tenants`, `internal_url`. So M7 cannot fill these NOT NULL columns from trusted data. The easy patch, "add `effect_class` to the request body", lets a buggy or compromised connector send `write_reversible` for a `high_risk` tool. AP-01 then gives `not_required`, and the effect runs with no human.
   - **Fix:** Put each connector's tool list (`name`, `version`, `effect_class`, `executes_via`, `dispatch_timeout_s`) into the bundle, for example under `platform.services[].tools`, filled by `gxg-policy build` from `connectors/*/gxg-manifest.yaml`. Then the Approval Service looks the tool up by `(iss of service JWT, tool_key, tool_version)` and rejects unknown tools. The same data also serves the M2 lint rule "class comes from the manifest".
   - **If we skip it:** M7 must invent a source, and the obvious one breaks the "agent proposes, a human decides" rule. Blocks M7.

3. **`GET /v1/intents/{id}` and `cancel` have no tenant scope for the MCP facade** (`schemas/approval-service.openapi.yaml:36-66`, `policies/platform/registry.example.yaml`)
   - **What this is:** The `mcp` facade (`effects.status`, `effects.cancel`) calls these endpoints for an agent. The rule is "an agent sees only its own intents; foreign ones are `not_found`".
   - **Problem:** The contract scopes the call only by `on_behalf_of` (a principal string), which is optional on GET. The facade's service JWT has `tenants: ["*"]`. Nothing in the contract carries the verified tenant of the agent. Case: the facade calls GET without `on_behalf_of` (a bug, or the UI path), and the contract allows it to return any tenant's intent by ID. Even with `on_behalf_of`, the tenant comes from the format of the principal string, which ADR-0001 still marks VERIFY.
   - **Fix:** Make `on_behalf_of` an object `{principal, tenant_id}` and required on both endpoints for callers of kind `approval_mcp`. The service filters `WHERE tenant_id = $t AND requester_principal = $p`. Give the UI its own path, or none, since the UI runs in-process.
   - **If we skip it:** M7 builds cross-tenant reads into the contract, and AC-M7-09 tests only the case where `on_behalf_of` is present. Blocks M7.

4. **MCC-09 makes `/readyz` depend on the Approval Service, so the M3 connector can never be ready** (`docs/specs/mcp-connector-contract-v1.md:91-92`, plan AC-M3-01)
   - **What this is:** MCC-09: `/readyz` = ready only if "backend, Approval Service, JWKS are reachable". AC-M3-01: `docker run` of `demo` gives `/readyz` = 200 with a valid bundle.
   - **Problem:** The Approval Service and the JWKS do not exist until M7/M8. If `demo` follows MCC-09, AC-M3-01 fails. If it ignores MCC-09, it breaks the contract. Later, every read-only connector (KB, Telegram read) goes not-ready when the Approval Service is down, so reads fail for all 10 tenants because of an unrelated service.
   - **Fix:** In MCC-09, require the Approval Service and JWKS only for connectors that have effect tools (`executes_via` in the manifest). For read-only tools, require backend + valid bundle (GR-12). Two lines in the spec.
   - **If we skip it:** The M3 executor has to pick which rule to break, and the critic will mark it either way. Blocks M3.

5. **The newest spec clauses are missing from the base branch, and the bundle format has no machine-readable schema** (`claude/m0-foundation:docs/specs/resource-grants-v1.md`, `approval-service-v1.md`, `security-v1.md`; `schemas/`)
   - **What this is:** GR-11a (bundle format), GR-13a (12 h re-sign, anti-rollback), AS-06a (service JWT `kid`) and the SEC-22 pinned digest exist only on `claude/admiring-planck-vzys0x`, `claude/m1-connector-kit` and `claude/m2-policy-tooling`. `main` has no specs at all and is not an ancestor of `claude/m0-foundation`.
   - **Problem:** `git merge-tree` shows that merging the orchestrator branch into `claude/m0-foundation` conflicts in `docs/implementation-plan-v1.md`. The bundle is written by M2 (`gxg-policy build`) and read by M1 (loader), two agents on two branches, from prose only. `schemas/` has no `grants-bundle.schema.json`, no `.sig` schema and no sample signed bundle. Case: M2 writes `tenants.<id>.approval_policy` as `null` while M1 expects the key to be absent, or one side signs pretty-printed JSON instead of JCS bytes. Each side's tests pass, and M3's `/readyz` fails on the real bundle. The specs' own rule says the schemas are part of the spec and a mismatch is a bug (`docs/specs/README.md`).
   - **Fix:** Merge the orchestrator spec commits into the base branch and resolve the plan conflict once. Add `schemas/grants-bundle.schema.json`, `schemas/grants-bundle-sig.schema.json` and one test vector (a fixed key, the bundle bytes, the expected signature). Then both M1 and M2 test against the same file.
   - **If we skip it:** M3 is the first place where M1 and M2 meet, and a format mismatch shows up only there. Blocks M3 (and M7, which loads the same bundle).

## Should fix

6. **`retry_after_s` is "in details" in the contract but top-level in the schema** (`docs/specs/mcp-connector-contract-v1.md:37`, `schemas/error.schema.json`)
   - **What this is:** The `rate_limited` error tells the caller when to retry.
   - **Problem:** The MCC error table says `retry_after_s` goes in `details`. `error.schema.json` defines it at the top level, and `details` is a free object. A connector that follows the table sends `{"details": {"retry_after_s": 30}}`. A client that follows the schema reads the top level, finds nothing, and retries at once. The schema accepts both forms, so no test catches it.
   - **Fix:** Change the MCC table to "top-level `retry_after_s`", and make it required when `code = rate_limited` in the schema (one `if/then`).
   - **If we skip it:** M3 (MC-03) and M7 (AC-M7-07, `retry_after_s > 0`) can pick different places.

7. **"No right to this resource" is `not_found` in one place and `resource_forbidden` in another** (`docs/specs/mcp-connector-contract-v1.md:33`, `security-v1.md:28`, `conformance-matrix-v1.md:26`, `resource-grants-v1.md:25,130`)
   - **What this is:** The answer when an agent names a chat or board it has no grant for.
   - **Problem:** GR-02, GR-17 and AC-M3-05 say `not_found`, the same bytes as "does not exist". The MCC table defines `resource_forbidden` for exactly this case, and SEC step 3 and SC-05 accept "`not_found` / `resource_forbidden`". Case: `demo.notes.search` with `board_ids: ["a-secret"]` from `t-alpha:reader`. One reading gives `resource_forbidden`, which tells the agent that `a-secret` exists.
   - **Fix:** Say in MCC that a missing right on a named resource is always `not_found` (GR-17). Keep `resource_forbidden` only where existence is already known to the caller, or delete the code. Fix SEC step 3 and SC-05 to match.
   - **If we skip it:** The M3 test and the contract disagree, and the next connector (KB, Telegram) leaks the existence of hidden resources.

8. **`demo` cannot depend on `gxg-connector-kit` as the repo is laid out** (`connectors/demo/pyproject.toml:6`, `pyproject.toml:34`, `docs/specs/demo-connector-v1.md:17`)
   - **What this is:** The demo spec says `demo` uses FastMCP and `gxg-connector-kit`, with its own lockfile and a `Dockerfile` in `connectors/demo/`.
   - **Problem:** `connectors/*` is excluded from the uv workspace. `gxg-connector-kit` is a workspace member that is not published anywhere. When M3 adds the dependency, `cd connectors/demo && uv lock` cannot find the package unless it adds a path source to `../../packages/gxg-connector-kit`, and a `docker build connectors/demo` cannot copy a path outside its context.
   - **Fix:** Decide it now in the demo spec: a `[tool.uv.sources] gxg-connector-kit = { path = "../../packages/gxg-connector-kit" }` plus a Docker build from the repo root (`docker build -f connectors/demo/Dockerfile .`). Write it in one line of the spec and in AC-M3-01.
   - **If we skip it:** The M3 executor spends its first cycle on packaging, and each later connector solves it differently.

9. **An `auto_approve` rule with `limits: {}` is valid and has no limit** (`schemas/approval-policy.schema.json:151,177`)
   - **What this is:** The schema requires `limits` on every `auto_approve` rule, so that auto-approval is always rate-bound (AP-01 p.5, AP-03).
   - **Problem:** `max_per_hour` and `max_per_day` are both optional, so `limits: {}` passes (checked). AP-01 p.5 says auto-approve works "while the limits are not used up", and an empty set never is. A prompt-injected agent with that rule sends without a human as fast as the provider allows. The contract test `test_auto_approve_requires_limits_and_no_quorum` checks only that `limits` is missing or present.
   - **Fix:** Add `"minProperties": 1` to `limits` (or require `max_per_hour`) and one negative case to the test.
   - **If we skip it:** One typo in a tenant file turns off the only brake on auto-approval. M2 lint and M7 both inherit the hole.

10. **`rule_id` is undefined when several rules match** (`docs/specs/approval-policy-v1.md:11,76,81`, plan AC-M7-05)
    - **What this is:** AP-04 writes one `rule_id` into the intent, and `policy_version = bundle_version + rule_id`. EFF-03 builds `decided_by = policy:<rule_id>@<policy_version>`.
    - **Problem:** AP-01 p.4 merges all matching `require_approval` rules, and p.5 may apply several `auto_approve` rules. Then there is no single rule. AC-M7-05 asks for the expected `rule_id` in the case "two require with different `approvals`/ttl", and the spec gives no answer. Also, when `rule_id` is already inside `policy_version`, EFF-03 repeats it.
    - **Fix:** Store `rule_ids` as a sorted list (`text[]`). Set `policy_version = bundle_version` only, and `decided_by = policy:<first rule_id>@<bundle_version>`. Or define "the rule that gave the strictest value, ties by `id`". One paragraph.
    - **If we skip it:** M7 picks an order, and the audit cannot say why an intent needed two approvers.

11. **The OpenAPI file and the spec text disagree on request bodies and ID types** (`schemas/approval-service.openapi.yaml:63,82,103,145`, `docs/specs/approval-service-v1.md:199-200`)
    - **What this is:** AC-M7-01 makes the FastAPI OpenAPI match this file exactly.
    - **Problem:** The spec table gives `cancel` the body `{on_behalf_of}`. The OpenAPI requires `{on_behalf_of, expected_version}`. The OpenAPI declares `intent_id` and `execution_id` as `format: uuid`. The schemas they map to (`execution-token.jti`, `intent-status.intent_id`, `execute-request.*`) allow any string up to 256. The status table lists 404 for a foreign tenant, and `dispatch` has no 404 in the OpenAPI. A connector built from the text omits `expected_version` and gets 400.
    - **Fix:** Pick one: either add `expected_version` to the spec table, or drop it from OpenAPI. Use `format: uuid` in the JSON Schemas too (the DB columns are `uuid`). Add 404 to `dispatch`.
    - **If we skip it:** AC-M7-01 passes against the file while the connectors (M8) follow the text.

12. **A service key cannot be rotated without an outage** (`schemas/platform-registry.schema.json:41`, AS-06a on `claude/admiring-planck-vzys0x:docs/specs/approval-service-v1.md:171`)
    - **What this is:** AS-06a: the service JWT header `kid` must equal the JWK thumbprint of *the* key of `iss` in the registry. GR-13 lets a new bundle take up to 5 min to reach all services.
    - **Problem:** The registry holds one `public_key` per service. To rotate the `demo` key, the new bundle swaps it. For up to 5 minutes some receivers have the old bundle and reject tokens with the new `kid`, and the others reject the old one. The executor's own key has an overlap rule (AS-10); service keys do not. AS-06 also promises an allowlist "service → endpoint → tenants", and the registry has no endpoint field, so the endpoint gate is only implied by `kind`.
    - **Fix:** Make `public_keys` a list of 1–2 keys (accept any `kid` in the list, sign with the newest). Write in AS-06 that endpoints are allowed by `kind`, with the table, instead of promising a field that does not exist.
    - **If we skip it:** Each key rotation for one of the ~10 tenant connectors drops calls for minutes. M1 (`svc_jwt`) and M7 build on the single-key form.

13. **The bundle freshness rules disagree with each other** (`docs/specs/approval-service-v1.md:290`; GR-13/GR-13a on `claude/admiring-planck-vzys0x:docs/specs/resource-grants-v1.md:110-111`)
    - **What this is:** GR-13a re-signs the bundle every 12 h, with a new `built_at`. GR-13/AC-M1-07: older than 24 h means not ready. Anti-rollback: reject a bundle whose `built_at` is older than the loaded one.
    - **Problem:** The alert `gxg_bundle_age_seconds > 1 h` fires about 11 hours out of every 12 when there are no policy changes. Anti-rollback compares only with the bundle in memory. After a restart there is nothing to compare, so a 23-hour-old signed bundle is accepted. Case: a grant is revoked at 10:00. Whoever serves bundles (the storage host, a misconfigured mirror) gives a restarting connector the 09:00 bundle, and the revoked grant works again until 09:00 next day.
    - **Fix:** Alert on bundle age > 13 h (one missed re-sign), not 1 h. Keep the highest `built_at` seen on local disk and compare with it at start, or make the loader fetch "latest" and refuse anything older than the current re-sign period.
    - **If we skip it:** The on-call alert is ignored because it always fires, and a revocation can be undone by a restart.

14. **The manifest schema cannot hold preview labels, and `approval_default` contradicts the class** (`schemas/gxg-manifest.schema.json:49,97,137`, `docs/specs/effects-and-approval-v1.md:181`)
    - **What this is:** EFF-30: the preview title *and the field labels* come from the manifest, the values come from the args.
    - **Problem:** The tool object has `additionalProperties: false` and only a `title`. A manifest with any labels field fails the schema (checked), so the demo connector (M8, and its M3 schemas) must hard-code labels in code, against EFF-30. On the other side, `approval_default` is required for effect tools, and the schema accepts `high_risk` with `approval_default: not_required` (checked). AP-01 never reads `approval_default`: the default comes from the class.
    - **Fix:** Add `preview: {title, fields: {<arg>: <label>}}` to the tool object. Delete `approval_default` from the schema, the example manifests and MCC-01 (the class already says it).
    - **If we skip it:** Connectors drift from EFF-30, and a reader trusts a field that does nothing.

15. **The intent-create body is weaker than the intent record it creates** (`schemas/intent-create.schema.json:12,34,38`, `docs/specs/approval-service-v1.md:56`)
    - **What this is:** The body of `POST /v1/intents`.
    - **Problem:** `resource_id` has no `minLength`, so `""` is valid. In `effect-intent` and in every grant it must be a real RID. `idempotency_key` is optional, but the column is `NOT NULL` and EFF-08 says the *connector* makes one when the agent gave none. Case: a connector omits it. The schema says OK, and the service must either fail the insert (500) or invent a key, which the spec does not allow. Signals keys like `c-1:1` (5 chars) fail `minLength: 8`, though EFF-08 tells Signals to send `action_candidate_id:version`.
    - **Fix:** `resource_id`: use the RID pattern from `resource-grants.schema.json`. `idempotency_key`: required. State the minimum length in EFF-08, or lower it.
    - **If we skip it:** M7's 400/409 tests do not cover the empty-RID path, and Signals integration (stage D) fails on short IDs.

16. **`.env.example` and `README.md` still use the unpinned tag that SEC-22 now replaces** (`.env.example:5`, `README.md:66`, SEC-22 on `claude/admiring-planck-vzys0x:docs/specs/security-v1.md:92`)
    - **What this is:** SEC-22 now names one pinned image `…:v1.1.1@sha256:16c7a898…`.
    - **Problem:** `.env.example` sets `ghcr.io/ibm/mcp-context-forge:1.1.1`: no digest, and no `v` while the pinned ref has one. `README.md` still says "the tag is a candidate, not a pinned digest". Someone who copies `.env.example` runs whatever the tag points to today, not the image that M4 checked.
    - **Fix:** Put the SEC-22 reference with the digest into `.env.example` and fix the README line (AC-M4-03 already asks for it). The orchestrator branch should not claim the pin before the file has it.
    - **If we skip it:** M5's results (stage B) can be produced on an image other than the one that was reviewed.

## Nice to have

17. **The canonical-hash vectors miss the cases where languages differ** (`schemas/test-vectors/canonical-hash.json:3`)
    - **What this is:** EFF-05 and EX-22 require every language (a TS kit is planned) to give the same bytes.
    - **Problem:** The vectors have only ASCII keys and catch floats only with `19.99`. In JavaScript `1.0` and `1e2` parse as integers, so a TS kit accepts them while Python rejects `1.0`. JCS sorts keys by UTF-16 code units, and Python's `sort_keys` sorts by code points, so keys with characters outside the BMP sort differently. The boundary `±(2^53−1)` and `-0` are not covered.
    - **Fix:** Add 4 vectors: `{"a": 1.0}` → invalid, a valid one at `9007199254740991`, `-0` → invalid or `0` (pick one), and two keys `"ﬁ"` / `"\U0001F600"` that sort differently by UTF-16 and by code point, with the JCS order.
    - **If we skip it:** The first TS implementation passes all 6 vectors and still gives a different hash for real input, and its effects fail with 422 (EX-05).

Smaller items left out: 3 (`error.schema.json` does not tie `retryable` to `rate_limited`/`timeout_before_start`; the UDP `sendto` is not blocked by `conftest.py`; the spec's policy example names `-1001234567890` while `policies/approval/example.yaml` uses `-1009876543210`).

Verdict: The CI and the contract tooling are healthy. Fix 1–5 first: 2, 3 and 1 before M7 starts, 4 and 5 before M3 starts.

Lean: -10 lines possible (finding 14: `approval_default`), 0 dependencies.

Not checked:
- The code on `claude/m1-connector-kit`, `claude/m2-policy-tooling` and `claude/m4-*`. Only whether their specs contain GR-11a/GR-13a/AS-06a was checked, not whether M1 and M2 already agree on the bundle format.
- GitHub settings: branch protection on `main`, and the CODEOWNERS state in the UI (AC-M0-03 is a manual check). CI runs on GitHub were not re-run; only the local `scripts/ci/check-all.sh` was.
- That the SEC-22 digest exists in `ghcr.io` and is free of open advisories (no registry access was attempted).
- `docs/specs/roadmap-v1.md`, `service-integration-v1.md`, `target-platform-v1.md` and `docs/adr/0001-identity-propagation.md` were skimmed for IDs and roles, not read line by line. `tests/ci/*` and `scripts/ci/prove_schema_guard.py` were read in part; their results were checked by running them.
- `reports/*` (process records) and `.claude/skills/*` were not audited.
