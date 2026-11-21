"""KB wrapper connector (M6), stage B: read-only tools over the KB (docs/specs/service-integration-v1.md).

`server` (FastMCP app, /healthz, /readyz, configuration), `tools` (identity, grants, the three read tools),
`backend` (fake-KB until the gap audit, docs/audits/kb-gap-audit.md).
"""

__all__ = ["__version__"]

__version__: str = "0.1.0"
