# GxG Human Approvals — overview

**Status:** target design only, not implemented. New GxG Hub, not the legacy Human Inbox.

The separate **GxG Approval Service** holds durable requests and genuine human decisions; **Action Gate** checks approval policy before effect tools are called; the business workflow pauses/resumes its own task; connectors independently validate execution reservations and tenant/resource authorization. A human decision does not grant an agent extra rights. No effect can proceed if any enforcement point is missing or broken.

**Normative source of truth:** [Approval State Machine v1](specs/approval-state-machine-v1.md) (immutable intent, decision, atomic reservation, reconciliation). Trust boundaries are defined in [Security & Action Gate v1](specs/security-and-action-gate-v1.md); tool requirements in [Connector Contract v1](specs/mcp-connector-contract-v1.md), with [Acceptance Matrix](specs/conformance-matrix-v1.md).

ContextForge's `tool_pre_invoke` hook is the first integration candidate, not yet a verified deployment. Its fail-closed, identity and path coverage must be proven; protected connectors also deny direct bypass. The Approval Service is NOT the ContextForge Admin UI, and no approval is accepted just because a model supplies an `approval_id` or `approved:true`.
