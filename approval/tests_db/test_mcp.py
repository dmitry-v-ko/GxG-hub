"""`effects.status` / `effects.cancel` through an MCP client (FastMCP, in memory) → api → PostgreSQL. AC-M7-09."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastmcp import Client
from psycopg_pool import ConnectionPool

from gxg_approval.api import create_app
from gxg_approval.mcp import create_server
from gxg_connector_kit.identity import StubIssuer, StubVerifier

BOT, OTHER, BETA_BOT = "agent:t-alpha:bot", "agent:t-alpha:other", "agent:t-beta:bot"


class Agent:
    """Who the trusted channel says is calling (SEC-03)."""

    def __init__(self, issuer: StubIssuer) -> None:
        self.issuer = issuer
        self.headers: dict[str, str] = {}

    def be(self, tenant: str, principal: str) -> None:
        self.headers = self.issuer.headers(tenant_id=tenant, principal=principal, audience="approval-mcp")


@pytest.fixture
def mcp(world: Any, api_pool: ConnectionPool[Any], monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Agent]:
    monkeypatch.setenv("GXG_PROFILE", "test")
    issuer = StubIssuer()
    agent = Agent(issuer)
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(world.deps(api_pool))), base_url="http://api")
    server = create_server(
        api, lambda: world.token("approval-mcp"), StubVerifier("approval-mcp", issuer.public_key), lambda: agent.headers
    )
    return server, agent


def call(server: Any, tool: str, intent_id: str) -> Any:
    async def run() -> Any:
        async with Client(server) as c:
            return await c.call_tool(tool, {"intent_id": intent_id}, raise_on_error=False)

    return asyncio.run(run())


def test_tools_are_listed(mcp: tuple[Any, Agent]) -> None:
    async def run() -> Any:
        async with Client(mcp[0]) as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == {"effects.status", "effects.cancel"}
    assert tools["effects.status"].inputSchema["required"] == ["intent_id"]
    assert set(tools["effects.cancel"].inputSchema["properties"]) == {"intent_id"}  # no identity in args


def test_status_of_own_intent(mcp: tuple[Any, Agent], propose: Callable[..., Any]) -> None:
    server, agent = mcp
    created = propose().json()
    agent.be("t-alpha", BOT)
    res = call(server, "effects.status", created["intent_id"])
    assert not res.is_error and res.structured_content == created


def test_foreign_intent_is_not_found_byte_for_byte(mcp: tuple[Any, Agent], propose: Callable[..., Any]) -> None:
    """AC-M7-09: another requester or another tenant → `not_found`, identical to a missing intent."""
    server, agent = mcp
    mine = propose().json()["intent_id"]
    beta = propose(tenant="t-beta", requester=BETA_BOT, connection="demo-b", resource="demo:demo-b/board/b2")
    results = []
    for who, intent in (
        (("t-alpha", OTHER), mine),  # other requester, same tenant
        (("t-beta", BETA_BOT), mine),  # other tenant
        (("t-alpha", BOT), beta.json()["intent_id"]),  # own principal, other tenant's intent
        (("t-alpha", BOT), str(uuid.uuid4())),  # missing
    ):
        for tool in ("effects.status", "effects.cancel"):
            agent.be(*who)
            res = call(server, tool, intent)
            assert res.is_error
            results.append((res.content[0].text, repr(res.structured_content)))
    assert len(set(results)) == 1
    assert results[0][0] == '{"code":"not_found","message":"intent not found","retryable":false}'
    # The same principal ID in two tenants cannot come through here: the identity is `agent:<tenant>:<name>`
    # (TEN-06, IdentityVerifier); the api guards it for any approval_mcp (test_api_read.py, same_principal).


def test_intent_id_is_a_uuid_not_a_path(mcp: tuple[Any, Agent], propose: Callable[..., Any]) -> None:
    """AS-11a: a crafted intent_id does not reach other api endpoints; it is `not_found` like a missing intent."""
    server, agent = mcp
    intent = propose().json()["intent_id"]
    texts = set()
    for tool in ("effects.status", "effects.cancel"):
        for crafted in ("../../readyz", "../../.well-known/gxg-exec-jwks.json", "../../healthz", f"{intent}/../x", ""):
            agent.be("t-alpha", BOT)
            res = call(server, tool, crafted)
            assert res.is_error, crafted
            texts.add(res.content[0].text)
    assert texts == {'{"code":"not_found","message":"intent not found","retryable":false}'}
    agent.be("t-alpha", BOT)
    upper = call(server, "effects.cancel", intent.upper())  # any UUID spelling reaches the same intent
    assert not upper.is_error and upper.structured_content["status"] == "cancelled"


def test_cancel_own_intent(mcp: tuple[Any, Agent], propose: Callable[..., Any]) -> None:
    server, agent = mcp
    intent = propose().json()["intent_id"]
    agent.be("t-alpha", BOT)
    res = call(server, "effects.cancel", intent)
    assert not res.is_error and res.structured_content["status"] == "cancelled"
    replay = call(server, "effects.cancel", intent)  # the same identity assertion again (SEC-03 jti)
    assert replay.is_error and replay.structured_content["code"] == "unauthenticated"
    agent.be("t-alpha", BOT)
    again = call(server, "effects.cancel", intent)
    assert again.is_error and again.structured_content["code"] == "conflict"


def test_no_identity_no_answer(mcp: tuple[Any, Agent], propose: Callable[..., Any]) -> None:
    server, agent = mcp
    intent = propose().json()["intent_id"]
    agent.headers = {}
    res = call(server, "effects.status", intent)
    assert res.is_error and res.structured_content["code"] == "unauthenticated"
    agent.headers = {"x-gxg-test-identity": "forged.token.value"}
    assert call(server, "effects.status", intent).structured_content["code"] == "unauthenticated"


def test_api_down_is_temporarily_unavailable(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GXG_PROFILE", "test")
    issuer = StubIssuer()
    agent = Agent(issuer)
    agent.be("t-alpha", BOT)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    api = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://api")
    server = create_server(api, lambda: "t", StubVerifier("approval-mcp", issuer.public_key), lambda: agent.headers)
    res = call(server, "effects.status", str(uuid.uuid4()))
    assert res.is_error and res.structured_content["code"] == "temporarily_unavailable"
