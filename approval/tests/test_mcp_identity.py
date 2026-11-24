"""The production `mcp` process (variant C, SEC-03): ContextForge assertions bound to the call, no database."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp import Client

from gxg_approval import cli, mcp
from gxg_connector_kit import _jwt
from gxg_connector_kit.cf_identity import ISSUER, TYP, args_hash

INTENT = str(uuid.uuid4())
KID = "pilot-adapter-1"


class World:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.adapter = Ed25519PrivateKey.generate()
        self.svc = Ed25519PrivateKey.generate()
        self.requests: list[httpx.Request] = []
        (tmp / "svc").write_text(_jwt.b64url_encode(self.svc.private_bytes_raw()))
        pub = "ed25519:" + _jwt.b64url_encode(self.adapter.public_key().public_bytes_raw())
        for k, v in {
            "GXG_SERVICE_ID": "approval-mcp",
            "GXG_API_URL": "http://api.railway.internal:8090",
            "GXG_API_AUDIENCE": "approval-api",
            "GXG_SVC_KEY_FILE": str(tmp / "svc"),
            "GXG_IDENTITY_KEYS": f"{KID}={pub}",
        }.items():
            monkeypatch.setenv(k, v)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"intent_id": INTENT, "status": "pending_approval", "version": 3})

    def token(self, tool: str, args: dict[str, Any], *, signer: Ed25519PrivateKey | None = None) -> dict[str, str]:
        now = int(time.time())
        claims = {
            "iss": ISSUER, "aud": "approval-mcp", "sub": "agent:t-pilot:bot", "tenant_id": "t-pilot",
            "tool": tool, "ahs": args_hash(args), "iat": now, "exp": now + 60, "jti": uuid.uuid4().hex,
        }  # fmt: skip
        return {
            "X-GxG-Identity": jwt.encode(
                claims, signer or self.adapter, algorithm="EdDSA", headers={"typ": TYP, "kid": KID}
            )
        }

    def call(self, tool: str, headers: dict[str, str]) -> Any:
        server = mcp.from_env(transport=httpx.MockTransport(self.handler), headers=lambda: headers)

        async def run() -> Any:
            async with Client(server) as c:
                return await c.call_tool(tool, {"intent_id": INTENT}, raise_on_error=False)

        return asyncio.run(run())


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def test_signed_assertion_reaches_the_api_with_the_verified_scope(world: World) -> None:
    res = world.call("effects.status", world.token("effects.status", {"intent_id": INTENT}))
    assert not res.is_error and res.structured_content["version"] == 3
    (req,) = world.requests
    assert dict(req.url.params) == {"tenant_id": "t-pilot", "principal": "agent:t-pilot:bot"}  # from the assertion
    claims = jwt.decode(
        req.headers["authorization"].split()[1], world.svc.public_key(), algorithms=["EdDSA"], audience="approval-api"
    )
    assert claims["iss"] == "approval-mcp"  # the process's own service JWT


@pytest.mark.parametrize(
    "make",
    [
        lambda w: {},  # no assertion
        lambda w: w.token("effects.cancel", {"intent_id": INTENT}),  # bound to another tool
        lambda w: w.token("effects.status", {"intent_id": str(uuid.uuid4())}),  # bound to other arguments
        lambda w: w.token("effects.status", {"intent_id": INTENT}, signer=Ed25519PrivateKey.generate()),  # foreign key
    ],
    ids=["missing", "other-tool", "other-args", "unknown-signer"],
)
def test_calls_without_a_matching_assertion_never_reach_the_api(
    world: World, make: Callable[[World], dict[str, str]]
) -> None:
    res = world.call("effects.status", make(world))
    assert res.is_error
    assert world.requests == []


def test_cancel_is_bound_to_effects_cancel(world: World) -> None:
    res = world.call("effects.cancel", world.token("effects.cancel", {"intent_id": INTENT}))
    assert not res.is_error
    assert [r.method for r in world.requests] == ["GET", "POST"]


def test_mcp_process_starts(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[object, str]] = []
    monkeypatch.setattr(cli, "_serve", lambda app, listen: served.append((app, listen)))
    cli.main(["mcp"])
    assert served and served[0][1] == "0.0.0.0:8091"


def test_mcp_refuses_to_start_without_identity_keys(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GXG_IDENTITY_KEYS")
    with pytest.raises(SystemExit, match="GXG_IDENTITY_KEYS"):
        mcp.from_env()
