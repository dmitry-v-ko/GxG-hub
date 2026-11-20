"""Echo MCP server (streamable HTTP, JSON responses).

Logs every request's headers and JSON-RPC body (incl. params._meta) to $ECHO_LOG.
Authorization/Cookie values are redacted. Option A is verified offline by attacks.py.
With GXG_PUBKEY set, tools/call also runs the option C verifier (gxg_verify.py) online,
logs its verdict, and keeps raw X-GXG-Identity values only in $ECHO_RAW (gitignored).
"""

import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("ECHO_LOG", "/evidence/echo.jsonl")
RAW = os.environ.get("ECHO_RAW", "/evidence/raw/jws.jsonl")
REDACT = {"authorization", "cookie", "proxy-authorization"}
ID_HEADER = "x-gxg-identity"
VERIFIER = None
if os.environ.get("GXG_PUBKEY"):
    from gxg_verify import Verifier

    with open(os.environ["GXG_PUBKEY"], "rb") as f:
        VERIFIER = Verifier(f.read())
TOOLS = [
    {
        "name": "echo",
        "description": "Returns its arguments and the identity headers/_meta it received.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "additionalProperties": True},
    }
]


def log(entry, path=LOG):
    with open(path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def short(token):
    return f"<jws sha256={hashlib.sha256(token.encode()).hexdigest()[:16]}>"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj=None, extra=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(405)

    def do_DELETE(self):
        self._send(200)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        headers = {k.lower(): ("<redacted>" if k.lower() in REDACT else v) for k, v in self.headers.items()}
        id_values = self.headers.get_all(ID_HEADER) or []
        if id_values:
            headers[ID_HEADER] = [short(v) for v in id_values]
        try:
            msg = json.loads(raw or b"{}")
        except ValueError:
            msg = {"_unparsed": raw.decode(errors="replace")}
        entry = {"ts": time.time(), "peer": self.client_address[0], "path": self.path, "headers": headers, "body": msg}
        if VERIFIER and isinstance(msg, dict) and msg.get("method") == "tools/call":
            args = (msg.get("params") or {}).get("arguments")
            ok, res = VERIFIER.verify(id_values, args, audience=f"http://echo:9100{self.path}")
            entry["gxg_verdict"] = {"accepted": ok, "result": res}
            if id_values:
                log({"ts": entry["ts"], "path": self.path, "arguments": args, "values": id_values}, RAW)
        log(entry)
        if isinstance(msg, list) or "id" not in msg:  # notifications / batches
            return self._send(202)
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg["id"]
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "gxg-echo", "version": "0.1"},
            }
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result}, {"Mcp-Session-Id": "echo-session"})
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        if method == "tools/call" and VERIFIER and not entry["gxg_verdict"]["accepted"]:
            # what a real connector does: unauthenticated, no tool execution
            err = {"code": -32001, "message": "unauthenticated", "data": {"reason": entry["gxg_verdict"]["result"]}}
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "error": err})
        if method == "tools/call":
            seen = {
                "gxg_verdict": entry.get("gxg_verdict"),
                "x_forwarded_user": {k: v for k, v in headers.items() if k.startswith("x-forwarded-user")},
                "meta": params.get("_meta"),
                "arguments": params.get("arguments"),
            }
            text = json.dumps(seen, sort_keys=True)
            return self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False},
                },
            )
        if method == "ping":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {}})
        return self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), H).serve_forever()
