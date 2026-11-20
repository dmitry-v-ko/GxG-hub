# GxG Hub — Conformance & Security Test Matrix v1

**Status:** required acceptance plan; only JSON Schema smoke tests exist (`tests/contract/test_envelopes.py`). All live ContextForge, provider and approval tests below are **NOT EXECUTED**.

## Local schema smoke test

```bash
python -m pip install -r tests/contract/requirements.txt
python -m unittest discover -s tests/contract -p 'test_*.py' -v
```

This validates GxG domain envelope examples, NOT a live MCP server, authentication, identity forwarding, permissions or runtime security.

## Enforced acceptance matrix

| ID | Phase | Test | Required result |
| --- | --- | --- | --- |
| SC-01 | P0 | Two tenants, two agents with distinct credentials; list tools + direct calls | Correct least privilege; hidden tool invocation independently rejected |
| SC-02 | P0 | Anonymous, expired, wrong-audience token; spoof tenant/agent headers/arguments | 401/403 (or MCP equivalent); zero downstream effect; safe audit |
| SC-03 | P0 | Signed identity forwarding with tampered signature, stale expiry, replay, wrong issuer/audience | Connector rejects before credential/provider lookup |
| SC-04 | P0 | Authorized user of tenant A requests tenant B chat/account/knowledge collection | Deny at gateway/connector; zero provider calls |
| SC-05 | P0 | Read-only principal invokes effect tool by direct ID or raw connector URL | Deny even if discovery hid tool; private endpoint inaccessible |
| SC-06 | P0 | Tool pre-hook removed, crashes, times out or runs in permissive mode | Effect disabled/fail closed, no external call |
| SC-07 | P0 | Action Gate applies 'approval_required'; forged `approved:true`, untrusted `approval_id` or prompt injection | No effect; pending request, no privilege escalation |
| SC-08 | P0 | Wrong/expired/revoked approval, changed recipient/message/account/tool version | Rejected before effect; new intent required |
| SC-09 | P0 | Fake approver/agent self-approval/cross-tenant approver/CSRF or stale UI | Rejected; separate human authentication and policy |
| SC-10 | P0 | Two simultaneous workers reserve one approved intent | Exactly one reservation; no concurrent duplicate dispatch |
| SC-11 | P0 | Crash/timeout after provider submit, response lost | `outcome_unknown`, reconcile; never blind repeat |
| SC-12 | P0 | SSRF through connector URL, OAuth discovery, redirect, metadata service | Deny disallowed hosts/IPs even after DNS redirects |
| SC-13 | P0 | Model injects a tool call in Telegram text/KB search result | Treated as untrusted content, never new grants or approvals |
| MC-01 | P1 | MCP negotiate/discover/invoke against pinned ContextForge and reference connector | Correct input/output schemas and stable version registration |
| MC-02 | P1 | Search result schema, paging, valid empty result, response size limit | Valid typed data; empty success doesn't trigger fallback |
| MC-03 | P1 | Transport failure vs provider 429/timeout vs unknown external effect | Canonical errors; retries only for safe cases |
| MC-04 | P1 | Provider credential isolation, redaction, secret rotation and revocation | No secret leakage, old credentials no longer accepted |
| MC-05 | P1 | ContextForge upgrade, plugin configuration, virtual server and alias variations | Gate intercepts all effect paths; permission tests still pass |
| SI-01 | P1 | Source collect through Hub → Signals SourceInbox/evaluation/candidate | No loss/deduplication errors; domain state outside gateway |
| SI-02 | P1 | Action Candidate → Hub/gate/approval → test provider → Signals Outcome | Single authoritative dispatcher; durable outcome and auditable correlation |
| OPS-01 | P2 | Restart after pending approval/reserved action/unknown outcome | Durable resume or reconciliation without double dispatch |
| OPS-02 | P2 | Policy change, admin compromise, credential revocation, emergency effect kill switch | Block future actions immediately; audit change |

## Release gates

- **Stage A: docs/schema**: canonical spec + validated example schemas + smoke tests in CI. This is the only stage that can begin without running ContextForge.
- **Stage B: read-only PoC**: SC-01..05, SC-12..13, MC-01..04 must pass with synthetic data; add signed identity/connector checks. Do not connect personal Telegram until safe.
- **Stage C: effect sandbox**: SC-06..11, MC-05, SI-02 must pass with *fake provider* capable of counting dispatches, blocking duplicates and simulating timeouts.
- **Stage D: production candidate**: hardening, backup/restore, incident response, secret management, audit retention, change approval, and operational tests; operator go/no-go.

Each test result should record: commit, ContextForge image digest, MCP protocol/SDK versions, policy version, caller identity fixture, expected/actual, sanitized evidence, reviewer and timestamp. Mark each test `not_run` / `passed` / `failed`; do not label any production ready based on a checklist alone.

## Implementation strategy

First build a synthetic read-only MCP server plus a **fake effect tool** with a counter. For approval races, use two concurrent workers against the same intent and transactionally assert one reservation. For direct bypass, call the connector endpoint both with and without the trusted gateway. For an unknown outcome, have the fake provider commit once and drop the response; caller must go to reconciliation and not automatically resend.

See [Security and Action Gate](security-and-action-gate-v1.md), [Approval State Machine](approval-state-machine-v1.md), and [Connector Contract](mcp-connector-contract-v1.md).