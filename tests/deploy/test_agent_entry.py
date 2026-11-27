"""Owner decision 2026-11-26 ("close /rpc to agents"): the agent port of ContextForge on Railway (443, the only one
`tag:agent` reaches, SEC-26) publishes only the virtual servers and /health. ContextForge v1.1.1 plugin hooks do not
see the request path (reports/M11-cf-stand.md), so the `tailscale serve` mapping is the control. Live twin on the
stand: stand/m11cf/checks.py `agent_entry` (the same mapping in stand/m11cf/agent-entry.nginx.conf)."""

from __future__ import annotations

from scripts.deploy import railway_deploy as rd

SERVE = rd.load_spec()["services"]["contextforge"]["env"]["GXG_TS_SERVE"]
ROUTES = [line.split() for line in SERVE.strip().splitlines()]


def test_the_agent_port_publishes_only_virtual_servers_and_health() -> None:
    assert sorted(path for port, path, _ in ROUTES if port == "443") == ["/health", "/servers"]
    for port, path, target in ROUTES:
        if port == "443":
            assert target == f"http://127.0.0.1:4444{path}", (path, target)


def test_the_whole_gateway_is_only_on_the_operator_port() -> None:
    assert [(p, path) for p, path, _ in ROUTES if path == "-"] == [("8443", "-")]
