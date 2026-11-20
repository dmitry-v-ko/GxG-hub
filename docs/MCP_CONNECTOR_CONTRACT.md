# MCP Connector Contract v1 — GxG Hub (draft)

**Status:** proposed normative GxG integration rules, not evidence of implementation. MCP itself is an external protocol; this document only adds conventions for our platform.

## Why

Any standards-compliant MCP server can in principle be registered with ContextForge. GxG needs additional consistency across Knowledge, Signals, Telegram and future tools: a **security and operations contract**, not a home-grown MCP protocol.

## 1. Interface

- Publish only well-described business capabilities as MCP tools; use stable unambiguous names, versioned semantics and machine-readable `inputSchema` / `outputSchema` where supported by the selected SDK and protocol version.
- Prefer structured results (`structuredContent` when supported) with explicit typed fields. Do not use free-form prose as the only output for a declared structured operation.
- Names and types must be tested for compatible updates; breaking changes require a new capability version. Existing clients are not automatically migrated.
- Return source references and pagination/cursor/partial-result indicators for retrieval tools where relevant; never disclose sensitive underlying data beyond the caller's scope.
- Bound response size, pagination, concurrency, per-tool timeout and cancellation behavior.

## 2. Caller identity and authorization

- Trust **only authenticated** tenant and principal identity from a proven gateway-to-connector mechanism; never infer authority from agent-provided tool arguments (`agent_id`, `tenant_id`, `role`).
- ContextForge checks capability/tool grants; connector independently checks tenant, connection, and resource-level permissions (specific Telegram chats, knowledge collections, accounts).
- Validate signed forwarded identity/claims or use a trusted introspection/service-to-service mechanism, depending on verified ContextForge support. No implicit identity propagation.
- Every external credential is tenant-scoped and held in a secret manager/connector runtime, not in MCP tool arguments, responses, prompts or repository files.
- Direct backend endpoint exposure must not bypass the same checks. Deny by default.

## 3. Effects and human approval

Classify every tool as `read`, `write_reversible`, `external_effect` or `high_risk`. Declare any preconditions and idempotency guarantees.

- Human approval for an effect is a **separate service and control-plane decision** per [Approval Architecture](APPROVALS.md), not an MCP protocol feature and not a ContextForge UI task.
- The caller/orchestrator handles pending/resume. The effect MUST NOT execute while approval is pending.
- The execution gate must verify the effective agent/tenant rights **and** approval bound to the exact action, resource and argument digest. No `approved: true` flag from an agent.
- For sending, publishing, CRM changes and other effects, require an idempotency key, durable receipt and reconciliation of unknown outcomes. Avoid blind technical retries.

## 4. Error and result semantics

A connector must distinguish, via a documented structured error contract, at least:

- `invalid_input` / `unsupported_version`
- `unauthenticated` / `forbidden` / `resource_out_of_scope`
- `approval_required` (no side effect yet) / `approval_rejected` / `approval_expired`
- `rate_limited` / `temporarily_unavailable` / `timeout`
- `unknown_outcome` (may have executed; reconciliation required)
- `internal_error`

Map those conditions appropriately into MCP error and tool-result conventions of the chosen protocol/SDK; **not** every tool error is a protocol failure. Document `retryable` only where safe and prove effect behavior.

## 5. Routing, business process, observability

- The connector can choose among technical providers and do safe technical fallback; ContextForge catalogs, authenticates and routes MCP tools. The calling agent judges semantic completeness.
- Long-running business orchestration and domain records belong to their owning services. In particular, **Signal Intelligence both consumes source tools through Hub and invokes effect tools through Hub**, but owns its own scoring, candidates, and outcomes.
- Correlate `tenant`, `principal`, `tool_key/version`, `invocation_id`, `business_task_id?`, `approval_request_id?`, policy decision, latency and result category in safe audit events.
- Redact private payloads and credentials. State support and compatibility with specific tested MCP / SDK / ContextForge versions.

## 6. Conformance test suite (required before enabling)

1. Tool discovery and valid/invalid JSON Schema input/output.
2. Unauthorized direct tool calls denied, even if discovery hides the tool.
3. Forged identity/tenant, foreign resource, incorrect connection and missing scopes denied.
4. Cross-tenant data and credentials never observed.
5. Stable error codes, bounded timeouts and safe retry behavior.
6. Approval required, denied, expired, changed-payload and replay cases are blocked for effectful tools.
7. Exactly-once **business effect** is not claimed without idempotency/reconciliation tests.
8. Audit correlation exists with no accidental disclosure of message bodies or secrets.

## 7. Existing vs planned adapters

- **Knowledge:** audit the existing MCP facade before connecting; do not rewrite the database or assume it meets this contract.
- **Telegram:** keep the session/archive as a single existing owner; start with scoped read tools; add send only after approval + idempotency tests.
- **Signals:** keep Mastra/domain process; expose narrow read capabilities to other agents if needed; route source collection **and** later external effects via GxG Hub.

**Open items to validate experimentally:** actual supported ContextForge identity forwarding, custom admission extension points, MCP protocol revision/SDK selection, signed approval verification path and resource authorization model. Until validated, connectors that perform effects remain disabled.
