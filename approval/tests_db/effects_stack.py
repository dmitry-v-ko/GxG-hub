"""The M8 stack for tests: Approval Service `api`, connector `demo` (MCP + internal + test controls) and executors,
over HTTP on Unix sockets (no TCP in tests, AC-M0-01), on the real PostgreSQL of tests_db.

One signed bundle for both sides: the demo fixtures' grants (connectors/demo/fixtures/grants), an approval policy
that auto-approves the actors, and a platform registry with generated keys and the demo manifest's tools (GR-11a).
`demo` is built by `demo_connector.server.build` from environment-style settings (the production path); only the
transports to `api` are Unix sockets. `python effects_stack.py <config.json>` serves the same `demo` in a separate
process, so a test can kill it (AC-M8-07).
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import httpx2
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from demo_connector import server as demo_server
from demo_connector import testctl
from demo_connector.backend import Backend
from gxg_connector_kit import _jwt, canonical, exec_token
from gxg_connector_kit.identity import StubIssuer
from gxg_policy.bundle import TOOL_KEYS

REPO = next(p for p in Path(__file__).resolve().parents if (p / "uv.lock").exists())
DEMO = REPO / "connectors" / "demo"
KID = "ci-test"
API_ID, MCP_ID, EXECUTOR_ID, CONNECTOR = "approval-api", "approval-mcp", "executor", "demo"
ACTOR_A, ACTOR_B, READER_A = "agent:t-alpha:actor", "agent:t-beta:actor", "agent:t-alpha:reader"
BOARD_A, BOARD_B = "demo:demo-main/board/a-public", "demo:demo-main/board/b-public"
TENANTS = ("t-alpha", "t-beta")
MCP_URL = "http://demo/mcp"


def b64(raw: bytes) -> str:
    return _jwt.b64url_encode(raw)


def pub(key: Ed25519PrivateKey) -> str:
    return "ed25519:" + b64(_jwt.raw_public_key(key.public_key()))


def seed(key: Ed25519PrivateKey) -> str:
    return b64(key.private_bytes_raw())


def auto_policy(tenant: str, actor: str, **rule: Any) -> dict[str, Any]:
    return {
        "tenant": tenant,
        "schema_version": 1,
        "quotas": {"propose_per_requester_per_hour": 1000, "pending_per_requester_max": 1000},
        "rules": [
            {
                "id": "demo-auto",
                "match": {"tools": ["demo.message.propose"], "requesters": [actor]},
                "decision": "auto_approve",
                "limits": {"max_per_hour": 1000, "max_per_day": 10000},
                **rule,
            }
        ],
    }


@dataclass
class Keys:
    """Every key of one test, generated here; none is stored outside the test's tmp directory."""

    ci: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    services: dict[str, Ed25519PrivateKey] = field(
        default_factory=lambda: {s: Ed25519PrivateKey.generate() for s in (API_ID, MCP_ID, EXECUTOR_ID, CONNECTOR)}
    )
    executor: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)  # execution tokens
    payload: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)  # tenant → purpose → (sk, pk)
    issuer: StubIssuer = field(default_factory=StubIssuer)  # agent identity (test profile)

    def __post_init__(self) -> None:
        from gxg_approval import crypto

        self.payload = {t: {p: crypto.generate() for p in ("args", "preview")} for t in TENANTS}


def bundle(
    keys: Keys, *, built_at: float, grants: dict[str, Any] | None = None, policies: Any = None, timeout: int = 0
) -> bytes:
    """GR-11a bundle (canonical JSON) of the demo fixtures; `grants` replaces a tenant's grants file; `timeout`
    replaces the tools' dispatch_timeout_s."""
    manifest = yaml.safe_load((DEMO / "gxg-manifest.yaml").read_text(encoding="utf-8"))
    tools = [{k: t[k] for k in TOOL_KEYS if k in t} for t in manifest["tools"]]
    for t in tools:
        if timeout and "dispatch_timeout_s" in t:
            t["dispatch_timeout_s"] = timeout
    files = {t: yaml.safe_load((DEMO / "fixtures" / "grants" / f"{t}.yaml").read_text("utf-8")) for t in TENANTS}
    files.update(grants or {})
    policies = policies or {"t-alpha": auto_policy("t-alpha", ACTOR_A), "t-beta": auto_policy("t-beta", ACTOR_B)}
    services: list[dict[str, Any]] = [
        {"id": API_ID, "kind": "approval_api", "tenants": ["*"]},
        {"id": MCP_ID, "kind": "approval_mcp", "tenants": ["*"]},
        {"id": EXECUTOR_ID, "kind": "executor", "tenants": ["*"]},
        {"id": CONNECTOR, "kind": "connector", "tenants": list(TENANTS), "internal_url": "https://demo.internal:8081",
         "tools": tools},
    ]  # fmt: skip
    for s in services:
        s["public_keys"] = [pub(keys.services[s["id"]])]
    doc = {
        "schema_version": 1,
        "bundle_version": "e" * 40,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(built_at)),
        "tenants": {t: {"grants": files[t], "approval_policy": policies.get(t)} for t in TENANTS},
        "platform": {"schema_version": 1, "services": services},
    }
    return canonical.dumps(doc)


def write_bundle(keys: Keys, directory: Path, data: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    sig = json.dumps({"alg": "EdDSA", "kid": KID, "sig": b64(keys.ci.sign(data))}).encode()
    (directory / "grants-bundle.json").write_bytes(data)
    (directory / "grants-bundle.json.sig").write_bytes(sig)


def jwks(*keys: Ed25519PrivateKey) -> dict[str, Any]:
    return {"keys": [exec_token.public_jwk(k.public_key()) for k in keys]}


# --- demo ----------------------------------------------------------------------------------------


@dataclass
class DemoConfig:
    """Everything a `demo` instance needs (JSON for the subprocess variant)."""

    dir: str  # sockets, bundle, state, provider journal
    bundle_key: str  # DEMO_BUNDLE_KEYS value
    issuer_key: str  # DEMO_TEST_ISSUER_KEY value
    approval_sock: str

    @property
    def path(self) -> Path:
        return Path(self.dir)

    def sock(self, name: str) -> str:
        return str(self.path / f"demo-{name}.sock")


def demo_apps(cfg: DemoConfig, clock: Callable[[], float] = time.time) -> tuple[Any, Any, Any, Any]:
    """(mcp app, internal app, test-controls app, connector), built as the image does from its settings."""
    settings = demo_server.Settings.from_env(
        {
            "GXG_PROFILE": "test",
            "DEMO_BUNDLE_DIR": str(cfg.path / "bundle"),
            "DEMO_BUNDLE_KEYS": cfg.bundle_key,
            "DEMO_STATE_FILE": str(cfg.path / "demo-state" / "bundle-state.json"),
            "DEMO_TEST_ISSUER_KEY": cfg.issuer_key,
            "DEMO_TEST_CONTROLS": "1",
            "DEMO_APPROVAL_URL": "http://approval",
            "DEMO_SVC_KEY_FILE": str(cfg.path / "demo-svc.seed"),
            "DEMO_PROVIDER_DB": str(cfg.path / "provider.sqlite3"),
            "DEMO_PROVIDER_TOKEN": "demo-secret-test",
        }
    )
    wiring = demo_server.Wiring(
        approval=httpx2.AsyncHTTPTransport(uds=cfg.approval_sock),
        jwks=httpx2.HTTPTransport(uds=cfg.approval_sock),
        clock=clock,
    )
    app, connector = demo_server.build(settings, Backend(scan_delay_s=0), wiring)
    assert connector.effects is not None
    return (
        app,
        connector.effects.internal_app(),
        testctl.create_app(connector.backend, connector.effects.provider),
        connector,
    )


class Servers:
    """uvicorn servers on Unix sockets in one event loop, in a background thread."""

    def __init__(self, apps: list[tuple[Any, str]]) -> None:
        self.servers = [uvicorn.Server(uvicorn.Config(a, uds=s, log_level="warning", lifespan="on")) for a, s in apps]
        self.thread = threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True)
        self.thread.start()
        deadline = time.time() + 20
        while not all(s.started for s in self.servers):
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("servers did not start")
            time.sleep(0.02)

    async def _serve(self) -> None:
        await asyncio.gather(*(s.serve() for s in self.servers))

    def stop(self) -> None:
        for s in self.servers:
            s.should_exit = True
        self.thread.join(timeout=20)


def serve_demo(cfg: DemoConfig, clock: Callable[[], float] = time.time) -> tuple[Servers, Any]:
    for name in ("mcp", "internal", "ctl"):
        Path(cfg.sock(name)).unlink(missing_ok=True)
    mcp, internal, ctl, connector = demo_apps(cfg, clock)
    return Servers([(mcp, cfg.sock("mcp")), (internal, cfg.sock("internal")), (ctl, cfg.sock("ctl"))]), connector


class DemoClient:
    """The executor's view (internal port), the agent's view (MCP) and the test controls of one `demo`."""

    def __init__(self, cfg: DemoConfig, keys: Keys) -> None:
        self.cfg, self.keys = cfg, keys

    def internal(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=self.cfg.sock("internal")), base_url="http://demo.internal"
        )

    def ctl(self, method: str, path: str, **kw: Any) -> Any:
        with httpx.Client(transport=httpx.HTTPTransport(uds=self.cfg.sock("ctl")), base_url="http://ctl") as c:
            r = c.request(method, path, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def mode(self, mode: str, count: int | None = None) -> None:
        self.ctl("PUT", "/_test/provider/mode", json={"mode": mode, "count": count})

    def calls(self, key: str | None = None) -> list[dict[str, Any]]:
        params = {"idempotency_key": key} if key else {}
        calls: list[dict[str, Any]] = self.ctl("GET", "/_test/provider/calls", params=params)["calls"]
        return calls

    def propose(self, principal: str, args: dict[str, Any]) -> dict[str, Any]:
        """`demo.message.propose` through the official MCP SDK client, as the agent (test issuer identity)."""

        async def run() -> dict[str, Any]:
            headers = self.keys.issuer.headers(tenant_id=principal.split(":")[1], principal=principal, audience="demo")
            transport = httpx2.AsyncHTTPTransport(uds=self.cfg.sock("mcp"))
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(
                    httpx2.AsyncClient(transport=transport, base_url="http://demo", headers=headers)
                )
                read, write = await stack.enter_async_context(
                    streamable_http_client("http://demo/mcp", http_client=http)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                result = await session.call_tool("demo.message.propose", args)
                out: dict[str, Any] = result.model_dump(by_alias=True, exclude_none=True, mode="json")
                return out

        return asyncio.run(run())

    def tools(self) -> list[str]:
        async def run() -> list[str]:
            transport = httpx2.AsyncHTTPTransport(uds=self.cfg.sock("mcp"))
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(httpx2.AsyncClient(transport=transport, base_url="http://demo"))
                read, write = await stack.enter_async_context(
                    streamable_http_client("http://demo/mcp", http_client=http)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                return sorted(t.name for t in (await session.list_tools()).tools)

        return asyncio.run(run())


@contextmanager
def demo_process(cfg: DemoConfig) -> Iterator[Any]:
    """`demo` in its own process (killable). Yields the Popen."""
    import subprocess

    for name in ("mcp", "internal", "ctl"):
        Path(cfg.sock(name)).unlink(missing_ok=True)
    conf = cfg.path / f"demo-{uuid.uuid4().hex}.json"
    conf.write_text(json.dumps(cfg.__dict__))
    proc = subprocess.Popen([sys.executable, __file__, str(conf)])
    try:
        deadline = time.time() + 30
        while not all(Path(cfg.sock(n)).exists() for n in ("mcp", "internal", "ctl")):
            if time.time() > deadline or proc.poll() is not None:
                raise RuntimeError("demo process did not start")
            time.sleep(0.05)
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


if __name__ == "__main__":  # the subprocess of demo_process()
    import os

    os.environ["GXG_PROFILE"] = "test"
    config = DemoConfig(**json.loads(Path(sys.argv[1]).read_text()))
    servers, _ = serve_demo(config)
    servers.thread.join()
