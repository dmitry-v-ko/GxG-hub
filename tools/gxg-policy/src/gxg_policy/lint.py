"""Cross-reference lint for policies (GR-04…GR-09, AP-02, approval-policy «Lint в CI»).

Runs on top of `load()`: schema errors come from there, this module adds what a schema can't see.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

from gxg_policy.load import REPO_ROOT, Finding, Policies, load, parse_ts, parse_yaml, schema_findings

# effects-and-approval-v1.md «Сроки по умолчанию»; a policy may only shorten them.
SYSTEM_DEFAULTS = {"approval_ttl": 24 * 3600, "execution_window": 3600}
EXPIRY_WARNING = timedelta(days=14)  # GR-06


def seconds(duration: str) -> int:
    return int(duration[:-1]) * {"m": 60, "h": 3600, "d": 86400}[duration[-1]]


def agent_tenant(principal: str) -> str | None:
    """Tenant segment of `agent:<tenant>:<name>` (format of all examples; VERIFY after the ContextForge spike)."""
    parts = principal.split(":")
    return parts[1] if parts[0] == "agent" and len(parts) >= 3 else None


def expand(refs: Iterable[str], groups: dict[str, list[str]]) -> list[str]:
    """Replace `group:<name>` with its members; keep order, drop duplicates."""
    out: list[str] = []
    for ref in refs:
        for p in groups.get(ref[6:], []) if ref.startswith("group:") else [ref]:
            if p not in out:
                out.append(p)
    return out


def default_manifests() -> list[Path]:
    """The manifests whose tools go into the bundle (GR-11a): AP-02 checks the same classes AS-26 enforces."""
    return sorted(REPO_ROOT.glob("connectors/*/gxg-manifest.yaml"))


def load_manifests(pol: Policies, paths: Iterable[Path]) -> None:
    """Schema-checked connector manifests into `pol.manifests` (connector id -> manifest); errors as findings."""
    for path in paths:
        try:
            doc = parse_yaml(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
            pol.findings.append(Finding(str(path), "", "manifest", str(exc).replace("\n", " ")))
            continue
        bad = schema_findings(str(path), "gxg-manifest.schema.json", doc)
        if not bad and doc["connector"] in pol.manifests:
            bad = [Finding(str(path), "connector", "duplicate-id", f"second manifest for {doc['connector']!r}")]
        if not bad:  # AP-02 and AS-26 look tools up by name: one name, one class, also inside one manifest
            seen = {t["name"] for m in pol.manifests.values() for t in m["tools"]}
            for i, t in enumerate(doc["tools"]):
                if t["name"] in seen:
                    bad.append(
                        Finding(
                            str(path), f"tools[{i}].name", "duplicate-tool", f"tool {t['name']!r} is declared twice"
                        )
                    )
                seen.add(t["name"])
        pol.findings += bad
        if not bad:
            pol.manifests[doc["connector"]] = doc


def rid_overlap(a: str, b: str) -> bool:
    return a == b or (a.endswith("/*") and b.startswith(a[:-1])) or (b.endswith("/*") and a.startswith(b[:-1]))


class _Linter:
    def __init__(self, pol: Policies, now: datetime) -> None:
        self.pol, self.now = pol, now
        self.classes = {t["name"]: t["effect_class"] for m in pol.manifests.values() for t in m["tools"]}
        self.out: list[Finding] = []

    def err(self, file: str, path: str, code: str, msg: str, level: str = "error") -> None:
        self.out.append(Finding(file, path, code, msg, level))

    def unique(self, file: str, items: Iterable[tuple[str, str]], what: str) -> None:
        seen: set[str] = set()
        for path, value in items:
            if value in seen:
                self.err(file, path, "duplicate-id", f"duplicate {what} id {value!r}")
            seen.add(value)

    def foreign(self, file: str, path: str, tenant: str, principal: str) -> bool:
        other = agent_tenant(principal)
        if other is not None and other != tenant:
            self.err(file, path, "foreign-principal", f"{principal!r} belongs to tenant {other!r}, not {tenant!r}")
            return True
        return False

    def rid(self, file: str, path: str, rid: str, conns: dict[str, str]) -> None:
        domain, rest = rid.split(":", 1)
        conn = rest.split("/", 1)[0]
        if conn not in conns:
            self.err(file, path, "dangling-connection", f"connection {conn!r} of {rid!r} is not declared")
        elif conns[conn] != domain:
            self.err(file, path, "rid-domain", f"{rid!r}: connection {conn!r} has domain {conns[conn]!r}")

    def tenant_file(self, file: str, doc: dict[str, Any]) -> None:  # noqa: PLR0912 - flat list of independent checks
        tenant = doc["tenant"]
        conns = {c["id"]: c["domain"] for c in doc["connections"]}
        self.unique(file, ((f"connections[{i}].id", c["id"]) for i, c in enumerate(doc["connections"])), "connection")
        agents = doc["principals"].get("agents", [])
        humans = doc["principals"].get("humans", [])
        self.unique(
            file,
            [(f"principals.agents[{i}].id", a["id"]) for i, a in enumerate(agents)]
            + [(f"principals.humans[{i}].id", h["id"]) for i, h in enumerate(humans)],
            "principal",
        )
        known = {a["id"] for a in agents} | {h["id"] for h in humans}
        for i, a in enumerate(agents):
            self.foreign(file, f"principals.agents[{i}].id", tenant, a["id"])
        groups: dict[str, list[str]] = doc.get("groups", {})
        for name, members in groups.items():
            for i, m in enumerate(members):
                if not self.foreign(file, f"groups.{name}[{i}]", tenant, m) and m not in known:
                    self.err(file, f"groups.{name}[{i}]", "dangling-subject", f"{m!r} is not a declared principal")
        self.unique(file, ((f"grants[{i}].id", g["id"]) for i, g in enumerate(doc["grants"])), "grant")
        domains = set(conns.values())
        enabled = doc.get("extensions", {}).get("enabled", [])
        self.unique(file, ((f"extensions.enabled[{i}]", c) for i, c in enumerate(enabled)), "extensions.enabled entry")
        for i, c in enumerate(enabled):
            if c.split(":", 1)[0] not in domains:
                self.err(file, f"extensions.enabled[{i}]", "ext-domain", f"no connection with the domain of {c!r}")
        for i, g in enumerate(doc["grants"]):
            for j, s in enumerate(g["subjects"]):
                path = f"grants[{i}].subjects[{j}]"
                if s.startswith("group:"):
                    if s[6:] not in groups:
                        self.err(file, path, "dangling-subject", f"group {s[6:]!r} is not declared")
                    elif any(not m.startswith("agent:") for m in groups[s[6:]]):
                        self.err(file, path, "group-human-subject", f"group {s[6:]!r} has non-agent members")
                elif not self.foreign(file, path, tenant, s) and s not in known:
                    self.err(file, path, "dangling-subject", f"{s!r} is not a declared principal")
            for j, r in enumerate(g["resources"]):
                self.rid(file, f"grants[{i}].resources[{j}]", r, conns)
            if "expires_at" in g:
                left = parse_ts(g["expires_at"]) - self.now
                if left <= timedelta(0):
                    self.err(file, f"grants[{i}].expires_at", "grant-expired", "grant has expired", "warning")
                elif left <= EXPIRY_WARNING:
                    self.err(file, f"grants[{i}].expires_at", "grant-expiring", "expires within 14 days", "warning")

    def approval_file(self, file: str, doc: dict[str, Any]) -> None:  # noqa: PLR0912 - flat list of independent checks
        grants = self.pol.tenants.get(doc["tenant"], (None, None))[1]
        if grants is None:
            self.err(file, "tenant", "no-grants-file", f"no tenants/ file for tenant {doc['tenant']!r}")
        tenant = doc["tenant"]
        groups: dict[str, list[str]] = (grants or {}).get("groups", {})
        agents = {a["id"] for a in (grants or {}).get("principals", {}).get("agents", [])}
        roles = {h["id"]: h["roles"] for h in (grants or {}).get("principals", {}).get("humans", [])}
        conns = {c["id"]: c["domain"] for c in (grants or {}).get("connections", [])}

        limits = dict(SYSTEM_DEFAULTS)
        for key, value in doc.get("defaults", {}).items():
            if seconds(value) > SYSTEM_DEFAULTS[key]:
                self.err(file, f"defaults.{key}", "ttl-exceeds-default", f"{value} is longer than the system default")
            limits[key] = min(limits[key], seconds(value))

        rules = doc["rules"]
        self.unique(file, ((f"rules[{i}].id", r["id"]) for i, r in enumerate(rules)), "rule")
        for i, rule in enumerate(rules):
            match = rule["match"]
            for key in ("approval_ttl", "execution_window"):
                if key in rule and seconds(rule[key]) > limits[key]:
                    self.err(file, f"rules[{i}].{key}", "ttl-exceeds-default", f"{rule[key]} is longer than default")
            if grants is None:
                continue
            for j, r in enumerate(match.get("resources", [])):
                self.rid(file, f"rules[{i}].match.resources[{j}]", r, conns)
            for j, req in enumerate(match.get("requesters", [])):
                path = f"rules[{i}].match.requesters[{j}]"
                if req.startswith("group:"):
                    if req[6:] not in groups:
                        self.err(file, path, "dangling-requester", f"group {req[6:]!r} is not declared")
                elif not self.foreign(file, path, tenant, req) and req not in agents:
                    self.err(file, path, "dangling-requester", f"{req!r} is not a declared agent")
            for j, ap in enumerate(rule.get("approvers", [])):
                path = f"rules[{i}].approvers[{j}]"
                if ap.startswith("group:") and ap[6:] not in groups:
                    self.err(file, path, "dangling-approver", f"group {ap[6:]!r} is not declared")
                    continue
                for person in expand([ap], groups):
                    if person not in roles and not person.startswith("agent:"):
                        self.err(file, path, "dangling-approver", f"{person!r} is not a declared human")
                    elif "approver" not in roles.get(person, []):
                        self.err(file, path, "approver-role", f"{person!r} has no `approver` role")
            if rule["decision"] == "auto_approve":
                self.auto_approve(file, i, match)

        if grants is None:
            return
        # AP-01 p.4: approvers of require_approval rules that can match one intent are intersected.
        strict = [(i, r) for i, r in enumerate(rules) if r["decision"] == "require_approval" and "approvers" in r]
        for (_, a), (j, b) in combinations(strict, 2):
            if self.may_overlap(a["match"], b["match"], groups) and not (
                set(expand(a["approvers"], groups)) & set(expand(b["approvers"], groups))
            ):
                self.err(
                    file,
                    f"rules[{j}].approvers",
                    "approvers-empty-intersection",
                    f"no common approver with {a['id']!r}",
                )

    def auto_approve(self, file: str, i: int, match: dict[str, Any]) -> None:
        if "high_risk" in match.get("effect_classes", []):
            self.err(
                file,
                f"rules[{i}].match.effect_classes",
                "auto-approve-high-risk",
                "auto_approve for effect class high_risk (AP-02)",
            )
        for j, tool in enumerate(match.get("tools", [])):
            cls = self.classes.get(tool)
            if cls == "high_risk":
                self.err(file, f"rules[{i}].match.tools[{j}]", "auto-approve-high-risk", f"{tool} is high_risk (AP-02)")
            elif cls is None:
                self.err(file, f"rules[{i}].match.tools[{j}]", "unknown-tool", f"no manifest for {tool}", "warning")

    def may_overlap(self, a: dict[str, Any], b: dict[str, Any], groups: dict[str, list[str]]) -> bool:
        if "tools" in a and "tools" in b and not set(a["tools"]) & set(b["tools"]):
            return False
        if "effect_classes" in a and "effect_classes" in b and not set(a["effect_classes"]) & set(b["effect_classes"]):
            return False
        for x, y in ((a, b), (b, a)):
            if "tools" in x and "effect_classes" in y:
                known = [self.classes.get(t) for t in x["tools"]]
                if None not in known and not set(known) & set(y["effect_classes"]):
                    return False
        if "resources" in a and "resources" in b:
            if not any(rid_overlap(r, s) for r in a["resources"] for s in b["resources"]):
                return False
        if "requesters" in a and "requesters" in b:
            return bool(set(expand(a["requesters"], groups)) & set(expand(b["requesters"], groups)))
        return True

    def platform_file(self, file: str, doc: dict[str, Any]) -> None:
        self.unique(file, ((f"services[{i}].id", s["id"]) for i, s in enumerate(doc["services"])), "service")
        hooks = doc.get("webhooks", [])
        self.unique(file, ((f"webhooks[{i}].id", w["id"]) for i, w in enumerate(hooks)), "webhook")
        refs = [
            (f"services[{i}].tenants[{j}]", t)
            for i, s in enumerate(doc["services"])
            for j, t in enumerate(s["tenants"])
        ]
        refs += [(f"webhooks[{i}].tenant", w["tenant"]) for i, w in enumerate(hooks)]
        for i, svc in enumerate(doc["services"]):
            if "tools" in svc:
                self.err(file, f"services[{i}].tools", "ignored-tools", "build takes tools from manifests", "warning")
        for path, t in refs:
            if t != "*" and t not in self.pol.tenants:
                self.err(file, path, "unknown-tenant", f"no tenants/ file for {t!r}", "warning")


def lint(root: Path, manifests: Iterable[Path] | None = None, now: datetime | None = None) -> Policies:
    pol = load(root)
    load_manifests(pol, default_manifests() if manifests is None else manifests)
    linter = _Linter(pol, now or datetime.now(UTC))
    for file, doc in pol.tenants.values():
        linter.tenant_file(file, doc)
    for file, doc in pol.approval.values():
        linter.approval_file(file, doc)
    if pol.platform:
        linter.platform_file(*pol.platform)
    pol.findings += linter.out
    return pol
