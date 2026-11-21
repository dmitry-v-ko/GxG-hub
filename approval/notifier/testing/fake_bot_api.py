"""Fake Telegram Bot API for tests and the compose test profile (never the real Telegram).

Contract of https://core.telegram.org/bots/api for what the notifier uses: `POST /bot<token>/<method>` with a
JSON body, answer `{"ok": true, "result": …}` or `{"ok": false, "error_code": …, "description": …}`; methods
`getMe`, `getUpdates` (offset, timeout long poll, allowed_updates), `sendMessage` (chat_id, text 1-4096,
reply_markup). A test injects updates (`push_text`, or `POST /_test/updates`) and reads what the bot sent
(`sent`, or `GET /_test/sent`). Standard library only, so it also runs in a bare python image.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_TEXT = 4096


class FakeBotApi:
    def __init__(self, token: str, max_wait_s: float | None = None) -> None:
        self.token = token
        self.max_wait_s = max_wait_s  # None: honour the client's long-poll timeout
        self.sent: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._updates: list[dict[str, Any]] = []
        self._next_update = 1
        self._next_message = 1
        self._cond = threading.Condition()

    def push(self, update: dict[str, Any]) -> int:
        with self._cond:
            update = {"update_id": self._next_update, **update}
            self._next_update += 1
            self._updates.append(update)
            self._cond.notify_all()
            return int(update["update_id"])

    def push_text(self, user_id: int, text: str, chat_type: str = "private") -> int:
        chat = {"id": user_id, "type": chat_type}
        sender = {"id": user_id, "is_bot": False, "first_name": "Test"}
        return self.push({"message": {"message_id": 1, "date": 0, "chat": chat, "from": sender, "text": text}})

    def push_callback(self, user_id: int, data: str) -> int:
        """A pressed inline button with callback_data: the bot never asked for callbacks (AS-20)."""
        sender = {"id": user_id, "is_bot": False, "first_name": "Test"}
        return self.push({"callback_query": {"id": "1", "from": sender, "data": data, "chat_instance": "1"}})

    def handle(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        prefix = f"/bot{self.token}/"
        if not path.startswith(prefix):
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        method = path[len(prefix) :]
        self.calls.append(method)
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "gxg", "username": "gxg_bot"}}
        if method == "getUpdates":
            return 200, {"ok": True, "result": self._get_updates(body)}
        if method == "sendMessage":
            text, chat = body.get("text"), body.get("chat_id")
            if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT or not isinstance(chat, int | str):
                return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message text is empty"}
            with self._cond:
                message = {"message_id": self._next_message, "date": 0, "chat": {"id": chat}, "text": text}
                self._next_message += 1
                self.sent.append(body)
            return 200, {"ok": True, "result": message}
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    def _get_updates(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        offset = int(body.get("offset") or 0)
        allowed = body.get("allowed_updates")
        wait = float(body.get("timeout") or 0)
        if self.max_wait_s is not None:
            wait = min(wait, self.max_wait_s)
        with self._cond:
            self._updates = [u for u in self._updates if u["update_id"] >= offset]  # offset confirms earlier ones

            def ready() -> list[dict[str, Any]]:
                return [u for u in self._updates if not allowed or any(k in u for k in allowed)]

            self._cond.wait_for(lambda: bool(ready()), timeout=wait)
            return ready()


def serve(api: FakeBotApi, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, doc: Any) -> None:
            data = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/_test/sent":
                self._reply(200, api.sent)
            else:
                self._reply(*api.handle(self.path, {}))

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/_test/updates":
                self._reply(200, {"update_id": api.push_text(int(body["user_id"]), str(body["text"]))})
            else:
                self._reply(*api.handle(self.path, body))

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":  # compose test profile: python fake_bot_api.py <token-file> <port>
    import sys
    from pathlib import Path

    serve(FakeBotApi(Path(sys.argv[1]).read_text().strip()), "0.0.0.0", int(sys.argv[2]))  # noqa: S104
    threading.Event().wait()
