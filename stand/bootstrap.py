"""Configure the running dev stand through the ContextForge REST API. Idempotent: rerun after any `up`.

`uv run python stand/bootstrap.py` after `docker compose up -d`:
  teams t-alpha, t-beta (private); agents reader and nobody in each (<name>@<tenant>.agents.gxg.example.com,
  role member, one API token bound to its team: SEC-02); gateway `demo-<tenant>` -> http://demo:8080/mcp in each
  team (visibility team); a virtual server per team; an alias on t-alpha's demo.boards.list; a REST tool
  `demo.message.execute` in t-alpha that unified_pdp must refuse to every agent (EFF-02). M6: the same for the KB
  wrapper: gateway `kb-<tenant>` -> http://kb:8080/mcp and virtual server `vs-kb-<tenant>`. Then the identity adapter
  gets the team and gateway ids (stand/secrets/plugins.yaml) and ContextForge restarts if they changed.
State for the tests (ids, agent tokens) -> stand/secrets/state.json (git-ignored).
shortcut: REST calls instead of config as code; ContextForge v1.1.1 has no import for users and tokens.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx
import jwt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init import ENV, REPO, SECRETS, env_values, password, plugins_yaml

CF = "http://127.0.0.1:4444"
STATE = SECRETS / "state.json"
TENANTS = ("t-alpha", "t-beta")
AGENTS = ("reader", "nobody")
ALIAS = "alpha_boards"
EXECUTE = "demo.message.execute"
CONNECTORS = {"demo": "http://demo:8080/mcp", "kb": "http://kb:8080/mcp"}  # gateway `<id>-<tenant>` in each team


def email(tenant: str, name: str) -> str:
    return f"{name}@{tenant}.agents.gxg.example.com"


class Api:
    def __init__(self, token: str) -> None:
        self.http = httpx.Client(base_url=CF, headers={"Authorization": f"Bearer {token}"}, timeout=60)

    def __call__(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201)) -> Any:
        r = self.http.request(method, path, json=body)
        if r.status_code not in ok:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.content else None


def items(resp: Any, key: str) -> list[dict[str, Any]]:
    return resp if isinstance(resp, list) else resp.get(key, resp.get("items", []))


def token_expires(token: str) -> float:
    """`exp` of a ContextForge API token (read only to decide on reissue; ContextForge verifies it)."""
    return float(jwt.decode(token, options={"verify_signature": False})["exp"])


def wait_healthy(timeout_s: float = 240) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(CF + "/health", timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise SystemExit("ContextForge is not healthy")


def login(env: dict[str, str]) -> str:
    r = httpx.post(
        CF + "/v1/auth/login",
        json={"email": env["PLATFORM_ADMIN_EMAIL"], "password": env["PLATFORM_ADMIN_PASSWORD"]},
        timeout=30,
    )
    r.raise_for_status()
    return str(r.json()["access_token"])


def virtual_servers(api: Api, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Each team's tools by upstream name, and a virtual server per connector gateway; returns all tools."""
    tools = items(api("GET", "/v1/tools?include_inactive=true&limit=0"), "tools")
    servers = {s["name"]: s for s in items(api("GET", "/v1/servers?include_inactive=true&limit=0"), "servers")}
    for tenant, t in state["teams"].items():
        t["tools"] = {}
        for connector, gateway_id in t["gateways"].items():
            own = [x for x in tools if x.get("gatewayId") == gateway_id]
            t["tools"] |= {x["originalName"]: {"id": x["id"], "name": x["name"]} for x in own}
            name = f"vs-{tenant}" if connector == "demo" else f"vs-{connector}-{tenant}"
            srv = servers.get(name) or api(
                "POST",
                "/v1/servers",
                {
                    "server": {"name": name, "associated_tools": [x["id"] for x in own]},
                    "team_id": t["id"],
                    "visibility": "team",
                },
            )
            t["server_id" if connector == "demo" else f"{connector}_server_id"] = srv["id"]
    return tools


def main() -> None:
    env = env_values()
    wait_healthy()
    api = Api(login(env))
    state: dict[str, Any] = json.loads(STATE.read_text()) if STATE.exists() else {"teams": {}, "agents": {}}
    teams = {t["slug"]: t for t in items(api("GET", "/v1/teams/?limit=100"), "teams")}
    users = {u["email"] for u in items(api("GET", "/v1/auth/email/admin/users?limit=100"), "users")}
    gateways = {g["name"]: g for g in items(api("GET", "/v1/gateways?include_inactive=true&limit=0"), "gateways")}
    for tenant in TENANTS:
        team = teams.get(tenant) or api("POST", "/v1/teams/", {"name": tenant, "slug": tenant, "visibility": "private"})
        t = state["teams"].setdefault(tenant, {})
        t["id"] = team["id"]
        members = {
            m.get("user_email") or m.get("email")
            for m in items(api("GET", f"/v1/teams/{team['id']}/members"), "members")
        }
        for name in AGENTS:
            mail = email(tenant, name)
            # SEC-02: an agent has only its token. Its password is random, used once and not kept, so nobody can log in
            # as the agent and mint more tokens; reset on every run for users made before this rule.
            if mail not in users:
                api(
                    "POST",
                    "/v1/auth/email/admin/users",
                    {
                        "email": mail,
                        "password": password(),
                        "full_name": f"agent:{tenant}:{name}",
                        "password_change_required": False,
                    },
                )
            else:
                api("PATCH", f"/v1/auth/email/admin/users/{mail}", {"password": password()})
            if mail not in members:  # also after an interrupted run
                api("POST", f"/v1/teams/{team['id']}/members", {"email": mail, "role": "member"})
            agent = state["agents"].setdefault(f"agent:{tenant}:{name}", {"email": mail, "team": tenant})
            if "token" not in agent or token_expires(agent["token"]) < time.time() + 86400:  # reissue a day early
                tok = api(
                    "POST",
                    "/v1/tokens",
                    {
                        "name": f"agent-{tenant}-{name}-{int(time.time())}",
                        "team_id": team["id"],
                        "user_email": mail,
                        "expires_in_days": 30,
                    },
                )
                agent["token"] = tok.get("access_token") or tok.get("token")
        for connector, url in CONNECTORS.items():
            gw = gateways.get(f"{connector}-{tenant}") or api(
                "POST",
                "/v1/gateways",
                {
                    "name": f"{connector}-{tenant}",
                    "url": url,
                    "transport": "STREAMABLEHTTP",
                    "teamId": team["id"],
                    "visibility": "team",
                },
            )
            t.setdefault("gateways", {})[connector] = gw["id"]
            # the connector's current tools and schemas: ContextForge keeps what it saw at registration (M6)
            api("POST", f"/v1/gateways/{gw['id']}/tools/refresh")
        t["gateway_id"] = t["gateways"]["demo"]
    tools = virtual_servers(api, state)
    alpha = state["teams"]["t-alpha"]
    boards = alpha["tools"]["demo.boards.list"]
    if boards["name"] != ALIAS:
        boards["name"] = api("PUT", f"/v1/tools/{boards['id']}", {"customName": ALIAS})["name"]
    execute = next((x for x in tools if x.get("originalName") == EXECUTE or x.get("name") == EXECUTE), None)
    if execute is None:
        execute = api(
            "POST",
            "/v1/tools",
            {
                "tool": {
                    "name": EXECUTE,
                    "url": "http://demo:8080/healthz",
                    "integration_type": "REST",
                    "request_type": "GET",
                    "description": "EFF-02 probe: must never be callable by an agent",
                    "visibility": "team",  # the API reads it here for tools, not next to team_id
                },
                "team_id": alpha["id"],
                "visibility": "team",
            },
        )
    if execute.get("visibility") != "team":
        execute = api("PUT", f"/v1/tools/{execute['id']}", {"visibility": "team"})
    alpha["execute"] = {"id": execute["id"], "name": execute["name"]}
    STATE.write_text(json.dumps(state, indent=1))
    STATE.chmod(0o600)
    plugins = plugins_yaml(
        {t["id"]: tenant for tenant, t in state["teams"].items()},
        {g: connector for t in state["teams"].values() for connector, g in t["gateways"].items()},
    )
    if (SECRETS / "plugins.yaml").read_text() != plugins:
        (SECRETS / "plugins.yaml").write_text(plugins)
        subprocess.run(["docker", "compose", "restart", "contextforge"], cwd=REPO, check=True)
        wait_healthy()
    print(f"stand/bootstrap.py: OK ({STATE.relative_to(REPO)}, {ENV.name})")


if __name__ == "__main__":
    main()
