# GxG Hub — MCP Connector Toolkit (target)

**Status:** target structure and deliverables. Except for the ContextForge Docker configuration, the items here are **not yet implemented**.

## Four parts of the platform

1. **IBM ContextForge**, used *unmodified* from a tested, pinned upstream container release/digest. We do not vendor or fork its source.
2. **MCP development toolkit:** official TypeScript MCP SDK for TypeScript codebases and a maintained FastMCP framework for Python; templates for both where beneficial. Prefer the SDK already natural to the existing service.
3. **Connector library:** independently deployable adapters for Knowledge, Telegram and Signal Intelligence; connector internals may call existing HTTP APIs, Mastra services or other existing systems.
4. **GxG MCP Connector Contract v1 + automated conformance tests:** [specification](MCP_CONNECTOR_CONTRACT.md) and [approval architecture](APPROVALS.md).

## Suggested repository target

```text
GxG-hub/
  compose.yaml                     # ContextForge upstream image, pinned and verified
  docs/
    ARCHITECTURE.md
    MCP_CONNECTOR_CONTRACT.md
    APPROVALS.md
    CONNECTOR_TOOLKIT.md
    MIGRATION.md
    POC.md
  toolkit/
    templates/
      python-fastmcp/             # planned starter, thin service template
      typescript-mcp/             # planned starter, thin service template
    scripts/
      new-connector               # planned generator/CLI
    tests/
      conformance/                # planned reusable contract tests
  connectors/
    knowledge-mcp/                # planned facade/integration; reuse existing MCP
    telegram-mcp/                 # planned read first, send gated
    signals-mcp/                  # planned narrow interface, not Signals workflow engine
  approvals/                      # planned separate approval control-plane module/service
  policies/                       # capability/resource/effect policies
```

The generator should ask for name, language, read/effect classification, inputs, resource scopes and chosen framework. Output: MCP skeleton, examples of authenticated identity extraction, result/error mapping, audit, health/readiness, Docker/test harness and contract-test fixtures. No credentials or working external effect tools by default.

## Work order

1. Finalize contract and approval gate design; confirm an upstream ContextForge version without known critical security issues.
2. Implement a **read-only reference MCP server**, generator and conformance tests. Prove validated identity propagation/tenant isolation.
3. Audit and attach existing Knowledge MCP.
4. Attach Telegram scoped read tools, without moving or duplicating archive/session.
5. Integrate Signal Intelligence's source-collection and action-execution **boundaries** through Hub, leaving evaluation and workflow state in Signals.
6. Add the shared approval service and first guarded, idempotent effect; only then enable outbound Telegram/CRM/publication.

Do not claim a deployed service, working toolkit or production security until tests and deployment verification exist.
