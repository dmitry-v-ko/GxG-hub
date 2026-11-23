"""A stateful in-memory stand-in for the Railway GraphQL API (only the operations railway_deploy.py uses)."""

from __future__ import annotations

import itertools
import re
from typing import Any

MUTATIONS = {
    "ProjectCreate", "ServiceCreate", "ServiceDelete", "ServiceInstanceUpdate", "ServiceInstanceRedeploy",
    "ServiceConnect",
    "VariableCollectionUpsert", "VolumeCreate", "ServiceDomainDelete", "CustomDomainDelete", "TcpProxyDelete",
}  # fmt: skip


class MockRailway:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.project: dict[str, Any] | None = None
        self.services: dict[str, dict[str, Any]] = {}  # id -> {name, vars, instance, volume}
        self.shared: dict[str, str] = {}
        self.service_domains: dict[str, list[dict[str, str]]] = {}
        self.custom_domains: dict[str, list[dict[str, str]]] = {}
        self.tcp: dict[str, list[dict[str, Any]]] = {}
        self.mutations: list[str] = []
        self.fail_with: str | None = None

    def new_id(self) -> str:
        return f"id-{next(self._ids)}"

    def by_name(self, name: str) -> dict[str, Any]:
        return next(s for s in self.services.values() if s["name"] == name)

    def sid(self, name: str) -> str:
        return next(i for i, s in self.services.items() if s["name"] == name)

    def _project_node(self) -> dict[str, Any]:
        assert self.project is not None
        return {
            "id": "p1",
            "name": self.project["name"],
            "environments": {"edges": [{"node": {"id": "e1", "name": self.project["env"]}}]},
            "services": {"edges": [{"node": {"id": i, "name": s["name"]}} for i, s in self.services.items()]},
        }

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        op = re.search(r"(?:query|mutation)\s+(\w+)", query)
        assert op, query
        name = op.group(1)
        if name in MUTATIONS:
            self.mutations.append(name)
        if self.fail_with:
            return {"errors": [{"message": self.fail_with}]}
        return {"data": getattr(self, f"op_{name}")(variables)}

    # -- operations
    def op_Projects(self, v: dict[str, Any]) -> dict[str, Any]:
        nodes = [self._project_node()] if self.project else []
        return {"projects": {"edges": [{"node": n} for n in nodes]}}

    def op_ProjectCreate(self, v: dict[str, Any]) -> dict[str, Any]:
        self.project = {"name": v["input"]["name"], "env": v["input"]["defaultEnvironmentName"]}
        assert v["input"]["isPublic"] is False
        return {"projectCreate": self._project_node()}

    def op_ServiceCreate(self, v: dict[str, Any]) -> dict[str, Any]:
        sid = self.new_id()
        src = v["input"].get("source", {})
        # the live API bound a repository service to `main` whatever `branch` said: the mock keeps that behaviour
        trigger = {"repository": src["repo"], "branch": "main"} if "repo" in src else None
        self.services[sid] = {
            "name": v["input"]["name"],
            "vars": {},
            "instance": {},
            "volume": None,
            "trigger": trigger,
        }
        return {"serviceCreate": {"id": sid, "name": v["input"]["name"]}}

    def op_ServiceDelete(self, v: dict[str, Any]) -> dict[str, Any]:
        del self.services[v["id"]]
        return {"serviceDelete": True}

    def op_ServiceInstance(self, v: dict[str, Any]) -> dict[str, Any]:
        return {"serviceInstance": dict(self.services[v["serviceId"]]["instance"])}

    def op_ServiceInstanceUpdate(self, v: dict[str, Any]) -> dict[str, Any]:
        self.services[v["serviceId"]]["instance"].update(v["input"])
        return {"serviceInstanceUpdate": True}

    def op_ServiceInstanceRedeploy(self, v: dict[str, Any]) -> dict[str, Any]:
        return {"serviceInstanceRedeploy": True}

    def op_Variables(self, v: dict[str, Any]) -> dict[str, Any]:
        sid = v.get("serviceId")
        return {"variables": dict(self.services[sid]["vars"]) if sid else dict(self.shared)}

    def op_VariableCollectionUpsert(self, v: dict[str, Any]) -> dict[str, Any]:
        self.services[v["input"]["serviceId"]]["vars"].update(v["input"]["variables"])
        return {"variableCollectionUpsert": True}

    def op_Volumes(self, v: dict[str, Any]) -> dict[str, Any]:
        nodes = [
            {"id": f"v-{i}", "volumeInstances": {"edges": [{"node": {"serviceId": i, "mountPath": s["volume"]}}]}}
            for i, s in self.services.items()
            if s["volume"]
        ]
        return {"project": {"volumes": {"edges": [{"node": n} for n in nodes]}}}

    def op_VolumeCreate(self, v: dict[str, Any]) -> dict[str, Any]:
        self.services[v["input"]["serviceId"]]["volume"] = v["input"]["mountPath"]
        return {"volumeCreate": {"id": self.new_id()}}

    def op_Domains(self, v: dict[str, Any]) -> dict[str, Any]:
        sid = v["serviceId"]
        return {
            "domains": {
                "serviceDomains": self.service_domains.get(sid, []),
                "customDomains": self.custom_domains.get(sid, []),
            }
        }

    def op_TcpProxies(self, v: dict[str, Any]) -> dict[str, Any]:
        return {"tcpProxies": self.tcp.get(v["serviceId"], [])}

    def op_ServiceDomainDelete(self, v: dict[str, Any]) -> dict[str, Any]:
        for sid, items in self.service_domains.items():
            self.service_domains[sid] = [d for d in items if d["id"] != v["id"]]
        return {"serviceDomainDelete": True}

    def op_CustomDomainDelete(self, v: dict[str, Any]) -> dict[str, Any]:
        for sid, items in self.custom_domains.items():
            self.custom_domains[sid] = [d for d in items if d["id"] != v["id"]]
        return {"customDomainDelete": True}

    def op_TcpProxyDelete(self, v: dict[str, Any]) -> dict[str, Any]:
        for sid, items in self.tcp.items():
            self.tcp[sid] = [d for d in items if d["id"] != v["id"]]
        return {"tcpProxyDelete": True}

    def op_DeploymentTriggers(self, v: dict[str, Any]) -> dict[str, Any]:
        trigger = self.services[v["serviceId"]]["trigger"]
        return {"deploymentTriggers": {"edges": [{"node": trigger}] if trigger else []}}

    def op_ServiceConnect(self, v: dict[str, Any]) -> dict[str, Any]:
        self.services[v["id"]]["trigger"] = {"repository": v["input"]["repo"], "branch": v["input"]["branch"]}
        return {"serviceConnect": {"id": v["id"]}}
