# GxG Hub — Security & Action Gate v1

**Status:** normative target design, NOT a deployed or validated security mechanism. 2026-11-20.
**Authority:** this file defines identity, trust boundaries and effect enforcement. Connector profile: [MCP Connector Contract](mcp-connector-contract-v1.md); approvals: [Approval State Machine](approval-state-machine-v1.md).

## Threat and intended guarantees

The system must reject an **unknown person, rogue agent, stolen/forged low-privilege credential, cross-tenant caller, direct connector caller, and a replayed approval** before it can trigger Telegram sends, CRM writes, publications or other real-world effects. An attacker might control tool arguments, MCP message contents, HTTP headers or retrieved prompt text; none of those confer identity or permissions.

**No software architecture can promise that nobody will ever compromise a system.** Our concrete acceptance claim is narrower: all production effect paths enforce authenticated actor + tenant + scoped tool/resource grant + applicable human approval + replay protection, and fail closed on missing evidence.

## Enforcement pipeline

1. **Trusted ingress:** HTTPS/TLS, validated agent identity from a configured IdP or per-agent machine credential, verified issuer/audience/expiry (and key rotation). Remove client-supplied forwarded-identity, upstream-authorization and approval headers; enforce rate limits. Admin endpoints are on a separate restricted interface. Do not expose arbitrary server registration to tenants by default.
2. **ContextForge:** enforce tool-level RBAC and tenant-bound gateway/connection configuration. Never assume hide-from-discovery prevents direct tool invocation.
3. **Action Gate:** candidate implementation is the supported ContextForge `tool_pre_invoke` hook in strict enforcement mode. It classifies the concrete tool and parsed payload, evaluates policy, and returns DENY / ALLOW / APPROVAL_REQUIRED. If a required hook is missing, unloaded, times out, errors, or has no trustworthy principal, **deny**. Hook availability is documented upstream, but these guarantees need an integration PoC across every invocation path.
4. **Approval Service:** for APPROVAL_REQUIRED creates a durable request after server-side canonicalization, captures decision by a separate authenticated human, and issues a short-lived action-bound authorization or provides a trusted introspection call. Agents never approve themselves. Details: [Approval State Machine](approval-state-machine-v1.md).
5. **Connector/runtime:** a second mandatory enforcement point. Validate verified tenant/principal claims, resource grants, effect-class policy, exact request hash, approval status and execution reservation before touching external credentials or providers. Do not expose a public direct path that bypasses this. Service-to-service authentication does NOT mean blanket resource access.
6. **Provider:** use a distinct tenant-scoped credential, provider-side idempotency where available, bounded egress allowlists and a reconciliation operation for unknown outcomes.

## Identity contract (proposed profile, verify actual ContextForge fields)

- Principal identities are immutable machine/user IDs from the identity authority, not display names or MCP tool arguments. A human approver is a distinct principal with an explicit tenant role; agent tokens cannot call the approve endpoint.
- A trusted assertion MUST bind `iss`, `aud` (connector/gateway target), `sub` (principal), `tenant_id`, `scopes`, `iat`, `nbf`, `exp`, `jti`, `kid` or equivalent. Verify authenticity, issuer, expiry, audience, tenant membership and, where appropriate, jti replay. Represent any delegated task ID and delegation chain explicitly and validate it at each hop.
- Downstream delegation is constrained to the intersection of actor grants, tenant grants, target connection and exact resource grants. Never trust request-supplied `agent_id`, `tenant_id`, `approved`, `approval_id`, or forwarded headers.
- ContextForge documents optional identity propagation and HMAC signing; **both are off by default**. Prove the chosen envelope, signed claims and identity-to-tenant mapping on the pinned release. Until then, do not turn on sensitive connectors. Do not accept unverified `_meta` or HTTP headers as authority.
- Prevent OAuth token passthrough: inbound client tokens must be audience-bound; credentials for upstream provider APIs must be distinct tokens or explicitly scoped exchange results, not raw inbound tokens.

## Approval is not authorization

- First evaluate whether the **tool and resource** are allowed. Approval only adds an extra condition; it cannot grant a forbidden tool or cross-tenant resource.
- A valid human decision is bound to `{tenant, requester, tool+version, connection, resource, canonical_args_hash, business_task_id, idempotency_key, policy_version, expiry, max_uses}`.
- An app must not reconstruct modified arguments after approval. If the connection, recipient, exact message, price or other effect parameters change, require a new approval.
- The Approval Service must atomically reserve an approved intent **before** the connector executes; use a unique idempotency key per effect. Once reserved/dispatching, concurrent requests cannot reserve the same intent again. Unknown provider outcomes go to reconciliation, not blind retry.
- Require a trusted human review session, role/resource checks and step-up MFA for high-impact policies. Protect approval APIs against CSRF and forged webhooks. No untrusted Telegram text can be treated as an approval command without an independently authenticated interaction.

## Attack paths that must be tested

| Attack | Required result |
| --- | --- |
| Anonymous client invokes an effect tool | Rejected at ingress, no provider call |
| Agent with read-only grant invokes an effect by direct tool ID | Rejected even without discovery |
| Forged tenant, `agent_id`, forwarded headers or `approved: true` | Rejected; log safe security event |
| Stolen token for another MCP audience | Rejected for incorrect audience |
| Cross-tenant account/chat/document requested | Rejected before credential lookup |
| Authorized agent requests operation beyond resource scope | Rejected before provider call |
| Agent attempts human approval endpoint | Rejected; human role + step-up check |
| Approved message recipient/body changed | Rejected, new approval required |
| Approval expired, revoked, replayed or consumed | Rejected |
| Two workers execute same approval concurrently | Exactly one reservation; no unbounded duplicate external send |
| Gate plugin disabled, throws or times out | Effect blocked / connector fails closed |
| Agent bypasses gateway to connector IP/URL | Network denied or connector independently rejects |
| SSRF through URL-based connector registration | Reject non-allowlisted hosts/IPs, redirects and private network targets |
| Source document/Telegram message contains tool-call instructions | Treat as untrusted data; cannot gain execution authorization |

## Network and operations requirements

- Internal connectors, Approval Service and database are **not public**. Bind internal services to private network; enforce firewall/security group and network policies. For permitted service-to-service traffic use mTLS or authenticated workload identity with narrow audiences.
- Restrict egress of gateway and connectors. Deny localhost, metadata IPs, RFC1918 ranges and unwanted redirects for any user-configured remote endpoint; an explicit operator allowlist is the default.
- Separate administrators who configure policies from human approvers. Restrict registration/change of tools, downstream connections and policies; log every change with identity, timestamp, before/after hash, and alert on policy weakening.
- Use a managed secret store or encrypted scoped credentials and a documented rotation/revocation procedure. Never place provider tokens in prompts, approval UI, Git, raw traces or shared tool responses.
- Audit metadata must correlate request/decision/execute with redacted payloads. Retention, backup/recovery and incident response are required before production; never infer approval or execution success from logs alone.
- Implement a *global effect kill switch*, per-tenant effect disablement and emergency token revocation. Default effect-tool registration = disabled until security PoC passes.

## ContextForge validation gate

The IBM plugin docs describe `tool_pre_invoke`, `enforce` mode and identity propagation, but are not proof that our full Action Gate is effective. Before release, test across direct tools, virtual servers, tool-name aliases, delegated agents, API/HTTP paths, plugin failure, upgrades and direct connector endpoints. Validate what fields are authenticated in the hook context and how policy results are returned. If the hook cannot provide complete coverage, place a mandatory external admission adapter on the effect path **and** preserve connector enforcement; do not quietly fall back to permissive execution.

## References

- [IBM ContextForge plugin hooks](https://ibm.github.io/mcp-context-forge/1.1.x/using/plugins/)
- [IBM ContextForge identity propagation](https://ibm.github.io/mcp-context-forge/1.1.x/manage/identity-propagation/)
- [IBM ContextForge configuration](https://ibm.github.io/mcp-context-forge/latest/manage/configuration/)
- [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [IBM security bulletin on upstream credential override (CVE-2026-110279)](https://www.ibm.com/support/pages/node/7296490)
