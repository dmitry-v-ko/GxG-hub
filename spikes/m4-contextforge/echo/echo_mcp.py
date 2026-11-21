"""Echo MCP server for the M4 spike (streamable HTTP, JSON responses).

Logs every request's headers and JSON-RPC body (including params._meta) as one JSON line
to stdout and to $ECHO_LOG. The `echo` tool returns what it received, so the caller can
see exactly what reached the "connector".
Variant C: with $ECHO_ADAPTER_PUBKEY set, every tools/call is verified on arrival (verify_jws.py, PyJWT
from the ContextForge image echo runs on); the verdict goes to the log line and to the tool result.
shortcut: a rejected call still gets the echo result; the M5 connector answers it with an error instead.
"""

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("ECHO_LOG", "/data/echo.jsonl")
PUBKEY = os.environ.get("ECHO_ADAPTER_PUBKEY")
SEEN_JTI: set = set()
TOOLS = [
    {
        "name": "echo",
        "description": "Returns the headers and _meta it received.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
]


def log(entry: dict) -> None:
    line = json.dumps(entry, sort_keys=True)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def verdict(token, path: str, params) -> dict:
    aud = "gxg-connector:echo-" + path.rsplit("/", 1)[-1]  # echo serves /mcp/<tenant>; one connector id per gateway
    try:
        return {"accepted": verify_jws.verify(token, aud, params if isinstance(params, dict) else {}, SEEN_JTI, KEYS)}
    except verify_jws.Rejected as e:
        return {"rejected": str(e)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: dict | None = None, extra: dict | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # no server-initiated stream
        if self.path == "/health":
            return self._send(200, {"ok": True})
        self._send(405)

    def do_DELETE(self) -> None:
        self._send(200)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            msg = json.loads(raw)
        except ValueError:
            return self._send(400, {"error": "bad json"})
        headers = {k: v for k, v in self.headers.items()}
        received_at = datetime.now(UTC).isoformat()
        entry = {"at": received_at, "peer": self.client_address[0], "path": self.path, "headers": headers, "body": msg}
        if PUBKEY and isinstance(msg, dict) and msg.get("method") == "tools/call":
            entry["verdict"] = verdict(self.headers.get("X-GxG-Identity"), self.path, msg.get("params"))
        log(entry)

        if isinstance(msg, list) or "id" not in msg:  # notification / batch: ack
            return self._send(202)
        method, mid, params = msg.get("method"), msg["id"], msg.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "gxg-echo", "version": "0.1.0"},
            }
            return self._send(
                200, {"jsonrpc": "2.0", "id": mid, "result": result}, {"Mcp-Session-Id": uuid.uuid4().hex}
            )
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        if method == "tools/call":
            seen = {
                "received_at": received_at,
                "headers": headers,
                "_meta": params.get("_meta"),
                "arguments": params.get("arguments"),
                "verdict": entry.get("verdict"),
            }
            result = {"content": [{"type": "text", "text": json.dumps(seen, sort_keys=True)}], "isError": False}
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": result})
        if method == "ping":
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {}})
        self._send(200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})

    def log_message(self, *args) -> None:  # request lines are already in the JSON log
        pass


if PUBKEY:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I drops the script dir
    import verify_jws
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    with open(PUBKEY, "rb") as f:
        KEYS = {os.environ.get("ECHO_ADAPTER_KID", "spike-1"): load_pem_public_key(f.read())}

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    ThreadingHTTPServer(
        ("0.0.0.0", port),  # noqa: S104 — container; host side is published on 127.0.0.1 only
        Handler,
    ).serve_forever()
