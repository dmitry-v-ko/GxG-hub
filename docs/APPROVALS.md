# GxG Hub — Human Approval Architecture (target v1)

**Status:** target design / decision proposal; not implemented. This document is for the **new GxG Hub** and does not inherit an old project's approval implementation.

## Architectural decision

**Human approval belongs to a dedicated GxG Approval Service (control plane) alongside ContextForge** — not inside an MCP connector, not inside ContextForge's upstream code, and not in the agent's prompt. **GxG Action Gate** is a trusted policy/enforcement layer: it intercepts an effectful invocation, determines whether approval is required, and prevents execution while approval is pending. It is our adapter, not an assumed native ContextForge feature. The service can initially run as a small module in the same deployment, but owns a separate responsibility and durable state.

- **ContextForge:** authenticates the caller, enforces tool-level grants and routes authorized MCP calls; it must not become the human review UI or a long-running business workflow engine.
- **Action Gate:** applies effect and approval policies to a trusted invocation; creates pending requests using Approval Service and prevents bypass. The technical placement must be proved in the chosen ContextForge integration.
- **Approval Service:** creates, persists and expires approval requests; verifies the human approver's authority; records decisions; issues a narrowly scoped, short-lived authorization/approval grant or exposes a trusted server-to-server decision check.
- **Calling business application / orchestrator:** owns the user's business task and pauses/resumes that task. It creates the approval request through the trusted backend when a proposed action needs approval.
- **MCP connector / executor:** checks its own resource-level authorization and, for sensitive effects, validates an approval grant or a trusted approval decision before any side effect. It owns execution and idempotency, not human interaction.
- **Human-facing UI:** an Approval Inbox (later optionally embedded in a tenant workspace); it calls the Approval Service. ContextForge's admin UI is not the end-user approval surface.

The two controls are complementary: **capability authorization is not human approval**. A human's approval does not elevate an agent's/tenant's existing privileges.

## Flow (asynchronous, no long-running MCP request)

1. Agent/workflow proposes an effect with a canonical request: tenant, authenticated caller, exact operation/version, connection/resource, concrete arguments and idempotency key.
2. The Action Gate / trusted policy application classifies the effect. If the caller has no capability grant, **deny**; do not ask a human to override missing permissions.
3. If approval is required, **do not invoke the effect**. The trusted backend opens an approval request, preserving the canonical payload or a secure reference and hash. Return `approval_required` plus a request ID to the calling application.
4. The Approval Inbox shows **who** is asking, **what** will happen, **which exact account/recipient/resource**, and relevant impact. An authorized human chooses approve or reject.
5. Approval Service stores the approver's identity, decision, timestamp, expiry and original payload hash. Approval is **bound** to tenant, caller/delegated task, action, resource, parameter hash and max uses (normally one).
6. The application resumes the workflow using the **same canonical intent**, re-checking grants and policy; the gateway/admission component and connector enforce the relevant checks. The connector must reject a missing, expired, consumed or mismatched approval.
7. The executor performs the action once (or reconciles a previous uncertain outcome), records the receipt/outcome and marks the approval consumed. Audit events correlate approval ID, invocation ID and business-task ID **without leaking private payloads**.

**Important integration constraint:** ContextForge must not be assumed to support approval enforcement out of the box. The GxG policy/admission adapter or connector must implement and test the gate. If it cannot be proved end-to-end, effects stay disabled.

## Invariants

- Default deny on missing approval, bad identity, stale or changed arguments, expired policy, and direct connector access that bypasses the trusted gate.
- An agent cannot self-approve, fabricate a reviewer, pass `approved: true`, or use a raw model-supplied `approval_id` as authorization.
- Human approval must be obtained from an authenticated, authorized **person** acting within the correct tenant; organizational approval policy may require multiple reviewers.
- Approval can never bypass tenant, connection, chat, document or CRM-record permissions.
- Different action payload/recipient/account => **new approval**. Revocation, expiry, denial or cancellation => no execution.
- For writes/sends, require idempotency and reconcile uncertain outcomes rather than blindly retrying.
- Secret values, access tokens, full private chat content and unnecessary personal data do not belong in approval notifications or logs.
- Technical execution, approval history and business workflow progress are separate states, linked by correlation IDs.

## Minimal proposed data contract

`ApprovalRequest`: `id`, `tenant_id`, `requester_principal`, `business_task_id`, `tool_key`, `tool_version`, `target_resource`, `payload_hash`, `payload_ref`, `idempotency_key`, `reason`, `status`, `created_at`, `expires_at`.

`ApprovalDecision`: `request_id`, `decider_principal`, `decision`, `decided_at`, `policy_version`, `comment_ref?`.

States: `pending` → `approved | rejected | expired | cancelled`; successful authorized execution records `consumed` and an execution receipt. Approval alone is **not** proof the effect happened.

## Acceptance tests

- Read-only tool requires no human review unless tenant policy explicitly says otherwise.
- A permitted high-risk `telegram.send` produces a pending approval and **zero messages** before approval.
- Approver without the tenant role cannot approve; originating agent cannot approve itself.
- Approved request with changed chat, text, tenant, account or payload hash is rejected.
- Expired/rejected/replayed authorization cannot cause an external side effect.
- Repeated/resumed calls with the same idempotency key cannot send twice, including after a timeout.
- Direct network invocation of a protected connector cannot bypass enforcement.
- Denied capability stays denied even after a forged or genuine approval.

## Boundary for Signal Intelligence

Signal Intelligence may ask for approval before an external action, but **Signals retains its business lifecycle, intent, evaluation, candidate and outcome**. The shared Approval Service is only the approval authority; ContextForge is only the MCP access/routing layer. The same approval mechanism is reusable for Telegram, CRM and publication tools.
