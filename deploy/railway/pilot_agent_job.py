"""One-off jobs of the pilot agent (M13). Runs inside the Approval image as a transient Railway service; the script
arrives in the variable GXG_JOB_PY and `scripts/deploy/pilot_agent.py` reads the log. Never prints a secret value:
the agent token leaves sealed (RSA-OAEP wrapped Fernet key, the public key is GXG_JOB_SEAL_PUBKEY), ids and tool
names are not secrets.

GXG_JOB=setup  platform admin (PLATFORM_ADMIN_EMAIL / PLATFORM_ADMIN_PASSWORD, references to ContextForge's variables):
               team, agent user and its API token (SEC-02: random password nobody keeps), gateways for the Telegram
               connector and the Approval MCP, one virtual server each with only the allowed tools. Prints
               `IDS <json>` and `SEALED <token>`.
GXG_JOB=probe  as the agent (PILOT_AGENT_TOKEN, reference): `tools/list` and one `message.propose` through ContextForge;
               prints tool names and the error code of the propose, nothing else.
"""

import base64
import json
import os
import secrets
import socket
import sys
import time

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

CF = os.environ.get("GXG_CF_URL", "http://contextforge.railway.internal:4444")
TENANT = os.environ.get("GXG_TENANT", "t-pilot")
AGENT = os.environ.get("GXG_AGENT", "pilot")
EMAIL = f"{AGENT}@{TENANT}.agents.gxg.example.com"
GATEWAYS = {  # name -> upstream MCP url (Streamable HTTP)
    f"telegram-{TENANT}": os.environ.get("GXG_TELEGRAM_MCP", "http://telegram.railway.internal:8080/mcp"),
    f"approval-{TENANT}": os.environ.get("GXG_APPROVAL_MCP", "http://mcp.railway.internal:8091/mcp"),
}
ALLOWED = {  # the tools the pilot agent may see and call (originalName); everything else stays unlisted and denied
    "telegram.messages.search",
    "telegram.thread.read",
    "telegram.message.propose",
    "effects.status",
    "effects.cancel",
}


def items(resp, key):
    return resp if isinstance(resp, list) else resp.get(key, resp.get("items", []))


def seal(plain: str) -> str:
    public = serialization.load_pem_public_key(os.environ["GXG_JOB_SEAL_PUBKEY"].encode())
    data_key = Fernet.generate_key()
    wrapped = public.encrypt(
        data_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    return base64.b64encode(wrapped).decode() + "." + Fernet(data_key).encrypt(plain.encode()).decode()


def wait_healthy(deadline_s=300):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            if httpx.get(CF + "/health", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise SystemExit("ContextForge is not healthy")


class Api:
    def __init__(self, token):
        self.http = httpx.Client(base_url=CF, headers={"Authorization": f"Bearer {token}"}, timeout=60)

    def __call__(self, method, path, body=None, ok=(200, 201)):
        r = self.http.request(method, path, json=body)
        if r.status_code not in ok:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:300]}")  # never carries a request secret
        return r.json() if r.content else None


def setup():
    wait_healthy()
    login = httpx.post(
        CF + "/v1/auth/login",
        json={"email": os.environ["PLATFORM_ADMIN_EMAIL"], "password": os.environ["PLATFORM_ADMIN_PASSWORD"]},
        timeout=30,
    )
    login.raise_for_status()
    api = Api(login.json()["access_token"])
    teams = {t["slug"]: t for t in items(api("GET", "/v1/teams/?limit=100"), "teams")}
    team = teams.get(TENANT) or api("POST", "/v1/teams/", {"name": TENANT, "slug": TENANT, "visibility": "private"})
    users = {u["email"] for u in items(api("GET", "/v1/auth/email/admin/users?limit=100"), "users")}
    password = secrets.token_urlsafe(32)  # used once, not kept: nobody can log in as the agent (SEC-02)
    if EMAIL not in users:
        api(
            "POST",
            "/v1/auth/email/admin/users",
            {
                "email": EMAIL,
                "password": password,
                "full_name": f"agent:{TENANT}:{AGENT}",
                "password_change_required": False,
            },
        )
    else:
        api("PATCH", f"/v1/auth/email/admin/users/{EMAIL}", {"password": password})
    members = {
        m.get("user_email") or m.get("email") for m in items(api("GET", f"/v1/teams/{team['id']}/members"), "members")
    }
    if EMAIL not in members:
        api("POST", f"/v1/teams/{team['id']}/members", {"email": EMAIL, "role": "member"})
    token = api(
        "POST",
        "/v1/tokens",
        {
            "name": f"agent-{TENANT}-{AGENT}-{int(time.time())}",
            "team_id": team["id"],
            "user_email": EMAIL,
            "expires_in_days": 30,
        },
    )
    for url in GATEWAYS.values():  # which addresses the private names resolve to (SSRF allow-list of ContextForge)
        host = url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]
        try:
            found = sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
        except OSError as exc:
            found = [type(exc).__name__]
        print(f"resolve {host} -> {found}", flush=True)
    gateways = {g["name"]: g for g in items(api("GET", "/v1/gateways?include_inactive=true&limit=0"), "gateways")}
    for name, url in GATEWAYS.items():
        if name not in gateways:
            gateways[name] = api(
                "POST",
                "/v1/gateways",
                {"name": name, "url": url, "transport": "STREAMABLEHTTP", "teamId": team["id"], "visibility": "team"},
            )
    tools = items(api("GET", "/v1/tools?include_inactive=true&limit=0"), "tools")
    ids = {name: g["id"] for name, g in gateways.items()}
    listed = [
        {"original": t.get("originalName"), "name": t["name"], "gateway": t.get("gatewayId"), "id": t["id"]}
        for t in tools
        if t.get("gatewayId") in ids.values()
    ]
    servers = {s["name"]: s for s in items(api("GET", "/v1/servers?include_inactive=true&limit=0"), "servers")}
    out_servers = {}
    for gw_name, gw_id in ids.items():
        own = [t["id"] for t in listed if t["gateway"] == gw_id and t["original"] in ALLOWED]
        name = f"vs-{gw_name}"
        srv = servers.get(name) or api(
            "POST",
            "/v1/servers",
            {"server": {"name": name, "associated_tools": own}, "team_id": team["id"], "visibility": "team"},
        )
        out_servers[name] = srv["id"]
    for t in tools:  # EFF-02: execute is never registered
        if "execute" in str(t.get("originalName")) and t.get("gatewayId") in ids.values():
            api("DELETE", f"/v1/tools/{t['id']}", ok=(200, 204))
    print(
        "IDS " + json.dumps({"team": team["id"], "gateways": ids, "servers": out_servers, "tools": listed}), flush=True
    )
    print("SEALED " + seal(token.get("access_token") or token.get("token")), flush=True)


def revoke():
    """Keep the newest agent token (the one in the vault), revoke the older ones. Prints names and counts only."""
    wait_healthy()
    login = httpx.post(
        CF + "/v1/auth/login",
        json={"email": os.environ["PLATFORM_ADMIN_EMAIL"], "password": os.environ["PLATFORM_ADMIN_PASSWORD"]},
        timeout=30,
    )
    login.raise_for_status()
    api = Api(login.json()["access_token"])
    prefix = f"agent-{TENANT}-{AGENT}-"
    found = []
    for path in ("/v1/tokens/admin/all?limit=500", "/v1/tokens?limit=500&include_inactive=false"):
        r = api.http.get(path)
        print(f"GET {path.split('?')[0]} -> {r.status_code}", flush=True)
        if r.status_code == 200:
            found = [t for t in items(r.json(), "tokens") if str(t.get("name", "")).startswith(prefix)]
            if found:
                break
    found.sort(key=lambda t: t["name"])  # the suffix is the creation time
    print(f"agent tokens: {[t['name'] for t in found]}", flush=True)
    for t in found[:-1]:
        for path in (f"/v1/tokens/admin/{t['id']}", f"/v1/tokens/{t['id']}"):
            r = api.http.delete(path)
            print(f"revoke {t['name']} via {path.rsplit('/', 1)[0]} -> {r.status_code}", flush=True)
            if r.status_code in (200, 204):
                break
    print(f"kept {found[-1]['name'] if found else 'none'}", flush=True)


def rpc(client, url, method, params=None, rid=1):
    body = {
        "jsonrpc": "2.0",
        "method": method,
        **({"params": params} if params is not None else {}),
        **({"id": rid} if rid else {}),
    }
    r = client.post(url, json=body, headers={"Accept": "application/json, text/event-stream"})
    text = r.text
    if "data:" in text:  # SSE: the last data line is the answer
        text = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")][-1]
    return r.status_code, (json.loads(text) if text.strip().startswith("{") else {})


def probe():
    wait_healthy()
    server = os.environ["GXG_PROBE_SERVER"]  # id of the virtual server
    url = f"{CF}/servers/{server}/mcp"
    client = httpx.Client(headers={"Authorization": "Bearer " + os.environ["PILOT_AGENT_TOKEN"]}, timeout=60)
    status, _init = rpc(
        client,
        url,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pilot-agent-probe", "version": "1"},
        },
    )
    print(f"initialize http={status}", flush=True)
    rpc(client, url, "notifications/initialized", rid=0)
    status, listed = rpc(client, url, "tools/list", {}, 2)
    names = sorted(t["name"] for t in listed.get("result", {}).get("tools", []))
    print(f"tools/list http={status} count={len(names)}", flush=True)
    for n in names:
        print(f"tool {n}", flush=True)
    for tool in listed.get("result", {}).get("tools", []):
        if tool["name"].endswith("message-propose"):  # the argument schema: names and types only, no values
            print("schema " + json.dumps(tool.get("inputSchema", {}), separators=(",", ":"))[:1500], flush=True)
    for n in names:
        if n.endswith("message-propose"):
            status, res = rpc(
                client,
                url,
                "tools/call",
                {"name": n, "arguments": json.loads(os.environ.get("GXG_PROBE_ARGS", "{}"))},
                3,
            )
            text = json.dumps(res)
            code = next(
                (
                    c
                    for c in ("effect_disabled", "not_found", "forbidden", "invalid_request", "rate_limited")
                    if c in text
                ),
                "other",
            )
            print(f"propose http={status} error_code={code}", flush=True)
            if code == "other":  # the tool's own answer (an error text of the connector, no credentials in it)
                print("propose answer " + text.replace("\\n", " ")[:900], flush=True)


if __name__ == "__main__":
    {"setup": setup, "probe": probe, "revoke": revoke}[os.environ.get("GXG_JOB", "")]()
    sys.exit(0)
