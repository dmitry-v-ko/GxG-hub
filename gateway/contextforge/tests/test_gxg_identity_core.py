"""Identity adapter decision and claims (gateway/contextforge/gxg_identity/core.py), and its contract with the kit."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # gateway/contextforge: the plugin package
from gxg_identity import core

from gxg_connector_kit import TokenError
from gxg_connector_kit.cf_identity import ContextForgeVerifier

TEAM_A, TEAM_B, GW = "team-a-uuid", "team-b-uuid", "gw-uuid"
CFG = core.Config.parse(
    {
        "kid": "k1",
        "tenants": {
            TEAM_A: {"tenant": "t-alpha", "agent_domain": "t-alpha.agents.example.com"},
            TEAM_B: {"tenant": "t-beta", "agent_domain": "t-beta.agents.example.com"},
        },
        "connectors": {GW: "demo"},
    }
)
ARGS = {"query": "привет", "limit": 3}
T0 = 1_800_000_000


def ok_inputs(**kw: Any) -> dict[str, Any]:
    return {
        "email": "reader@t-alpha.agents.example.com",
        "teams": [TEAM_A],
        "tool_team": TEAM_A,
        "tool_name": "demo.notes.search",
        "context_tenant": TEAM_A,
        "gateway_id": GW,
        "arguments": ARGS,
        "request_id": "req-1",
        "now": lambda: T0,
    } | kw


def test_claims_of_an_agent() -> None:
    c = core.claims_for(CFG, **ok_inputs())
    jti = c.pop("jti")
    assert len(jti) == 32 and int(jti, 16) >= 0
    assert c == {
        "iss": "gxg-identity-adapter",
        "aud": "demo",
        "sub": "agent:t-alpha:reader",
        "tenant_id": "t-alpha",
        "team_id": TEAM_A,
        "tool": "demo.notes.search",
        "iat": T0,
        "exp": T0 + 60,
        "request_id": "req-1",
        "ahs": core.args_hash(ARGS),
    }
    assert core.claims_for(CFG, **ok_inputs())["jti"] != jti  # new for every call


def test_no_arguments_hash_as_empty_object() -> None:
    assert core.claims_for(CFG, **ok_inputs(arguments=None))["ahs"] == core.args_hash({})


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"email": None}, "no authenticated user"),
        ({"email": ""}, "no authenticated user"),
        ({"tool_name": None}, "tool record has no upstream name"),
        ({"tool_team": None}, "tool has no team"),
        ({"context_tenant": TEAM_B}, "context tenant disagrees"),
        ({"context_tenant": None}, "context tenant disagrees"),
        ({"teams": []}, "exactly the tool's team"),
        ({"teams": None}, "exactly the tool's team"),
        ({"teams": [TEAM_A, TEAM_B]}, "exactly the tool's team"),
        ({"teams": [TEAM_B]}, "exactly the tool's team"),
        ({"tool_team": "team-x", "context_tenant": "team-x", "teams": ["team-x"]}, "not a registered tenant"),
        ({"email": "reader@t-beta.agents.example.com"}, "not an agent of this tenant"),
        ({"email": "reader@evil.t-alpha.agents.example.com"}, "not an agent of this tenant"),
        ({"email": "@t-alpha.agents.example.com"}, "not an agent of this tenant"),
        ({"email": "reader"}, "not an agent of this tenant"),
        ({"gateway_id": "other-gw"}, "gateway is not a registered connector"),
        ({"gateway_id": None}, "gateway is not a registered connector"),
    ],
)
def test_fail_closed(change: dict[str, Any], why: str) -> None:
    with pytest.raises(core.Denied, match=why):
        core.claims_for(CFG, **ok_inputs(**change))


def test_incoming_identity_header_is_replaced() -> None:
    out = core.with_identity({"x-gxg-identity": "forged", "X-GXG-IDENTITY": "forged2", "Accept": "a"}, "real")
    assert out == {"Accept": "a", "X-GxG-Identity": "real"}


def test_config_parse_types() -> None:
    cfg = core.Config.parse({"kid": 7, "tenants": {1: {"tenant": "t", "agent_domain": "d"}}, "connectors": {2: 3}})
    assert cfg == core.Config("7", {"1": core.Tenant("t", "d")}, {"2": "3"})


def test_adapter_and_kit_verifier_agree() -> None:
    """The contract of variant C: what the adapter signs, the connector accepts, and nothing else."""
    key = Ed25519PrivateKey.generate()
    verifier = ContextForgeVerifier("demo", {"k1": key.public_key()}, now=lambda: T0)
    token = core.sign(core.claims_for(CFG, **ok_inputs()), key, "k1")
    headers = core.with_identity({}, token)
    ident = verifier.verify(headers, tool="demo.notes.search", arguments={"limit": 3, "query": "привет"})
    assert (ident.tenant_id, ident.principal, ident.request_id) == ("t-alpha", "agent:t-alpha:reader", "req-1")
    with pytest.raises(TokenError, match="token rejected"):
        verifier.verify(headers, tool="demo.notes.search", arguments=ARGS)  # the same assertion again: replay
