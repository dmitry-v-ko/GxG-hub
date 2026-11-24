#!/usr/bin/env python3
"""The pilot agent in ContextForge (M13): one-off jobs inside Railway and what follows from their output.

    RAILWAY_TOKEN=... python scripts/deploy/pilot_agent.py setup   # team, agent user + API token, gateways, virtual servers
    RAILWAY_TOKEN=... python scripts/deploy/pilot_agent.py probe   # the agent lists tools through ContextForge

`setup` runs `deploy/railway/pilot_agent_job.py` (it is in the Approval image) as a temporary service (platform admin credentials by reference to
ContextForge's variables, never read here), reads its log (ids, tool names, the agent token SEALED to a one-time RSA key
made in this process), stores the token in the vault service `pilot-agent` (PILOT_AGENT_TOKEN, AS-28 `agent_token`) and the
ids in PILOT_AGENT_IDS, deletes the job. Neither the token nor the admin password is printed or written to disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from scripts.deploy import railway_deploy as rd

VAULT = "pilot-agent"
APPROVAL = "deploy/railway/approval.Dockerfile"
UI = "deploy/railway/ui.Dockerfile"
# Runs in the ui image: the saved scope of the pilot (chat refs in the order the operator chose them) with the titles.
SCOPE_PY = (
    "import os,httpx2\n"
    "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K\n"
    "from gxg_connector_kit import _jwt\n"
    "from gxg_approval_ui.tgadmin import TelegramAdmin\n"
    "key=K.from_private_bytes(_jwt.b64url_decode(open(os.environ['GXG_UI_SVC_KEY_FILE']).read().strip()))\n"
    "api=TelegramAdmin(httpx2.Client(base_url=os.environ['GXG_TELEGRAM_ADMIN_URL']),key)\n"
    "who=os.environ['GXG_OPERATOR']\n"
    "s=api.call('GET','scope',who)\n"
    "refs=s.get('chat_refs',[]) if isinstance(s,dict) else s\n"
    "t={c['chat_ref']:c for c in api.call('GET','chats',who)}\n"
    "[print('SCOPE',i,r,repr(t.get(r,{}).get('title')),t.get(r,{}).get('kind'),flush=True) for i,r in enumerate(refs)]\n"
)
Q_DEPLOY = "mutation Deploy($s: String!, $e: String!) { serviceInstanceDeployV2(serviceId: $s, environmentId: $e) }"
Q_DEPLOYMENTS = """query Deployments($input: DeploymentListInput!) { deployments(first: 1, input: $input) {
  edges { node { id status } } } }"""
Q_LOGS = "query Logs($d: String!, $l: Int) { deploymentLogs(deploymentId: $d, limit: $l) { message } }"
Q_BUILD_LOGS = "query BuildLogs($d: String!, $l: Int) { buildLogs(deploymentId: $d, limit: $l) { message } }"
Q_UPSERT_ONE = "mutation Upsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }"
FINAL = {"SUCCESS", "FAILED", "CRASHED", "REMOVED"}
OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def unseal(sealed: str, private: rsa.RSAPrivateKey) -> str:
    wrapped, _, token = sealed.partition(".")
    key = private.decrypt(base64.b64decode(wrapped), OAEP)
    return Fernet(key).decrypt(token.encode()).decode()


def parse_log(lines: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """(ids, sealed token) from the job's log lines."""
    ids, sealed = None, None
    for line in lines:
        if line.startswith("IDS "):
            ids = json.loads(line[4:])
        elif line.startswith("SEALED "):
            sealed = line[7:].strip()
    return ids, sealed


class Jobs:
    def __init__(self, api: rd.Railway, out: rd.Out) -> None:
        self.api, self.out = api, out
        self.spec = rd.load_spec()
        self.dep = rd.Deployer(api, self.spec, out)
        self.dep.discover(create=False)

    def variables(self, service: str) -> dict[str, str]:
        return dict(self.dep.read_variables(service))

    def set_var(self, service: str, name: str, value: str, *, secret: bool) -> None:
        if secret:
            self.out.add(value)
        self.api.call(
            Q_UPSERT_ONE,
            input={
                "projectId": self.dep.project_id, "environmentId": self.dep.env_id,
                "serviceId": self.dep.service_ids[service], "name": name, "value": value, "skipDeploys": True,
            },
        )  # fmt: skip
        self.out.say(f"set {name} on {service}")

    def run(  # noqa: PLR0917
        self,
        name: str,
        variables: dict[str, str],
        timeout_s: int = 600,
        start: str = "",
        dockerfile: str = APPROVAL,
        source: dict[str, str] | None = None,
    ) -> list[str]:
        """A temporary job service built from this repository: create, run once, return its log, delete."""
        dep = self.dep
        self.spec["services"][name] = {"kind": "job", "source": {**(source or {}), "dockerfile": dockerfile}}
        if name in dep.service_ids:  # a leftover of an interrupted run
            self.api.call(rd.Q_SERVICE_DELETE, id=dep.service_ids.pop(name), environmentId=dep.env_id)
        made = self.api.call(
            rd.Q_SERVICE_CREATE,
            input={"projectId": dep.project_id, "environmentId": dep.env_id, "name": name,
                   **dep.source_input(self.spec["services"][name])},
        )  # fmt: skip
        dep.service_ids[name] = made["serviceCreate"]["id"]
        for value in variables.values():
            self.out.add(value)
        self.api.call(
            rd.Q_UPSERT,
            input={
                "projectId": dep.project_id, "environmentId": dep.env_id, "serviceId": dep.service_ids[name],
                "variables": {"RAILWAY_DOCKERFILE_PATH": dockerfile, **variables},
                "skipDeploys": True,
            },
        )  # fmt: skip
        dep.ensure_source(name)
        self.api.call(
            rd.Q_INSTANCE_UPDATE,
            serviceId=dep.service_ids[name], environmentId=dep.env_id,
            input={"startCommand": start
                    or (
                        "/opt/gxg/entrypoint.sh python /opt/gxg/pilot_agent_job.py"
                        if dockerfile == APPROVAL
                        else "/opt/gxg/entrypoint.sh python -c \"import os; exec(os.environ['GXG_JOB_PY'])\""
                    ),
                   "restartPolicyType": "NEVER"},
        )  # fmt: skip
        self.api.call(Q_DEPLOY, s=dep.service_ids[name], e=dep.env_id)
        self.out.say(f"job {name} started")
        end, status, deployment = time.monotonic() + timeout_s, "", ""
        try:
            while time.monotonic() < end:
                time.sleep(10)
                edges = self.api.call(
                    Q_DEPLOYMENTS, input={"projectId": dep.project_id, "environmentId": dep.env_id,
                                          "serviceId": dep.service_ids[name]},
                )["deployments"]["edges"]  # fmt: skip
                if edges:
                    deployment, status = edges[0]["node"]["id"], edges[0]["node"]["status"]
                    if status in FINAL:
                        break
            lines: list[str] = []
            for _ in range(6):  # the log pipeline lags behind the status
                time.sleep(10)
                lines = [m["message"] for m in self.api.call(Q_LOGS, d=deployment, l=2000)["deploymentLogs"]]
                if any(x.startswith(("IDS ", "SEALED ")) for x in lines) or len(lines) > 3:
                    break
            if status != "SUCCESS":  # why a deployment failed before it ran: the build log (no variable values in it)
                build = self.api.call(Q_BUILD_LOGS, d=deployment, l=40)["buildLogs"]
                lines += ["build: " + m["message"] for m in build]
            self.out.say(f"job {name}: {status or 'no deployment'}")
            return lines
        finally:
            self.api.call(rd.Q_SERVICE_DELETE, id=dep.service_ids[name], environmentId=dep.env_id)
            self.out.say(f"job {name} deleted")

    def setup(self) -> int:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        lines = self.run(
            "pilot-agent-setup",
            {
                "GXG_JOB": "setup", "GXG_JOB_SEAL_PUBKEY": pem.decode(),
                "PLATFORM_ADMIN_EMAIL": "${{contextforge.PLATFORM_ADMIN_EMAIL}}",
                "PLATFORM_ADMIN_PASSWORD": "${{contextforge.PLATFORM_ADMIN_PASSWORD}}",
            },
        )  # fmt: skip
        ids, sealed = parse_log(lines)
        if ids is None or sealed is None:
            for line in [x for x in lines if not x.startswith("build: ")][
                -25:
            ]:  # the job's own messages carry no secret
                self.out.say("  " + re.sub(r"\s+", " ", line)[:300])
            raise rd.DeployError("the setup job did not finish: no ids or token in its log")
        token = unseal(sealed, private)
        self.set_var(VAULT, "PILOT_AGENT_TOKEN", token, secret=True)
        self.set_var(VAULT, "PILOT_AGENT_IDS", json.dumps(ids), secret=False)
        for tool in sorted(ids["tools"], key=lambda t: t["name"]):
            self.out.say(f"tool {tool['name']} (original {tool['original']})")
        self.out.say(f"team {ids['team']}, gateways {ids['gateways']}, servers {ids['servers']}")
        return 0

    def propose(self) -> int:
        """ONE proposal of the pilot's first send, through ContextForge as the agent. The idempotency key makes a repeat
        (a retried job) the same intent. Nothing is sent: a person approves it in the UI with a passkey."""
        args = {
            "chat_ref": os.environ["GXG_SEND_CHAT_REF"], "text": os.environ["GXG_SEND_TEXT"],
            "idempotency_key": "m13-first-send-1", "business_task_id": "m13-first-send",
        }  # fmt: skip
        return self.probe(only="telegram", args=json.dumps(args))

    def peek(self, command: str) -> int:
        """Print a text file of the Telegram service image (its entrypoint: code, no secrets) to see how it wires things."""
        lines = self.run(
            "pilot-agent-peek",
            {"GXG_X": "1"},
            start=command,
            dockerfile="Dockerfile",
            source={"repo": "dmitry-v-ko/gxg-telegram-service", "branch": "claude/m13-railway"},
        )
        for line in lines:
            if not line.startswith("build: "):
                self.out.say("  " + line[:300])
        return 0

    def scope(self) -> int:
        lines = self.run(
            "pilot-agent-scope",
            {
                "GXG_JOB_PY": SCOPE_PY,
                "GXG_SECRET__GXG_UI_SVC_KEY": "${{ui.GXG_SECRET__GXG_UI_SVC_KEY}}",  # a reference: never read here
                "GXG_TELEGRAM_ADMIN_URL": "${{ui.GXG_TELEGRAM_ADMIN_URL}}",
                "GXG_OPERATOR": os.environ.get("GXG_OPERATOR", ""),
            },
            dockerfile=UI,
        )
        for line in lines:
            if line.startswith("SCOPE ") or re.search(r"Traceback|Error|Failed|not found", line):
                self.out.say("  " + line[:300])
        return 0

    def revoke(self) -> int:
        lines = self.run(
            "pilot-agent-revoke",
            {
                "GXG_JOB": "revoke",
                "PLATFORM_ADMIN_EMAIL": "${{contextforge.PLATFORM_ADMIN_EMAIL}}",
                "PLATFORM_ADMIN_PASSWORD": "${{contextforge.PLATFORM_ADMIN_PASSWORD}}",
            },
        )
        for line in lines:
            if re.match(r"^(GET |agent tokens|revoke |kept )", line) or re.search(
                r"Traceback|Error|Failed|not found", line
            ):
                self.out.say("  " + line[:400])
        return 0

    def probe(self, only: str = "", args: str = "") -> int:
        have = self.variables(VAULT)
        ids = json.loads(have["PILOT_AGENT_IDS"])
        for gateway, server in sorted(ids["servers"].items()):
            if only and only not in gateway:
                continue
            lines = self.run(
                "pilot-agent-probe",
                {
                    "GXG_JOB": "probe", "GXG_PROBE_SERVER": server, "GXG_PROBE_ARGS": args,
                    "PILOT_AGENT_TOKEN": f"${{{{{VAULT}.PILOT_AGENT_TOKEN}}}}",
                },
            )  # fmt: skip
            self.out.say(f"== {gateway}")
            for line in lines:
                if re.match(r"^(initialize|tools/list|tool |propose |schema )", line) or re.search(
                    r"Traceback|Error|Failed|not found", line
                ):
                    self.out.say("  " + line[:1000])
        return 0


def main(argv: list[str] | None = None, *, transport: rd.Transport | None = None, out: rd.Out | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("action", choices=["setup", "probe", "revoke", "scope", "propose", "peek"])
    args = ap.parse_args(argv)
    out = out or rd.Out()
    try:
        token = os.environ.get("RAILWAY_TOKEN")
        if token:
            out.add(token)
        if transport is None:
            if not token:
                raise rd.DeployError("RAILWAY_TOKEN is not set")
            transport = rd.http_transport(token)
        jobs = Jobs(rd.Railway(transport), out)
        return {
            "setup": jobs.setup,
            "probe": jobs.probe,
            "revoke": jobs.revoke,
            "scope": jobs.scope,
            "propose": jobs.propose,
            "peek": lambda: jobs.peek(os.environ["GXG_PEEK"]),
        }[args.action]()
    except rd.DeployError as exc:
        out.say(f"error: {exc}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
