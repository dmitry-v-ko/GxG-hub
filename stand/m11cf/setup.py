"""M11.3/M11.4 stand check: secrets and registration of gxg-telegram-service in ContextForge (stand/m11cf/run.sh).

  uv run python stand/m11cf/setup.py init        after stand/init.py, before `docker compose up`
  uv run python stand/m11cf/setup.py bootstrap   after stand/bootstrap.py

init: random passwords (.env, M11CF_*), a fake api_hash and chat_ref secret, the service's db/ SQL (copied out of the
image) + one synthetic chat of Connection svc-test, the stand tenant file (policies/tenants/example-ext-test.yaml with
its chat replaced by a chat_ref the service can resolve), a bundle of that tenant signed with the stand bundle key,
and the unified_pdp rules: stand/unified_pdp.rules.json + gateway/contextforge/ext_roles.py of the tenant file.
bootstrap: team t-ext-test, agents reader and collector (one team token each, SEC-02), gateway telegram-t-ext-test ->
http://tgsvc:8080/mcp, one virtual server per agent tool set (ext_roles.build), plugins.yaml with the team and the
gateway, ContextForge restarted if it changed. State -> stand/secrets/m11cf/state.json. Synthetic only.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "gateway" / "contextforge"))
import bootstrap as base  # noqa: E402
import ext_roles  # noqa: E402
from init import ENV, KID_BUNDLE, REPO, SECRETS, env_values, password, plugins_yaml  # noqa: E402

OUT = SECRETS / "m11cf"
STATE = OUT / "state.json"
IMAGE = "gxg-telegram-service:m11-4-local"
TENANT_FILE = REPO / "policies" / "tenants" / "example-ext-test.yaml"
TENANT, CONNECTION, SVC_CONNECTION = "t-ext-test", "tg-test", "svc-test"
PEER = -1001000000777  # synthetic peer of the test chat (never a real one)
GATEWAY = f"telegram-{TENANT}"
SVC_URL = "http://tgsvc:8080/mcp"


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)  # read by non-root users of the containers; the directory stays 0700


def _in_image(script: str, *args: str) -> str:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", IMAGE, "-I", "-c", script, *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def init() -> None:
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    added = {"M11CF_DB_OWNER_PASSWORD", "M11CF_READER_PASSWORD", "M11CF_SERVICE_PASSWORD"} - set(env_values())
    with ENV.open("a") as f:
        f.writelines(f"{k}={secrets.token_urlsafe(24)}\n" for k in sorted(added))
    env = env_values()
    if not (OUT / "api_hash").exists():
        _write(OUT / "api_hash", secrets.token_hex(16))  # fake: no Telegram application behind it
    if not (OUT / "chat_ref_secret").exists():
        _write(OUT / "chat_ref_secret", secrets.token_urlsafe(48))
    ref = _in_image(
        "import sys; from gxg_telegram_service.chatrefs import ChatRefs;"
        "print(ChatRefs(sys.argv[1]).issue(sys.argv[2], int(sys.argv[3])))",
        (OUT / "chat_ref_secret").read_text().strip(),
        SVC_CONNECTION,
        str(PEER),
    )
    # The tenant file of the check: the repository's example-ext-test.yaml, its placeholder chat -> the stand chat.
    doc = yaml.safe_load(TENANT_FILE.read_text())
    placeholder = "telegram:tg-test/chat/tgc1_EXAMPLEtestChat0000000000000000000"
    for g in doc["grants"]:
        g["resources"] = [f"telegram:{CONNECTION}/chat/{ref}" if r == placeholder else r for r in g["resources"]]
    pol = OUT / "policies"
    shutil.rmtree(pol, ignore_errors=True)
    (pol / "tenants").mkdir(parents=True)
    (pol / "platform").mkdir()
    (pol / "tenants" / f"{TENANT}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    shutil.copy(REPO / "policies" / "platform" / "registry.example.yaml", pol / "platform")
    # The service's manifest and db/ come from the image under test.
    cid = subprocess.run(["docker", "create", IMAGE], capture_output=True, text=True, check=True).stdout.strip()
    try:
        for src, dst in (("/app/gxg-manifest.yaml", OUT / "gxg-manifest.yaml"), ("/app/db", OUT / "db")):
            shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else None
            subprocess.run(["docker", "cp", f"{cid}:{src}", str(dst)], check=True, capture_output=True)
    finally:
        subprocess.run(["docker", "rm", cid], check=True, capture_output=True)
    seed = base64.urlsafe_b64decode((SECRETS / "bundle.seed").read_text() + "=")
    version = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True)
    tmp = OUT / "bundle.new"
    shutil.rmtree(tmp, ignore_errors=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "gxg_policy.cli",
            "build",
            str(pol),
            "--out",
            str(tmp),
            "--kid",
            KID_BUNDLE,
            "--manifest",
            str(OUT / "gxg-manifest.yaml"),
            "--bundle-version",
            version.stdout.strip(),
        ],
        check=True,
        env=os.environ | {"GXG_BUNDLE_SIGNING_KEY": base64.urlsafe_b64encode(seed).rstrip(b"=").decode()},
        stdout=subprocess.DEVNULL,
    )
    (OUT / "bundle").mkdir(exist_ok=True)
    (OUT / "bundle").chmod(0o755)
    for p in sorted(tmp.iterdir()):
        p.chmod(0o644)
        p.replace(OUT / "bundle" / p.name)
    tmp.rmdir()
    db = OUT / "db"
    parts = [
        (db / n).read_text()
        for n in (
            "00_capability_hub_connections.sql",
            "20261105010000_telegram_archive.sql",
            "20261106010000_telegram_archive_semantic.sql",
            "10_telegram_service.sql",
            "reader_role.sql",
            "service_role.sql",
        )
    ]
    scope = json.dumps({"provider": "telegram", "scope": {"chats": [{"chat_ref": ref}], "folders": []}})
    parts.append(
        f"ALTER ROLE gxg_telegram_reader PASSWORD '{env['M11CF_READER_PASSWORD']}';\n"
        f"ALTER ROLE gxg_telegram_service PASSWORD '{env['M11CF_SERVICE_PASSWORD']}';\n"
        "INSERT INTO capability_hub.connections (id, tenancy_mode, credential_ref, metadata)\n"
        f"  VALUES ('{SVC_CONNECTION}', 'shared', 'secret://m11cf/{SVC_CONNECTION}', '{scope}'::jsonb);\n"
        "INSERT INTO telegram_archive.chats (connection_id, peer_id, chat_ref, state, synced_at)\n"
        f"  VALUES ('{SVC_CONNECTION}', {PEER}, '{ref}', 'synced', '2026-11-20T00:00:00Z');\n"
    )
    _write(OUT / "db-init.sql", "\n".join(parts))
    roles = ext_roles.build(doc)
    rules = json.loads((REPO / "stand" / "unified_pdp.rules.json").read_text()) + roles["pdp_rules"]
    _write(OUT / "unified_pdp.rules.json", json.dumps(rules, indent=1))
    _write(OUT / "roles.json", json.dumps(roles, indent=1))
    _write(OUT / "chat_ref", ref)
    print("stand/m11cf/setup.py init: OK")


def bootstrap() -> None:
    env = env_values()
    base.wait_healthy()
    api = base.Api(base.login(env))
    roles = json.loads((OUT / "roles.json").read_text())
    state: dict[str, Any] = json.loads(STATE.read_text()) if STATE.exists() else {"agents": {}}
    teams = {t["slug"]: t for t in base.items(api("GET", "/v1/teams/?limit=100"), "teams")}
    team = teams.get(TENANT) or api("POST", "/v1/teams/", {"name": TENANT, "slug": TENANT, "visibility": "private"})
    state["team_id"] = team["id"]
    users = {u["email"] for u in base.items(api("GET", "/v1/auth/email/admin/users?limit=200"), "users")}
    members = {
        m.get("user_email") or m.get("email")
        for m in base.items(api("GET", f"/v1/teams/{team['id']}/members"), "members")
    }
    for principal in roles["agents"]:
        mail = ext_roles.agent_email(principal)
        if mail not in users:
            api(
                "POST",
                "/v1/auth/email/admin/users",
                {"email": mail, "password": password(), "full_name": principal, "password_change_required": False},
            )
        else:
            api("PATCH", f"/v1/auth/email/admin/users/{mail}", {"password": password()})
        if mail not in members:
            api("POST", f"/v1/teams/{team['id']}/members", {"email": mail, "role": "member"})
        agent = state["agents"].setdefault(principal, {"email": mail})
        if "token" not in agent or base.token_expires(agent["token"]) < time.time() + 86400:
            tok = api(
                "POST",
                "/v1/tokens",
                {
                    "name": f"agent-{principal.replace(':', '-')}-{int(time.time())}",
                    "team_id": team["id"],
                    "user_email": mail,
                    "expires_in_days": 30,
                },
            )
            agent["token"] = tok.get("access_token") or tok.get("token")
    gateways = {g["name"]: g for g in base.items(api("GET", "/v1/gateways?include_inactive=true&limit=0"), "gateways")}
    gw = gateways.get(GATEWAY) or api(
        "POST",
        "/v1/gateways",
        {"name": GATEWAY, "url": SVC_URL, "transport": "STREAMABLEHTTP", "teamId": team["id"], "visibility": "team"},
    )
    state["gateway_id"] = gw["id"]
    tools = [
        x
        for x in base.items(api("GET", "/v1/tools?include_inactive=true&limit=0"), "tools")
        if x.get("gatewayId") == gw["id"]
    ]
    state["tools"] = {x["originalName"]: {"id": x["id"], "name": x["name"]} for x in tools}
    servers = {s["name"]: s for s in base.items(api("GET", "/v1/servers?include_inactive=true&limit=0"), "servers")}
    state["servers"] = {}
    for name, wanted in roles["servers"].items():
        ids = [state["tools"][t]["id"] for t in wanted if t in state["tools"]]
        srv = servers.get(name) or api(
            "POST",
            "/v1/servers",
            {"server": {"name": name, "associated_tools": ids}, "team_id": team["id"], "visibility": "team"},
        )
        state["servers"][name] = {
            "id": srv["id"],
            "tools": wanted,
            "missing": [t for t in wanted if t not in state["tools"]],
        }
    for principal, server in roles["agents"].items():
        state["agents"][principal]["server"] = server
    STATE.write_text(json.dumps(state, indent=1))
    STATE.chmod(0o600)
    # plugins.yaml: the stand's teams and gateways (stand/secrets/state.json) + this team and gateway
    stand = json.loads((SECRETS / "state.json").read_text())
    plugins = plugins_yaml(
        {t["id"]: tenant for tenant, t in stand["teams"].items()} | {team["id"]: TENANT},
        {t["gateway_id"]: "demo" for t in stand["teams"].values()}
        | {t["tg_gateway_id"]: "telegram" for t in stand["teams"].values()}
        | {gw["id"]: "telegram"},
    )
    if (SECRETS / "plugins.yaml").read_text() != plugins:
        (SECRETS / "plugins.yaml").write_text(plugins)
        subprocess.run(["docker", "compose", "restart", "contextforge"], cwd=REPO, check=True)
        base.wait_healthy()
    print(f"stand/m11cf/setup.py bootstrap: OK ({STATE.relative_to(REPO)}, {ENV.name})")


if __name__ == "__main__":
    {"init": init, "bootstrap": bootstrap}[sys.argv[1]]()
