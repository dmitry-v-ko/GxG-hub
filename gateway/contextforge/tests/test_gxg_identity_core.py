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


NO_USER = "no authenticated user"
NO_NAME = "tool record has no upstream name"
NO_TEAM = "tool has no team or context tenant disagrees with the tool record"
NOT_SCOPED = "caller token must be scoped to exactly the tool's team"
NO_TENANT = "team is not a registered tenant"
NOT_AGENT = "caller is not an agent of this tenant"
NO_GATEWAY = "gateway is not a registered connector"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"email": None}, NO_USER),
        ({"email": ""}, NO_USER),
        ({"tool_name": None}, NO_NAME),
        ({"tool_team": None}, NO_TEAM),
        ({"context_tenant": TEAM_B}, NO_TEAM),
        ({"context_tenant": None}, NO_TEAM),
        ({"teams": []}, NOT_SCOPED),
        ({"teams": None}, NOT_SCOPED),
        ({"teams": [TEAM_A, TEAM_B]}, NOT_SCOPED),
        ({"teams": [TEAM_B]}, NOT_SCOPED),
        ({"tool_team": "team-x", "context_tenant": "team-x", "teams": ["team-x"]}, NO_TENANT),
        ({"email": "reader@t-beta.agents.example.com"}, NOT_AGENT),
        ({"email": "reader@evil.t-alpha.agents.example.com"}, NOT_AGENT),
        ({"email": "x@reader@t-alpha.agents.example.com"}, NOT_AGENT),  # the first @ splits: domain must match
        ({"email": "@t-alpha.agents.example.com"}, NOT_AGENT),
        ({"email": "reader"}, NOT_AGENT),
        ({"gateway_id": "other-gw"}, NO_GATEWAY),
        ({"gateway_id": None}, NO_GATEWAY),
        ({"gateway_id": ""}, NO_GATEWAY),
    ],
)
def test_fail_closed(change: dict[str, Any], why: str) -> None:
    with pytest.raises(core.Denied) as exc:
        core.claims_for(CFG, **ok_inputs(**change))
    assert str(exc.value) == why


def test_empty_gateway_id_is_never_a_connector() -> None:
    cfg = core.Config("k1", CFG.tenants, {"": "demo"})  # even a config that maps an empty id
    for gateway_id in (None, ""):
        with pytest.raises(core.Denied):
            core.claims_for(cfg, **ok_inputs(gateway_id=gateway_id))


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
