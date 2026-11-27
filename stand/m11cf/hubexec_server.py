# Minimal dummy MCP server for the ADR-0007 condition: one ordinary tool named `hub.script.run`
# (owner decision 2026-11-26: renamed from `hub.execute`, which EFF-02 refuses). Runs nothing.
from fastmcp import FastMCP

mcp = FastMCP("hubexec")


@mcp.tool(
    name="hub.script.run",
    description="Dummy facade of the script executor (ADR-0007): echoes the script length, runs nothing.",
)
def hub_script_run(script: str) -> dict:
    return {"ran": False, "script_len": len(script)}


mcp.run(
    transport="http",
    host="0.0.0.0",  # noqa: S104 - its own container on the internal network
    port=8000,
    path="/mcp",
    stateless_http=True,
)
