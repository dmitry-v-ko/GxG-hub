"""Self-check of the ToolHive path C webhook (toolhive/webhook.py): HMAC, denials, JWS the M4 verifier accepts."""

import hashlib
import hmac
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE / "toolhive"), str(HERE.parent / "m4-contextforge/echo")]
import verify_jws  # noqa: E402
import webhook  # noqa: E402

KEY = Ed25519PrivateKey.generate()
webhook.KEY, webhook.SECRET = KEY, b"s3cret"
webhook.SERVERS = {"echo-alpha": {"tenant": "t-alpha", "aud": "gxg-connector:echo-t-alpha"}}
ARGS = {"text": "привет", "lang": "ru"}


def req(meta=None, server="echo-alpha", email="hermes@t-alpha.spike.example.com", tenant="t-alpha"):
    params = {"name": "echo", "arguments": ARGS} | ({"_meta": meta} if meta is not None else {})
    return {
        "version": "v0.1.0",
        "uid": "u1",
        "principal": {"sub": email, "email": email, "claims": {"tenant_id": tenant}},
        "mcp_request": {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
        "context": {"server_name": server},
    }


def test_signed_jws_lands_in_meta_and_verifies():
    out = webhook.decide(req(meta={"tenant_id": "t-beta", "gxg.dev/identity": "forged"}))
    assert out["allowed"] and out["patch"][0]["path"] == "/mcp_request/params/_meta/gxg.dev~1identity"
    got = verify_jws.verify(
        out["patch"][0]["value"], "gxg-connector:echo-t-alpha", {"name": "echo", "arguments": ARGS}, set(),
        {"spike-1": KEY.public_key()},
    )  # fmt: skip
    assert got["principal"] == "agent:t-alpha:hermes" and got["tool"] == "echo"
    assert webhook.decide(req())["patch"][0]["path"] == "/mcp_request/params/_meta"


def test_denials():
    for r, why in [
        (req(server="echo-x"), "server is not a registered connector"),
        (req(tenant="t-beta"), "caller tenant is not the tenant of this connector"),
        (req(email="admin@spike.example.com", tenant=None), "caller tenant is not the tenant of this connector"),
        (req(email="x@evil.example.com"), "caller is not an agent of this tenant"),
    ]:
        out = webhook.decide(r)
        assert (out["allowed"], out["reason"]) == (False, why)
    assert webhook.decide({"mcp_request": {"method": "tools/list"}})["allowed"]


def test_hmac():
    body, ts = b'{"a":1}', str(int(time.time()))
    sig = "sha256=" + hmac.new(b"s3cret", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert webhook.hmac_ok({"X-ToolHive-Timestamp": ts, "X-ToolHive-Signature": sig}, body)
    assert not webhook.hmac_ok({"X-ToolHive-Timestamp": ts, "X-ToolHive-Signature": sig}, b'{"a":2}')
    assert not webhook.hmac_ok({"X-ToolHive-Timestamp": str(int(ts) - 120), "X-ToolHive-Signature": sig}, body)
