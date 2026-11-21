"""Reference connector `demo` (docs/specs/demo-connector-v1.md), stage B: read-only tools (M3).

`server` (FastMCP app, /healthz, /readyz, configuration), `tools` (identity, grants, the three
read tools), `backend` (synthetic boards and notes, index/scan), `testctl` (test profile only).
"""

__all__ = ["__version__"]

__version__: str = "0.1.0"
