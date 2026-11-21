"""Approval policy engine, AP-01…AP-04 (docs/specs/approval-policy-v1.md).

Pure and deterministic: rule order in the file does not matter. The caller passes `auto_used(rule_id)`,
the number of intents this rule auto-approved in the last hour and day, counted under the T1 lock (AP-03).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

Mode = Literal["human", "auto", "not_required"]

SPEC_APPROVAL_TTL_S = 24 * 3600  # effects-and-approval-v1 «Сроки по умолчанию»
SPEC_EXECUTION_WINDOW_S = 3600
_UNITS = {"m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class Request:
    tool: str
    effect_class: str  # write_reversible | external_effect | high_risk
    resource: str
    requester: str


@dataclass(frozen=True)
class Outcome:
    mode: Mode | None  # None: deny, the intent is not stored
    required_approvals: int
    step_up: bool
    approvers: tuple[str, ...] | None  # None: any approver of the tenant
    approval_ttl_s: int
    execution_window_s: int
    rule_ids: tuple[str, ...]  # every matched rule, sorted (AP-04)
    deny_reason: str | None = None  # rule id or why, for the audit
    alerts: tuple[str, ...] = ()  # AP-02 and empty approver sets: for the operator


def duration_s(value: str) -> int:
    """`30m`, `4h`, `1d` (pattern of approval-policy.schema.json)."""
    return int(value[:-1]) * _UNITS[value[-1]]


def _members(ref: str, groups: Mapping[str, frozenset[str]]) -> frozenset[str]:
    if ref.startswith("group:"):
        return groups.get(ref[len("group:") :], frozenset())  # unknown group: nobody (lint catches it)
    return frozenset({ref})


def _resource_matches(pattern: str, rid: str) -> bool:
    if pattern.endswith("/*"):  # GR-04 type mask: every resource of the type in one connection
        prefix = pattern[:-1]
        return rid.startswith(prefix) and "/" not in rid[len(prefix) :]
    return rid == pattern


def _matches(rule: Mapping[str, Any], req: Request, groups: Mapping[str, frozenset[str]]) -> bool:
    match = rule["match"]
    if "tools" in match and req.tool not in match["tools"]:
        return False
    if "effect_classes" in match and req.effect_class not in match["effect_classes"]:
        return False
    if "resources" in match and not any(_resource_matches(p, req.resource) for p in match["resources"]):
        return False
    return "requesters" not in match or any(req.requester in _members(r, groups) for r in match["requesters"])


def _exhausted(rule: Mapping[str, Any], used: tuple[int, int]) -> bool:
    limits = rule.get("limits", {})
    hour, day = used
    return ("max_per_hour" in limits and hour >= limits["max_per_hour"]) or (
        "max_per_day" in limits and day >= limits["max_per_day"]
    )


def evaluate(  # noqa: PLR0911 (one return per AP-01 step)
    policy: Mapping[str, Any] | None,
    groups: Mapping[str, frozenset[str]],
    req: Request,
    auto_used: Callable[[str], tuple[int, int]],
) -> Outcome:
    """AP-01: base decision by class, then matched rules: deny > require_approval > auto_approve."""
    doc = policy or {}
    defaults = doc.get("defaults", {})
    ttl = min(SPEC_APPROVAL_TTL_S, duration_s(defaults.get("approval_ttl", "24h")))
    window = min(SPEC_EXECUTION_WINDOW_S, duration_s(defaults.get("execution_window", "1h")))
    matched = sorted((r for r in doc.get("rules", []) if _matches(r, req, groups)), key=lambda r: r["id"])
    rule_ids = tuple(r["id"] for r in matched)
    by = {d: [r for r in matched if r["decision"] == d] for d in ("deny", "require_approval", "auto_approve")}
    high_risk = req.effect_class == "high_risk"
    alerts: tuple[str, ...] = ()
    if high_risk and by["auto_approve"]:  # AP-02: CI rejects it; at run time it is ignored with an alert
        alerts = (f"auto_approve ignored for high_risk tool {req.tool}: {[r['id'] for r in by['auto_approve']]}",)

    def deny(reason: str, more: tuple[str, ...] = ()) -> Outcome:
        return Outcome(None, 0, False, None, ttl, window, rule_ids, reason, alerts + more)

    def human(approvals: int, step_up: bool, approvers: tuple[str, ...] | None = None) -> Outcome:
        return Outcome("human", approvals, step_up, approvers, ttl, window, rule_ids, None, alerts)

    if by["deny"]:
        return deny(by["deny"][0]["id"])
    if by["require_approval"]:
        rules = by["require_approval"]
        allowed: frozenset[str] | None = None
        for r in rules:
            if "approvers" in r:
                people = frozenset().union(*(_members(a, groups) for a in r["approvers"]))
                allowed = people if allowed is None else allowed & people
        if allowed is not None and not allowed:
            return deny("empty approver intersection", (f"rules {list(rule_ids)}: no approver satisfies all",))
        ttl = min([ttl] + [duration_s(r["approval_ttl"]) for r in rules if "approval_ttl" in r])
        window = min([window] + [duration_s(r["execution_window"]) for r in rules if "execution_window" in r])
        return human(
            max([1] + [r.get("approvals", 1) for r in rules]),
            high_risk or any(r.get("step_up", False) for r in rules),
            None if allowed is None else tuple(sorted(allowed)),
        )
    if by["auto_approve"] and req.effect_class == "external_effect":
        if any(_exhausted(r, auto_used(r["id"])) for r in by["auto_approve"]):
            return human(1, False)  # AP-01.6: exhausted limit → a human, not a refusal
        return Outcome("auto", 0, False, None, ttl, window, rule_ids, None, alerts)
    if req.effect_class == "write_reversible":
        return Outcome("not_required", 0, False, None, ttl, window, rule_ids, None, alerts)
    return human(1, high_risk)
