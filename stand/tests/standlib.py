"""Live stage B tests on the dev stand (compose.yaml), through ContextForge. Run by stand/run.sh.

Not in the root `testpaths`: they need the running stand (like tests/secrets needs the network).
`Stand` talks to ContextForge on 127.0.0.1:4444 as the agents of stand/secrets/state.json, reads the
audit lines `demo` writes for every tools/call that reaches it, drives `demo`'s test controls inside
its container (127.0.0.1:8090 there), and sends raw HTTP from inside the stand's networks with a
throw-away container (`probe`).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[2]
SECRETS = REPO / "stand" / "secrets"
sys.path.insert(0, str(REPO / "gateway" / "contextforge"))
sys.path.insert(0, str(REPO / "stand"))
from gxg_identity import core  # noqa: E402  the adapter's own claims and signing, for direct attacks
from init import KID_ADAPTER, env_values  # noqa: E402

CF = "http://127.0.0.1:4444"
PROTO = "2025-06-18"
PROBE_IMAGE = "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "gxg-stand")  # another project name: stand/m11cf/run.sh
NET_EDGE, NET_CONNECTORS = f"{PROJECT}_edge", f"{PROJECT}_connectors"
DEMO_URL = "http://demo:8080/mcp"
ALPHA_READER, ALPHA_NOBODY = "agent:t-alpha:reader", "agent:t-alpha:nobody"
BETA_READER, BETA_NOBODY = "agent:t-beta:reader", "agent:t-beta:nobody"
AGENTS = (ALPHA_READER, ALPHA_NOBODY, BETA_READER, BETA_NOBODY)

# stdlib only: the probe image has no packages. stdin: [{url, headers, body}], stdout: [{status, body} | {error}]
PROBE = r"""
import json, sys, urllib.request, urllib.error
out = []
for r in json.load(sys.stdin):
    req = urllib.request.Request(r["url"], data=json.dumps(r["body"]).encode() if r.get("body") is not None else None,
                                 headers=r.get("headers", {}), method=r.get("method", "POST"))
    try:
        with urllib.request.urlopen(req, timeout=r.get("timeout", 5)) as resp:
            out.append({"status": resp.status, "body": resp.read().decode()})
    except urllib.error.HTTPError as e:
        out.append({"status": e.code, "body": e.read().decode()})
    except Exception as e:
        out.append({"error": type(e).__name__ + ": " + str(getattr(e, "reason", e))})
print(json.dumps(out))
"""


def sh(
    *cmd: str,
    stdin: str | None = None,
    check: bool = True,
    timeout: float = 300,
    env_override: dict[str, str] | None = None,
) -> str:
    env = os.environ | (env_override or {})
    return subprocess.run(
        cmd, cwd=REPO, input=stdin, capture_output=True, text=True, check=check, timeout=timeout, env=env
    ).stdout


def parse(text: str) -> dict[str, Any]:
    """JSON, or the last `data:` line of an SSE body."""
    text = text.strip()
    if text.startswith(("event:", "data:")):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = datas[-1] if datas else "{}"
    out: dict[str, Any] = json.loads(text) if text else {}
    return out


def mcp_body(method: str, params: dict[str, Any] | None = None, rid: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def denied(resp: dict[str, Any]) -> str:
    """The refusal text of a ContextForge reply, or '' if it is a tool result. JSON-RPC error or isError text."""
    if "error" in resp:  # ContextForge puts the plugin's code (PDP_DENY, GXG_IDENTITY_DENIED) in error.data
        return f"{resp['error'].get('message')} {json.dumps(resp['error'].get('data'))}"
    if resp.get("result", None) == {}:
        return "empty result (SSE refusal)"
    result = resp.get("result", {})
    if result.get("isError") and "structuredContent" not in result:
        return " ".join(c.get("text", "") for c in result.get("content", []))
    return ""


def gxg_error(resp: dict[str, Any]) -> dict[str, Any] | None:
    """The connector's MCC-05 envelope (isError + structuredContent), if any."""
    result = resp.get("result", {})
    return result.get("structuredContent") if result.get("isError") else None


def ok(resp: dict[str, Any]) -> dict[str, Any]:
    result = resp.get("result", {})
    assert not result.get("isError") and "structuredContent" in result, resp
    sc: dict[str, Any] = result["structuredContent"]
    return sc


@dataclass
class Stand:
    state: dict[str, Any]
    env: dict[str, str]
    http: httpx.Client = field(default_factory=lambda: httpx.Client(base_url=CF, timeout=60))

    # ---- ContextForge as an agent
    def token(self, agent: str) -> str:
        return str(self.state["agents"][agent]["token"])

    def team(self, agent: str) -> dict[str, Any]:
        return dict(self.state["teams"][self.state["agents"][agent]["team"]])

    def tool(self, tenant: str, upstream: str) -> str:
        """ContextForge name of a demo tool in a team (alias for t-alpha's demo.boards.list)."""
        return str(self.state["teams"][tenant]["tools"][upstream]["name"])

    def headers(self, token: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        return h | (extra or {})

    def rpc(
        self,
        path: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        token: str | None,
        extra: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """One JSON-RPC request; on an MCP endpoint (`.../mcp/`) after initialize in the same session; on an SSE
        endpoint (`.../sse`) through the official SDK client."""
        if path.endswith("/sse"):
            return 200, asyncio.run(self._sse(path, method, params or {}, token, extra))
        h = self.headers(token, extra)
        if path.endswith("/mcp/"):
            init = {"protocolVersion": PROTO, "capabilities": {}, "clientInfo": {"name": "m5", "version": "0"}}
            r = self.http.post(path, headers=h, json=mcp_body("initialize", init))
            if r.status_code != 200:
                return r.status_code, {"_raw": r.text[:500]}
            if sid := r.headers.get("mcp-session-id"):
                h["Mcp-Session-Id"] = sid
            h["MCP-Protocol-Version"] = PROTO
            self.http.post(path, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = self.http.post(path, headers=h, json=mcp_body(method, params, 2))
        try:
            return r.status_code, parse(r.text)
        except ValueError:
            return r.status_code, {"_raw": r.text[:500]}

    async def _sse(
        self, path: str, method: str, params: dict[str, Any], token: str | None, extra: dict[str, str] | None
    ) -> dict[str, Any]:
        headers = ({"Authorization": f"Bearer {token}"} if token else {}) | (extra or {})
        async with sse_client(CF + path, headers=headers) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            try:
                if method == "tools/list":
                    tools = (await session.list_tools()).tools
                    return {"result": {"tools": [t.model_dump(by_alias=True, exclude_none=True) for t in tools]}}
                result = await session.call_tool(params["name"], params.get("arguments"), meta=params.get("_meta"))
            except MCPError as exc:
                return {"error": {"message": exc.error.message, "data": exc.error.data}}
            except ValidationError:
                # ContextForge answers a refused call on SSE with `result: {}`, not a CallToolResult (M5 GO note)
                return {"result": {}}
            return {"result": result.model_dump(by_alias=True, exclude_none=True, mode="json")}

    def paths(self, agent: str) -> dict[str, str]:
        """Call paths of the agent's own team: direct /rpc, global /mcp and /sse, its virtual server (MCP, SSE)."""
        server = f"/servers/{self.team(agent)['server_id']}"
        return {
            "rpc": "/rpc",
            "mcp": "/mcp/",
            "sse": "/sse",
            "virtual_server": f"{server}/mcp/",
            "virtual_server_sse": f"{server}/sse",
        }

    def call(
        self,
        agent: str,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        path: str = "/rpc",
        meta: dict[str, Any] | None = None,
        extra: dict[str, str] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": tool, "arguments": args or {}}
        if meta is not None:
            params["_meta"] = meta
        return self.rpc(path, "tools/call", params, token=token or self.token(agent), extra=extra)[1]

    def tools(self, agent: str, path: str = "/rpc") -> list[dict[str, Any]]:
        status, resp = self.rpc(path, "tools/list", token=self.token(agent))
        assert status == 200 and "result" in resp, resp
        return list(resp["result"]["tools"])

    # ---- the admin API
    def admin(self) -> httpx.Client:
        r = self.http.post(
            "/v1/auth/login",
            json={"email": self.env["PLATFORM_ADMIN_EMAIL"], "password": self.env["PLATFORM_ADMIN_PASSWORD"]},
        )
        r.raise_for_status()
        return httpx.Client(base_url=CF, timeout=60, headers={"Authorization": "Bearer " + r.json()["access_token"]})

    # ---- demo, from the inside
    def audit(self, service: str = "demo") -> list[dict[str, Any]]:
        """A connector's JSON audit lines (one per tools/call that reached it), oldest first."""
        out = sh("docker", "compose", "logs", "--no-log-prefix", service)
        return [json.loads(ln) for ln in out.splitlines() if ln.startswith('{"') and '"tool"' in ln]

    def ctl(self, method: str, path: str, body: dict[str, Any] | None = None, service: str = "demo") -> dict[str, Any]:
        """A connector's test controls on 127.0.0.1:8090 inside its container."""
        script = (
            "import json,sys,urllib.request as u;"
            f"r=u.Request('http://127.0.0.1:8090{path}',data=sys.stdin.read().encode() or None,method='{method}',"
            "headers={'content-type':'application/json'});print(u.urlopen(r,timeout=10).read().decode())"
        )
        return dict(json.loads(self.in_demo(script, json.dumps(body) if body else "", service)))

    def backend_calls(self) -> int:
        return sum(self.ctl("GET", "/_test/backend/calls").values())

    def readyz(self) -> int:
        script = (
            "import urllib.request as u,urllib.error as e\n"
            "try: print(u.urlopen('http://127.0.0.1:8080/readyz',timeout=5).status)\n"
            "except e.HTTPError as x: print(x.code)"
        )
        return int(self.in_demo(script))

    def in_demo(self, script: str, stdin: str = "", service: str = "demo") -> str:
        """Python inside a connector's container; one retry: a `docker exec` sometimes hangs in CI sandboxes."""
        for attempt in (1, 2):
            try:
                return sh(
                    "docker", "exec", "-i", self.container(service), "python", "-c", script, stdin=stdin, timeout=30
                )
            except subprocess.TimeoutExpired:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def wait_demo_ready(self, timeout_s: float = 90) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if "healthy" in sh("docker", "inspect", "-f", "{{.State.Health.Status}}", self.container("demo")):
                return
            time.sleep(1)
        raise AssertionError("demo did not become healthy")

    def container(self, service: str) -> str:
        return sh("docker", "compose", "ps", "-q", service).strip()

    def logs(self, service: str) -> str:
        return sh("docker", "compose", "logs", "--no-log-prefix", service)

    # ---- raw HTTP from inside a network of the stand
    def probe(self, network: str, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = sh(
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            network,
            PROBE_IMAGE,
            "python",
            "-I",
            "-c",
            PROBE,
            stdin=json.dumps(requests),
        )
        return list(json.loads(out))

    def sign(self, *, kid: str = KID_ADAPTER, key: Any = None, **overrides: Any) -> str:
        """A JWS like the adapter's, by default signed with the adapter key (what a captured one would hold)."""
        key = key or load_pem_private_key((SECRETS / "adapter_ed25519.pem").read_bytes(), password=None)
        now = int(time.time())
        claims = {
            "iss": core.ISSUER,
            "aud": "demo",
            "sub": ALPHA_READER,
            "tenant_id": "t-alpha",
            "team_id": self.state["teams"]["t-alpha"]["id"],
            "tool": "demo.boards.list",
            "iat": now,
            "exp": now + 60,
            "jti": secrets.token_hex(16),
            "request_id": "m5-direct",
            "ahs": core.args_hash({}),
        } | overrides
        return core.sign(claims, key, kid)

    def direct(
        self, network: str, calls: list[tuple[dict[str, str], dict[str, Any]]], url: str = DEMO_URL
    ) -> list[dict[str, Any]]:
        """tools/call straight to a connector from `network`: [(headers, params)] -> parsed replies or {error}."""
        reqs = [{"url": url, "headers": self.headers(None, h), "body": mcp_body("tools/call", p)} for h, p in calls]
        return [parse(r["body"]) if "body" in r else r for r in self.probe(network, reqs)]


@pytest.fixture(scope="session")
def stand() -> Iterator[Stand]:
    s = Stand(json.loads((SECRETS / "state.json").read_text()), env_values())
    assert s.http.get("/health").status_code == 200, "stand is not up: stand/run.sh"
    yield s
