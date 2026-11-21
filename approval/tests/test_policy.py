"""AC-M7-05 (EX-18): AP-01…AP-04 reference scenarios, and the edges of matching and merging."""

from __future__ import annotations

from typing import Any

import pytest

from gxg_approval.policy import Outcome, Request, duration_s, evaluate

BOT, SIGNALS = "agent:t-alpha:bot", "agent:t-alpha:signals"
H1, H2, H3 = (
    "oidc:https://idp.example.invalid:1",
    "oidc:https://idp.example.invalid:2",
    "oidc:https://idp.example.invalid:3",
)
GROUPS = {"bots": frozenset({BOT}), "finance": frozenset({H1, H2}), "ops": frozenset({H2, H3})}
MSG, WIRE, NOTE = "demo.message.propose", "demo.wire.propose", "demo.note.propose"
RID = "demo:demo-main/board/b1"


def req(tool: str = MSG, cls: str = "external_effect", rid: str = RID, requester: str = BOT) -> Request:
    return Request(tool, cls, rid, requester)


def rule(rid: str, decision: str, **kw: Any) -> dict[str, Any]:
    match = kw.pop("match", {"tools": [MSG, WIRE, NOTE]})
    return {"id": rid, "match": match, "decision": decision, **kw}


AUTO = rule("auto", "auto_approve", limits={"max_per_hour": 10, "max_per_day": 50})
UNUSED = lambda _rule: (0, 0)  # noqa: E731


def run(rules: list[dict[str, Any]], r: Request | None = None, used: Any = UNUSED, **policy: Any) -> Outcome:
    return evaluate({"tenant": "t-alpha", "schema_version": 1, "rules": rules, **policy}, GROUPS, r or req(), used)


def summary(o: Outcome) -> tuple[Any, ...]:
    return o.mode, o.required_approvals, o.step_up, o.rule_ids


# (name, rules, request, auto usage, expected (mode, required_approvals, step_up, rule_ids))
SCENARIOS: list[tuple[str, list[dict[str, Any]], Request, Any, tuple[Any, ...]]] = [
    ("1 deny + auto → deny", [rule("no", "deny"), AUTO], req(), UNUSED, (None, 0, False, ("auto", "no"))),
    (
        "2 require + auto → human",
        [rule("req", "require_approval"), AUTO],
        req(),
        UNUSED,
        ("human", 1, False, ("auto", "req")),
    ),
    (
        "3 two require, approvals 1 and 2 → 2, step_up OR",
        [rule("a", "require_approval", approvals=1), rule("b", "require_approval", approvals=2, step_up=True)],
        req(),
        UNUSED,
        ("human", 2, True, ("a", "b")),
    ),
    (
        "4 two require with ttl 4h and 2h → min",
        [
            rule("a", "require_approval", approval_ttl="4h"),
            rule("b", "require_approval", approval_ttl="2h", execution_window="30m"),
        ],
        req(),
        UNUSED,
        ("human", 1, False, ("a", "b")),
    ),
    ("5 auto within limits → auto", [AUTO], req(), UNUSED, ("auto", 0, False, ("auto",))),
    ("6 auto, hourly limit used up → human 1", [AUTO], req(), lambda _r: (10, 10), ("human", 1, False, ("auto",))),
    ("7 auto, daily limit used up → human 1", [AUTO], req(), lambda _r: (3, 50), ("human", 1, False, ("auto",))),
    ("8 auto for high_risk → ignored, step-up", [AUTO], req(WIRE, "high_risk"), UNUSED, ("human", 1, True, ("auto",))),
    (
        "9 no rules, write_reversible → not_required",
        [],
        req(NOTE, "write_reversible"),
        UNUSED,
        ("not_required", 0, False, ()),
    ),
    ("10 no rules, external_effect → 1 approver", [], req(), UNUSED, ("human", 1, False, ())),
    ("11 no rules, high_risk → 1 approver + step-up", [], req(WIRE, "high_risk"), UNUSED, ("human", 1, True, ())),
    (
        "12 auto for write_reversible → not_required (auto only for external_effect)",
        [AUTO],
        req(NOTE, "write_reversible"),
        UNUSED,
        ("not_required", 0, False, ("auto",)),
    ),
]


@pytest.mark.parametrize(("name", "rules", "request_", "used", "expected"), SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_reference_scenarios(
    name: str, rules: list[dict[str, Any]], request_: Request, used: Any, expected: tuple[Any, ...]
) -> None:
    assert summary(run(rules, request_, used)) == expected


def test_scenario_details() -> None:
    o = run(SCENARIOS[3][1])
    assert (o.approval_ttl_s, o.execution_window_s) == (2 * 3600, 30 * 60)
    assert run(SCENARIOS[0][1]).deny_reason == "no"
    high = run([AUTO], req(WIRE, "high_risk"))
    assert high.alerts and "high_risk" in high.alerts[0]  # AP-02: alert to the operator
    assert run([AUTO]).alerts == ()


def test_rule_order_does_not_matter() -> None:
    rules = [rule("b", "require_approval", approvals=2), rule("a", "require_approval", step_up=True), AUTO]
    assert run(rules) == run(list(reversed(rules)))


def test_every_auto_limit_counts() -> None:
    """AP-01.5: with several matching auto rules each limit is checked."""
    second = rule("auto2", "auto_approve", limits={"max_per_hour": 1})
    usage = {"auto": (0, 0), "auto2": (1, 1)}
    assert run([AUTO, second], used=lambda r: usage[r]).mode == "human"
    assert run([AUTO, second]).mode == "auto"


def test_limit_boundary() -> None:
    one = rule("one", "auto_approve", limits={"max_per_hour": 1})
    assert run([one], used=lambda _r: (0, 99)).mode == "auto"  # only the hourly limit is set
    assert run([one], used=lambda _r: (1, 1)).mode == "human"
    day = rule("day", "auto_approve", limits={"max_per_day": 2})
    assert run([day], used=lambda _r: (5, 1)).mode == "auto"
    assert run([day], used=lambda _r: (0, 2)).mode == "human"


def test_approvers_are_intersected_and_expanded() -> None:
    a = rule("a", "require_approval", approvers=["group:finance"])
    b = rule("b", "require_approval", approvers=["group:ops", H1])
    assert run([a, b]).approvers == (H1, H2)
    assert run([a]).approvers == (H1, H2)
    assert run([rule("c", "require_approval")]).approvers is None  # any approver of the tenant
    assert run([a, b, rule("c", "require_approval")]).approvers == (H1, H2)


def test_empty_approver_intersection_is_deny() -> None:
    a = rule("a", "require_approval", approvers=[H1])
    b = rule("b", "require_approval", approvers=[H3])
    o = run([a, b])
    assert o.mode is None and o.deny_reason == "empty approver intersection" and o.alerts
    assert run([rule("g", "require_approval", approvers=["group:missing"])]).mode is None


def test_defaults_can_only_shorten() -> None:
    assert (run([]).approval_ttl_s, run([]).execution_window_s) == (24 * 3600, 3600)
    short = run([], defaults={"approval_ttl": "2h", "execution_window": "10m"})
    assert (short.approval_ttl_s, short.execution_window_s) == (2 * 3600, 600)
    long = run([], defaults={"approval_ttl": "3d", "execution_window": "2h"})
    assert (long.approval_ttl_s, long.execution_window_s) == (24 * 3600, 3600)
    assert run([rule("a", "require_approval", approval_ttl="2d")]).approval_ttl_s == 24 * 3600


def test_no_policy_file_is_the_class_default() -> None:
    assert summary(evaluate(None, {}, req(), UNUSED)) == ("human", 1, False, ())
    assert summary(evaluate(None, {}, req(NOTE, "write_reversible"), UNUSED)) == ("not_required", 0, False, ())


@pytest.mark.parametrize(
    ("match", "request_", "hit"),
    [
        ({"tools": [MSG]}, req(), True),
        ({"tools": [WIRE]}, req(), False),
        ({"effect_classes": ["external_effect"]}, req(), True),
        ({"effect_classes": ["high_risk"]}, req(), False),
        ({"resources": [RID]}, req(), True),
        ({"resources": ["demo:demo-main/board/b2"]}, req(), False),
        ({"resources": ["demo:demo-main/board/*"]}, req(), True),
        ({"resources": ["demo:demo-main/chat/*"]}, req(), False),
        ({"resources": ["demo:demo-other/board/*"]}, req(), False),
        ({"resources": ["demo:demo-main/board/*"]}, req(rid="demo:demo-main/board/b1/x"), False),
        ({"requesters": [BOT]}, req(), True),
        ({"requesters": ["group:bots"]}, req(), True),
        ({"requesters": ["group:bots"]}, req(requester=SIGNALS), False),
        ({"requesters": ["group:missing"]}, req(), False),
        ({"requesters": [SIGNALS]}, req(), False),
        ({"tools": [MSG], "requesters": [SIGNALS]}, req(), False),  # AND across fields
        ({"tools": [WIRE, MSG], "resources": ["demo:x/board/1", RID]}, req(), True),  # OR inside a list
    ],
)
def test_match(match: dict[str, Any], request_: Request, hit: bool) -> None:
    o = run([rule("r", "require_approval", approvals=2, match=match)], request_)
    assert (o.rule_ids == ("r",)) == hit
    assert o.required_approvals == (2 if hit else 1)


def test_duration() -> None:
    assert [duration_s(v) for v in ("1m", "30m", "4h", "2d")] == [60, 1800, 14400, 172800]
