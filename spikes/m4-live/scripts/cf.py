"""Tiny ContextForge REST/MCP client shared by setup.py and attacks.py (stdlib only)."""

import json
import os
import pathlib
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.environ.get("CF_URL", "http://127.0.0.1:4444")
STATE = ROOT / "secrets" / "state.json"  # gitignored (secrets/)


def env():
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def http(method, url, body=None, token=None, headers=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url if url.startswith("http") else BASE + url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw, code, rh = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, code, rh = e.read(), e.code, dict(e.headers)
    text = raw.decode(errors="replace")
    if text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = datas[-1] if datas else text
    try:
        return code, json.loads(text), rh
    except ValueError:
        return code, text, rh


def login(email, password):
    code, d, _ = http("POST", "/auth/email/login", {"email": email, "password": password})
    if code != 200:
        raise RuntimeError(f"login {email}: {code} {d}")
    return d["access_token"]


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(s):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))
    os.chmod(STATE, 0o600)
