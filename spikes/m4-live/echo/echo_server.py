"""Echo MCP server (streamable HTTP, JSON responses, stdlib only).

Logs every request's headers and JSON-RPC body (incl. params._meta) to
/evidence/echo.jsonl. Authorization/Cookie values are redacted. It trusts
nothing and verifies nothing: verification is done offline by verify.py.
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("ECHO_LOG", "/evidence/echo.jsonl")
REDACT = {"authorization", "cookie", "proxy-authorization"}
TOOLS = [
    {
        "name": "echo",
        "description": "Returns its arguments and the identity headers/_meta it received.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "additionalProperties": True},
    }
]


def log(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


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
        try:
            msg = json.loads(raw or b"{}")
        except ValueError:
            msg = {"_unparsed": raw.decode(errors="replace")}
        log({"ts": time.time(), "peer": self.client_address[0], "path": self.path, "headers": headers, "body": msg})
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
        if method == "tools/call":
            seen = {
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
