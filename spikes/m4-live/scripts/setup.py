"""Creates 2 teams x 2 agents, one echo gateway per team, a virtual server and an alias per team.

Idempotent enough for a fresh stand (`docker compose down && up`). Writes ids/tokens to secrets/state.json.
"""

import secrets
import sys

from cf import env, http, login, save_state

E = env()
admin = login(E["PLATFORM_ADMIN_EMAIL"], E["PLATFORM_ADMIN_PASSWORD"])
state = {"teams": {}, "agents": {}}


def ok(code, d, what):
    if code not in (200, 201):
        sys.exit(f"{what}: {code} {d}")
    return d


for team in ("t-alpha", "t-beta"):
    agents = [f"{team}-a1", f"{team}-a2"]
    for a in agents:
        for _ in range(5):  # random passwords sometimes trip ContextForge's "sequential characters" rule
            pw = "Cc3!" + secrets.token_urlsafe(18)
            user = {"email": f"{a}@example.com", "password": pw, "full_name": a}
            code, d, _ = http("POST", "/auth/email/admin/users", user, admin)
            if code != 400:
                break
        ok(code, d, f"user {a}")
        state["agents"][a] = {"email": f"{a}@example.com", "password": pw, "team": team}
    t = ok(
        *http(
            "POST",
            "/teams/",
            {
                "name": team,
                "slug": team,
                "visibility": "private",
                "members": [{"email": f"{a}@example.com", "role": "member"} for a in agents],
            },
            admin,
        )[:2],
        f"team {team}",
    )
    team_id = t["id"]
    # members seed may produce invitations; add explicitly as well
    for a in agents:
        http("POST", f"/teams/{team_id}/members", {"email": f"{a}@example.com", "role": "member"}, admin)
    tinfo = {"id": team_id}
    vs_tools = []
    for kind in ("direct", "alias"):
        gw = ok(
            *http(
                "POST",
                "/gateways",
                {
                    "name": f"echo-{kind}-{team}",
                    "url": f"http://echo:9100/{team}/{kind}/mcp",
                    "transport": "STREAMABLEHTTP",
                    "team_id": team_id,
                    "visibility": "team",
                },
                admin,
            )[:2],
            f"gateway {kind} {team}",
        )
        code, tools, _ = http("GET", f"/tools?team_id={team_id}", None, admin)
        tools = tools if isinstance(tools, list) else tools.get("items", [])
        tool = next(x for x in tools if x.get("gatewayId") == gw["id"])
        if kind == "alias":
            ok(*http("PUT", f"/tools/{tool['id']}", {"custom_name": f"{team}-alias"}, admin)[:2], f"alias {team}")
            tool = http("GET", f"/tools/{tool['id']}", None, admin)[1]
            tinfo["alias_original_name"] = tool.get("originalName")
        tinfo[f"{kind}_gateway_id"], tinfo[f"{kind}_tool_id"], tinfo[f"{kind}_tool"] = (
            gw["id"],
            tool["id"],
            tool["name"],
        )
        vs_tools.append(tool["id"])
    vs = ok(
        *http(
            "POST",
            "/servers",
            {"server": {"name": f"vs-{team}", "associated_tools": vs_tools, "team_id": team_id, "visibility": "team"}},
            admin,
        )[:2],
        f"virtual server {team}",
    )
    tinfo["server_id"] = vs["id"]
    state["teams"][team] = tinfo
    for a in agents:
        tok = login(f"{a}@example.com", state["agents"][a]["password"])
        code, d, _ = http("POST", "/tokens", {"name": f"{a}-api", "team_id": team_id, "expires_in_days": 1}, tok)
        ok(code, d, f"token {a}")
        state["agents"][a]["token"] = d.get("access_token") or d.get("token")

save_state(state)
print(state["teams"])
