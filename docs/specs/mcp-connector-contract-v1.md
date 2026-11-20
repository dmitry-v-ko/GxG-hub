# GxG MCP Connector Contract v1

**Status:** единственный нормативный контракт MCP-коннекторов нового GxG Hub; реализация и прохождение тестов не подтверждены. 2026-11-20.

**Правило приоритета:** этот документ определяет интерфейсы и термины. [Security & Action Gate](security-and-action-gate-v1.md) определяет доверие/разрешения, [Approval State Machine](approval-state-machine-v1.md) — жизненный цикл согласований, [Toolkit](mcp-toolkit-v1.md) — разработку. В `docs/MCP_CONNECTOR_CONTRACT.md` остаётся только указатель; конкурирующих требований нет.

## Что стандартизируется

MCP — протокол discovery/invoke; GxG application profile — требования к именам, схемам, идентичности, результатам, побочным эффектам и тестам. Не добавлять обязательные нестандартные поля в MCP wire protocol; security context передавать только доверенным проверяемым каналом.

## Canonical vocabulary

- **Effect class** (в metadata/manifest, не в необязательном аргументе клиента): `read`, `write_reversible`, `external_effect`, `high_risk`. `read` не вызывает изменения бизнес-состояния/отправки. `write_reversible` — управляемая внутренняя запись; `external_effect` — отправка/публикация/запись вне GxG; `high_risk` — затрагивающие деньги, большие объёмы, необратимые или особо чувствительные действия.
- **Approval policy:** `not_required`, `required`, `policy_decides`; конкретное правило действует серверно по tenant/tool/resource/role/risk. Высокий риск может требовать двух человек и/или MFA. Approval не заменяет разрешение на tool.
- **Error code** (единый словарь): `invalid_input`, `unsupported_version`, `unauthenticated`, `permission_denied`, `resource_forbidden`, `approval_required`, `approval_rejected`, `approval_expired`, `rate_limited`, `temporarily_unavailable`, `timeout_before_start`, `outcome_unknown`, `internal_error`. Прежние алиасы `forbidden`, `unknown_outcome`, `timeout` в новых контрактах не использовать. Map into correct MCP protocol/tool error structure for selected version.
- **Outcome:** `succeeded`, `failed_before_effect`, `outcome_unknown`. Never call an attempted external effect an unambiguous failure after a timeout without proof.
- **Canonical serialization:** validate against a schema, then canonicalize normalized JSON deterministically before server-side hashing (e.g. RFC 8785/JCS with defined Unicode and number behavior). Include tool/version, tenant, principal, account/resource and connection in the protected intent envelope; approval must bind to same values.

## Requirements

**MCC-01 — Discovery and versioning.** Tool MUST have stable unique name, purpose, typed `inputSchema`, version in a GxG sidecar manifest and effect classification. Provide `outputSchema` and structured content when compatible with the pinned MCP/SDK version. Breaking changes require new major version; no opaque provider-specific passthrough with raw credentials.

**MCC-02 — Authentication and identity.** Request MUST bind to verified tenant and immutable caller principal; transport trust, issuer/audience/expiry, signing/rotation and delegation chain are described in [Security](security-and-action-gate-v1.md). Model-provided `agent_id`, `tenant_id`, `approved`, raw forwarded HTTP headers or opaque approval IDs never prove authority. Downstream MUST fail closed when signed claims/introspection are absent.

**MCC-03 — Resource access.** Permission is intersection of tenant, caller, tool, connection, exact resource, scopes and risk policy. Connector MUST check e.g. specific chat/document/CRM account before using provider credentials, and reject direct-network calls that lack equivalent trusted context. No token passthrough or cross-tenant provider credential reuse.

**MCC-04 — Structured results.** MCP results SHOULD use `structuredContent` for typed output on compatible clients. Search operations MUST use a bounded result contract matching [search result JSON Schema](../../schemas/search-result.schema.json) or a documented compatible domain-specific schema. Empty success is not a technical failure; include `next_cursor`, completeness and source references when relevant. Raw message history and secrets never leak through generic audit.

**MCC-05 — Errors.** Implement the canonical error codes above with a stable machine-readable envelope and map them to standard MCP tool errors (`isError` where appropriate), not always protocol errors. `timeout_before_start` is only used with reliable proof that no provider request occurred. `outcome_unknown` is never automatically retryable.

**MCC-06 — Routing and fallback.** Inside a domain connector, technical fallback may choose an approved provider for safe transport failures. It MUST NOT widen resource grants, trigger automatic semantic deep search on a valid empty result, or retry non-idempotent effects after an unknown outcome. Agent judges semantic adequacy.

**MCC-07 — Side effects.** Every effect tool MUST have immutable intent/parameter validation, tenant-scoped resource authorization, explicit approval decision when required, a unique idempotency key and durable execution receipt. Before dispatch the executor MUST obtain an atomic reservation from the trusted approval/execution authority. See [effect intent schema](../../schemas/effect-intent.schema.json), [effect result schema](../../schemas/effect-result.schema.json) and [Approval State Machine](approval-state-machine-v1.md). A model-supplied `approval_id` is never a credential.

**MCC-08 — Reliability, cost and audit.** Finite timeouts, response limits, batch size, rate/cost limits and provider-specific flood-waits. Correlate invocation/task/approval/outcome IDs across services, redact credentials and private messages. Document retention and recoverability; technical telemetry is not a business source of truth.

**MCC-09 — Publication gate.** Before registering a connector, execute contract/integration tests across two tenants, two agents, invalid schemas, forged IDs/headers, exact resource denials, errors and retries, plugin crash/omission, direct-to-connector access, approval races and audit redaction. See [Conformance Matrix](conformance-matrix-v1.md). Unverified = disabled for production effects.

**MCC-10 — Gate and human approval.** Action Gate MUST evaluate approval policy server-side and return `approval_required` without a provider call when needed. Approval Service is a **separate durable authority**, and executor MUST validate exact tenant/principal/tool+version/resource/hash/expiry/reservation. All protected effect paths must be fail-closed, including direct connector calls and plugin failure. Runtime proof is required; a `tool_pre_invoke` hook's mere existence is not enough.

## Example schemas and metadata

Machine-readable schemas are in `schemas/`. They describe domain envelopes, **not a replacement MCP wire format**. Tool-specific input/output schemas are supplied by each connector; the toolkit's validation tests use the shared envelopes and fixtures. The mandatory security and approval checks are server-side and cannot be established by schema validation alone.

## Existing systems

- Knowledge: audit and wrap the existing MCP only as necessary; don't duplicate KB or assume its current read/write keys identify agents/tenants.
- Telegram: existing session/archive owner remains; scoped reads first. Send tool off until the gate and approval tests pass.
- Signals: domain state remains in Signals; source collection is a read port through ContextForge, external actions use a separate effect port through ContextForge. Business outcomes are not owned by the gateway.

## External references

- [MCP specification](https://modelcontextprotocol.io/specification/2025-11-25)
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [ContextForge upstream](https://github.com/IBM/mcp-context-forge)
