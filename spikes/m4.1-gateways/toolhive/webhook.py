"""ADR-0002 path C on ToolHive without a fork: a mutating webhook of the thv/proxyrunner proxy puts the ADR-0001 JWS
into params._meta["gxg.dev/identity"] (the webhook may patch only /mcp_request/..., not headers).

Usage: python3 -I webhook.py <port> <adapter private key pem> <hmac secret file> <servers json>
servers json: {"<thv --name>": {"tenant": "t-alpha", "aud": "gxg-connector:echo-t-alpha"}}.
Same claims as the M4 plugin (spikes/m4-contextforge/plugin/gxg_identity/plugin.py), from ToolHive's principal
(the token_exchange token the proxy validated): sub = agent:<tenant>:<local part of email>, tool = params.name,
ahs = sha256(canonical arguments) — canonical() is the verifier's own. Denies (allowed: false -> HTTP 403, the call
never reaches the connector): bad or stale HMAC, no principal, tenant claim != tenant of this connector, email outside
the tenant's agent domain, unknown server. thv is configured with failure_policy: fail, so a dead webhook = 500.
shortcut: key from a file, one kid, servers map in a file; production: secret store, rotation, registry of connectors.
"""

import hashlib
import hmac
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

sys.path.insert(0, "/echo")  # M4 echo dir, mounted: reuse the verifier's canonical JSON
from verify_jws import canonical

KEY_NAME, KID, TTL, MAX_SKEW = "gxg.dev/identity", "spike-1", 60, 60
KEY, SECRET, SERVERS = None, b"", {}  # set in __main__ (tests set them directly)


def hmac_ok(headers, body: bytes) -> bool:
    """ToolHive signs "<unix ts>.<body>" with HMAC-SHA256 (TH/pkg/webhook/signing.go)."""
    ts, sig = headers.get("X-ToolHive-Timestamp", ""), headers.get("X-ToolHive-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > MAX_SKEW:
        return False
    want = "sha256=" + hmac.new(SECRET, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def decide(req: dict) -> dict:
    """Webhook response: allowed + JSON patch for tools/call, or a denial with the reason."""
    out = {"version": req.get("version"), "uid": req.get("uid"), "allowed": False}
    mcp = req.get("mcp_request") or {}
    if mcp.get("method") != "tools/call":
        return out | {"allowed": True}
    p, srv = req.get("principal") or {}, SERVERS.get((req.get("context") or {}).get("server_name"))
    tenant = (p.get("claims") or {}).get("tenant_id")
    local, _, domain = (p.get("email") or "").partition("@")
    if srv is None:
        return out | {"reason": "server is not a registered connector"}
    if not p.get("sub") or tenant != srv["tenant"]:
        return out | {"reason": "caller tenant is not the tenant of this connector"}
    if not local or domain != f"{tenant}.spike.example.com":
        return out | {"reason": "caller is not an agent of this tenant"}
    params, now = mcp.get("params") or {}, int(time.time())
    claims = {
        "iss": "gxg-identity-adapter",
        "aud": srv["aud"],
        "sub": f"agent:{tenant}:{local}",
        "tenant_id": tenant,
        "tool": params.get("name"),
        "iat": now,
        "exp": now + TTL,
        "jti": uuid.uuid4().hex,
        "request_id": req.get("uid"),
        "ahs": hashlib.sha256(canonical(params.get("arguments") or {})).hexdigest(),
    }
    token = jwt.encode(claims, KEY, algorithm="EdDSA", headers={"kid": KID})
    # "add" replaces a value the client already put under the same key
    if isinstance(params.get("_meta"), dict):
        op = {"op": "add", "path": "/mcp_request/params/_meta/" + KEY_NAME.replace("/", "~1"), "value": token}
    else:
        op = {"op": "add", "path": "/mcp_request/params/_meta", "value": {KEY_NAME: token}}
    return out | {"allowed": True, "patch_type": "json_patch", "patch": [op]}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not hmac_ok(self.headers, body):
            code, resp = 401, {"error": "bad signature"}  # thv: webhook error -> failure_policy fail -> 500
        else:
            code, resp = 200, decide(json.loads(body))
        print(json.dumps({"at": time.time(), "code": code, "allowed": resp.get("allowed"),
                          "reason": resp.get("reason")}), flush=True)  # fmt: skip
        data = json.dumps(resp).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    port, key, secret, servers = sys.argv[1:5]
    KEY = load_pem_private_key(Path(key).read_bytes(), password=None)
    SECRET, SERVERS = Path(secret).read_bytes(), json.loads(Path(servers).read_text())
    ThreadingHTTPServer(("127.0.0.1", int(port)), Handler).serve_forever()
