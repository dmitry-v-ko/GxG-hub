"""EX-26 (AS-30a, AS-30, AS-17): `gxg_decide_session`, the decision of an intent without step-up in the session.
Allowed → decided with `step_up = false` and `decided_by = oidc:…`; an intent with step-up, another approver, a stale
version or args hash → refused, the intent unchanged; `gxg_ui` cannot call `gxg_decide` (the decider's, AS-30)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

HUMAN1 = "oidc:https://idp.example.invalid:1"
BETA_HUMAN = "oidc:https://idp.example.invalid:beta"
WIRE = "demo.wire.propose"  # high_risk: step-up by default (AP-01)


def new_intent(propose: Callable[..., Any], owner: psycopg.Connection[Any], **kw: Any) -> dict[str, Any]:
    created = propose(**kw).json()
    row = owner.execute(
        "SELECT version, canonical_args_hash, step_up_required FROM intents WHERE intent_id = %s",
        (created["intent_id"],),
    ).fetchone()
    assert row is not None
    return {"intent_id": created["intent_id"], "version": row[0], "args_hash": row[1], "step_up": row[2]}


def decide(pg: Any, i: dict[str, Any], who: str = HUMAN1, decision: str = "approve", **over: Any) -> None:
    args = {"version": i["version"], "args_hash": i["args_hash"], **over}
    with pg.connect("gxg_ui") as ui:
        ui.execute(
            "SELECT gxg_decide_session(%s, %s, %s, %s, %s)",
            (who, i["intent_id"], args["version"], args["args_hash"], decision),
        )


def state(owner: psycopg.Connection[Any], intent_id: str) -> tuple[str, int]:
    row = owner.execute("SELECT status, (SELECT count(*) FROM decisions WHERE intent_id = %s) FROM intents "
                        "WHERE intent_id = %s", (intent_id, intent_id)).fetchone()  # fmt: skip
    assert row is not None
    return str(row[0]), int(row[1])


def refused(sqlstate: str) -> Any:
    return pytest.raises(psycopg.Error, check=lambda e: e.sqlstate == sqlstate)


def test_an_intent_without_step_up_is_decided_in_the_session(pg: Any, propose: Any, owner: Any) -> None:
    i = new_intent(propose, owner)
    assert i["step_up"] is False
    decide(pg, i)
    assert state(owner, i["intent_id"]) == ("approved", 1)
    assert owner.execute("SELECT decided_by, step_up, webauthn_assertion FROM decisions").fetchall() == [
        (HUMAN1, False, None)
    ]


def test_reject_in_the_session(pg: Any, propose: Any, owner: Any) -> None:
    i = new_intent(propose, owner)
    decide(pg, i, decision="reject")
    assert state(owner, i["intent_id"]) == ("rejected", 1)


def test_an_intent_with_step_up_is_refused_and_unchanged(pg: Any, propose: Any, owner: Any) -> None:
    i = new_intent(propose, owner, tool=WIRE, args={"text": "wire", "n": 9})
    assert i["step_up"] is True
    for decision in ("approve", "reject"):
        with refused("GX403"):
            decide(pg, i, decision=decision)
    assert state(owner, i["intent_id"]) == ("pending_approval", 0)


def test_another_approver_is_refused(pg: Any, propose: Any, owner: Any) -> None:
    i = new_intent(propose, owner)
    with refused("GX403"):
        decide(pg, i, who=BETA_HUMAN)  # an approver of another tenant only
    with refused("GX403"):
        decide(pg, i, who="not-a-principal")
    assert state(owner, i["intent_id"]) == ("pending_approval", 0)


def test_stale_version_or_args_hash_is_refused(pg: Any, propose: Any, owner: Any) -> None:
    i = new_intent(propose, owner)
    with refused("GX409"):
        decide(pg, i, version=i["version"] + 1)
    with refused("GX409"):
        decide(pg, i, args_hash="0" * 64)
    assert state(owner, i["intent_id"]) == ("pending_approval", 0)


def test_the_same_approver_cannot_decide_twice(pg: Any, propose: Any, owner: Any) -> None:
    i = new_intent(propose, owner)
    decide(pg, i)
    with pytest.raises(psycopg.Error):
        decide(pg, i, decision="reject")


def test_the_ui_role_cannot_call_the_deciders_function_or_write_decisions(pg: Any, propose: Any, owner: Any) -> None:
    i = new_intent(propose, owner)
    with pg.connect("gxg_ui") as ui:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ui.execute("SELECT gxg_decide(%s, '\\x01', 1, null, '{}'::jsonb)", (b"\x00",))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ui.execute("INSERT INTO decisions (decision_id) VALUES (gen_random_uuid())")
    assert state(owner, i["intent_id"]) == ("pending_approval", 0)
