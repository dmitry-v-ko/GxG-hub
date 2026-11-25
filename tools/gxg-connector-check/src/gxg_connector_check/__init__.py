"""Conformance suite of GxG MCP connectors (docs/specs/mcp-toolkit-v1.md «Общий conformance-набор», plan M1.1).

`check-connectors --connector <dir>` runs the checks of the Conformance Matrix that apply to one connector
(MC, SC, EX, MCC-05, MCC-09) against the connector's own `Harness` (`[tool.gxg-connector-check] harness` in its
pyproject.toml), in process, with the official MCP SDK client over an ASGI transport.
"""

from .harness import Call, Harness, Running

__all__ = ["Call", "Harness", "Running"]
