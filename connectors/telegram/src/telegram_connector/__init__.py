"""Telegram MCP connector, stage D read-only (M11): a facade over the existing Telegram service's domain ports.

`service` (the port and its result types), `fake` (the fake service of the test profile), `tools` (identity,
grants, the three read tools), `server` (FastMCP app, /healthz, /readyz, configuration), `testctl` (test only).
The connector never opens a Telegram session: the existing service is the only session owner.
"""

__all__ = ["__version__"]

__version__: str = "0.1.0"
