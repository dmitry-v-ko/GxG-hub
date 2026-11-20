"""Rights diff between two `policies/` trees (GR-09 p.3, GR-10, approval-policy «Lint в CI»).

Every change is classified `widening` (needs the owner's approval) or `narrowing`.
Where a change can go either way (a rewritten `match`), it is `widening`: fail closed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gxg_policy.lint import SYSTEM_DEFAULTS, expand, seconds
from gxg_policy.load import Policies, load, parse_ts

Entry = dict[str, Any]
Groups = dict[str, list[str]]
STRICTNESS = {"auto_approve": 0, "require_approval": 1, "deny": 2}
QUOTA_DEFAULTS = {"propose_per_requester_per_hour": 30, "pending_per_requester_max": 20, "pending_per_tenant_max": 100}
INF = float("inf")


class PolicyError(Exception):
    pass


def _load(root: Path) -> Policies:
    if not root.exists():  # e.g. the base commit has no policies/ yet
        return Policies(root)
    pol = load(root)
    if pol.errors:
        raise PolicyError("\n".join(map(str, pol.errors)))
    return pol


def _pairs(doc: dict[str, Any] | None) -> dict[tuple[str, str, str], str | None]:
    """(subject, action, resource) -> latest expiry (None = never expires)."""
    out: dict[tuple[str, str, str], str | None] = {}
    if doc is None:
        return out
    for g in doc["grants"]:
        exp = g.get("expires_at")
        for s in expand(g["subjects"], doc.get("groups", {})):
            for a in g["actions"]:
                for r in g["resources"]:
                    key = (s, a, r)
                    if key in out:
                        old = out[key]
                        exp_ = None if old is None or exp is None else max(old, exp, key=parse_ts)
                        out[key] = exp_
                    else:
                        out[key] = exp
    return out


def _expanded(rule: dict[str, Any], groups: Groups) -> dict[str, Any]:
    """Rule with `group:` refs in `match.requesters` and `approvers` replaced by members (compared as sets)."""
    rule = {**rule, "match": dict(rule["match"])}
    if "requesters" in rule["match"]:
        rule["match"]["requesters"] = sorted(expand(rule["match"]["requesters"], groups))
    if "approvers" in rule:
        rule["approvers"] = sorted(expand(rule["approvers"], groups))
    return rule


def _roles(doc: dict[str, Any] | None) -> set[tuple[str, str]]:
    humans = (doc or {}).get("principals", {}).get("humans", [])
    return {(h["id"], role) for h in humans for role in h["roles"]}


class _Diff:
    def __init__(self) -> None:
        self.widening: list[Entry] = []
        self.narrowing: list[Entry] = []

    def add(self, wider: bool, **entry: Any) -> None:
        (self.widening if wider else self.narrowing).append(entry)

    def number(self, wider_if_up: bool, base: float, head: float, **entry: Any) -> None:
        if base != head:
            entry.setdefault("change", "increased" if head > base else "decreased")
            self.add(
                (head > base) == wider_if_up,
                **entry,
                base=None if base == INF else base,
                head=None if head == INF else head,
            )

    def grants(self, tenant: str, base: dict[str, Any] | None, head: dict[str, Any] | None) -> None:
        b, h = _pairs(base), _pairs(head)
        for key in b.keys() | h.keys():
            s, a, r = key
            pair = {"tenant": tenant, "kind": "grant", "subject": s, "action": a, "resource": r}
            if key not in b:
                self.add(True, **pair, change="added", expires_at=h[key])
            elif key not in h:
                self.add(False, **pair, change="removed")
            elif b[key] != h[key]:
                bx, hx = (datetime.max.replace(tzinfo=UTC) if v is None else parse_ts(v) for v in (b[key], h[key]))
                self.add(
                    hx > bx,
                    **pair,
                    change="expiry_extended" if hx > bx else "expiry_shortened",
                    base=b[key],
                    head=h[key],
                )
        rb, rh = _roles(base), _roles(head)
        for p, role in rh - rb:
            self.add(True, tenant=tenant, kind="role", change="added", principal=p, role=role)
        for p, role in rb - rh:
            self.add(False, tenant=tenant, kind="role", change="removed", principal=p, role=role)

    def approval(
        self, tenant: str, base: dict[str, Any] | None, head: dict[str, Any] | None, groups: tuple[Groups, Groups]
    ) -> None:
        base, head = base or {"rules": []}, head or {"rules": []}
        for key, default in SYSTEM_DEFAULTS.items():
            bv, hv = (
                seconds(d[key]) if key in d else default for d in (base.get("defaults", {}), head.get("defaults", {}))
            )
            self.number(True, bv, hv, tenant=tenant, kind="approval_defaults", field=key)
        for key, default in QUOTA_DEFAULTS.items():
            bq, hq = (d.get(key, default) for d in (base.get("quotas", {}), head.get("quotas", {})))
            self.number(True, bq, hq, tenant=tenant, kind="quota", field=key)
        b = {r["id"]: r for r in base["rules"]}
        h = {r["id"]: r for r in head["rules"]}
        for rid in b.keys() | h.keys():
            e = {"tenant": tenant, "kind": "approval_rule", "rule": rid}
            if rid not in b:
                self.add(h[rid]["decision"] == "auto_approve", **e, change="added", decision=h[rid]["decision"])
            elif rid not in h:
                self.add(b[rid]["decision"] != "auto_approve", **e, change="removed", decision=b[rid]["decision"])
            else:
                self.rule(e, _expanded(b[rid], groups[0]), _expanded(h[rid], groups[1]))

    def rule(self, e: Entry, b: dict[str, Any], h: dict[str, Any]) -> None:
        if b["decision"] != h["decision"]:
            wider = STRICTNESS[h["decision"]] < STRICTNESS[b["decision"]]
            self.add(
                wider,
                **e,
                change="decision_relaxed" if wider else "decision_tightened",
                base=b["decision"],
                head=h["decision"],
            )
            return
        if h["decision"] == "auto_approve":
            # New requesters of an auto_approve rule get intents approved without a human.
            bm, hm = dict(b["match"]), dict(h["match"])
            self.sets(e, "requesters", bm.pop("requesters", ["*"]), hm.pop("requesters", ["*"]))
        else:
            bm, hm = b["match"], h["match"]
        if bm != hm:
            self.add(True, **e, change="match_changed")
        if h["decision"] == "auto_approve":
            for key in ("max_per_hour", "max_per_day"):
                self.number(True, b["limits"].get(key, INF), h["limits"].get(key, INF), **e, change="limits", field=key)
        if h["decision"] != "require_approval":
            return
        self.number(False, b.get("approvals", 1), h.get("approvals", 1), **e, change="approvals")
        if b.get("step_up", False) != h.get("step_up", False):
            self.add(
                not h.get("step_up", False),
                **e,
                change="step_up",
                base=b.get("step_up", False),
                head=h.get("step_up", False),
            )
        self.sets(e, "approvers", b.get("approvers", ["*"]), h.get("approvers", ["*"]))
        for key in SYSTEM_DEFAULTS:
            bv, hv = (seconds(r[key]) if key in r else INF for r in (b, h))
            self.number(True, bv, hv, **e, change="ttl", field=key)

    def sets(self, e: Entry, field: str, base: list[str], head: list[str]) -> None:
        """Added members widen, removed narrow. `"*"` (field absent) means everyone."""
        ba, ha = set(base), set(head)
        added, removed = (set() if "*" in ba else ha - ba), (set() if "*" in ha else ba - ha)
        if added:
            self.add(True, **e, change=f"{field}_added", **{field: sorted(added)})
        if removed:
            self.add(False, **e, change=f"{field}_removed", **{field: sorted(removed)})

    def platform(self, base: dict[str, Any] | None, head: dict[str, Any] | None) -> None:
        for kind, key in (("service", "services"), ("webhook", "webhooks")):
            b = {s["id"]: s for s in (base or {}).get(key, [])}
            h = {s["id"]: s for s in (head or {}).get(key, [])}
            for sid in b.keys() | h.keys():
                e = {"tenant": None, "kind": kind, "id": sid}
                if sid not in b:
                    self.add(True, **e, change="added")
                elif sid not in h:
                    self.add(False, **e, change="removed")
                elif kind == "webhook":
                    if b[sid] != h[sid]:
                        self.add(True, **e, change="changed")
                else:
                    for t in set(h[sid]["tenants"]) - set(b[sid]["tenants"]):
                        self.add(True, **e, change="tenant_added", service_tenant=t)
                    for t in set(b[sid]["tenants"]) - set(h[sid]["tenants"]):
                        self.add(False, **e, change="tenant_removed", service_tenant=t)
                    for f in ("kind", "public_key", "internal_url"):
                        if b[sid].get(f) != h[sid].get(f):
                            self.add(True, **e, change=f"{f}_changed")


def _key(e: Entry) -> str:
    return json.dumps(e, sort_keys=True, default=str)


def diff(base_root: Path, head_root: Path) -> dict[str, list[Entry]]:
    base, head = _load(base_root), _load(head_root)
    d = _Diff()
    for t in sorted(base.tenants.keys() | head.tenants.keys()):
        d.grants(t, base.tenants.get(t, ("", None))[1], head.tenants.get(t, ("", None))[1])
    for t in sorted(base.approval.keys() | head.approval.keys()):
        bg, hg = ((p.tenants.get(t, ("", None))[1] or {}).get("groups", {}) for p in (base, head))
        d.approval(t, base.approval.get(t, ("", None))[1], head.approval.get(t, ("", None))[1], (bg, hg))
    d.platform(base.platform and base.platform[1], head.platform and head.platform[1])
    return {"widening": sorted(d.widening, key=_key), "narrowing": sorted(d.narrowing, key=_key)}


MARKER = "<!-- gxg-policy-diff -->"


def _line(e: Entry) -> str:
    rest = ", ".join(f"{k}=`{v}`" for k, v in e.items() if k not in ("tenant", "kind", "change") and v is not None)
    return f"| `{e['tenant'] or 'platform'}` | {e['kind']} | {e['change']} | {rest} |"


def markdown(report: dict[str, list[Entry]]) -> str:
    out = [MARKER, "## Policy diff (gxg-policy)", ""]
    w, n = report["widening"], report["narrowing"]
    out.append(f"**widening: {len(w)}**, narrowing: {len(n)}.")
    if w:
        out.append("\nWidening needs the `widening-approved` label from a code owner (GR-10, AC-M2-04).")
    for title, items in (("Widening", w), ("Narrowing", n)):
        if items:
            out += ["", f"### {title}", "", "| tenant | kind | change | details |", "| --- | --- | --- | --- |"]
            out += [_line(e) for e in items]
    return "\n".join(out) + "\n"
