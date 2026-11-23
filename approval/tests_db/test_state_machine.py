"""AC-M7-02: every (intent status, event) pair of T1-T18 + T12a, through each role's own functions.

The expected table below is written from the spec (approval-service-v1.md (intent statuses and transitions)),
independently of gxg_approval.transitions. `executing` is split by the live execution's state, because
T9-T14 depend on it. A pair outside the table must return `conflict` and leave the intent, its executions
and the audit log unchanged; an execution-scoped event on an intent that never had an execution has
nothing to address and returns `not_found`, also without any change.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest

from gxg_approval.db import mapped_errors
from gxg_connector_kit.errors import GxgError

STATES = (
    "pending_approval",
    "approved",
    "executing/reserved",
    "executing/dispatching",
    "succeeded",
    "failed",
    "outcome_unknown",
    "reconciled_succeeded",
    "reconciled_failed",
    "rejected",
    "expired",
    "cancelled",
)
EXPECTED: dict[str, dict[str, set[str]]] = {
    "propose": {},  # T1 only creates; on an existing row it is an idempotent repeat (see test_propose_on_existing)
    "approve": {"pending_approval": {"approved", "pending_approval"}},  # T2 / T3
    "reject": {"pending_approval": {"rejected"}},  # T4
    "expire_approval": {"pending_approval": {"expired"}},  # T5
    "cancel": {"pending_approval": {"cancelled"}, "approved": {"cancelled"}},  # T6
    "reserve": {"approved": {"executing"}},  # T7
    "expire_execution": {"approved": {"expired"}},  # T8
    "dispatch": {"executing/reserved": {"executing"}},  # T9
    "reserve_lease_expired": {"executing/reserved": {"approved"}},  # T10
    "complete_succeeded": {"executing/dispatching": {"succeeded"}},  # T11
    "complete_failed_before_effect": {"executing/dispatching": {"approved", "failed"}},  # T12
    "complete_refused_before_dispatch": {"executing/reserved": {"approved", "failed"}},  # T12a
    "complete_rate_limited": {"executing/reserved": {"approved"}},  # T12b (AS-12a, M10)
    "complete_outcome_unknown": {"executing/dispatching": {"outcome_unknown"}},  # T13
    "dispatch_lease_expired": {"executing/dispatching": {"outcome_unknown"}},  # T14
    "reconcile_executed": {"outcome_unknown": {"reconciled_succeeded"}},  # T15
    "reconcile_not_executed": {"outcome_unknown": {"reconciled_failed"}},  # T16
    "reconcile_unknown": {"outcome_unknown": {"outcome_unknown"}},  # T17
    "resolve_manual": {"outcome_unknown": {"reconciled_succeeded", "reconciled_failed"}},  # T18
}
EXEC_EVENTS = {
    "dispatch",
    "reserve_lease_expired",
    "complete_succeeded",
    "complete_failed_before_effect",
    "complete_refused_before_dispatch",
    "complete_rate_limited",
    "complete_outcome_unknown",
    "dispatch_lease_expired",
}
HUMAN, HUMAN2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"
BOT = "agent:t-alpha:bot"


@dataclass
class Machine:
    """Calls each transition as the role that owns it (AS-27)."""

    owner: psycopg.Connection[Any]
    roles: dict[str, psycopg.Connection[Any]]
    propose: Callable[..., Any]
    decide_db: Callable[..., None]

    def call(self, role: str, sql: str, *args: Any) -> Any:
        with mapped_errors():
            row = self.roles[role].execute(sql, args).fetchone()
        return row[0] if row else None

    def row(self, intent: str) -> dict[str, Any]:
        cur = self.owner.cursor(row_factory=psycopg.rows.dict_row)
        found = cur.execute("SELECT * FROM intents WHERE intent_id = %s", (intent,)).fetchone()
        assert found is not None
        return found

    def execution(self, intent: str) -> str | None:
        row = self.owner.execute(
            "SELECT execution_id FROM executions WHERE intent_id = %s ORDER BY attempt DESC LIMIT 1", (intent,)
        ).fetchone()
        return str(row[0]) if row else None

    def snapshot(self, intent: str) -> tuple[Any, ...]:
        execs = self.owner.execute(
            "SELECT execution_id, state, completed_at FROM executions WHERE intent_id = %s ORDER BY attempt", (intent,)
        ).fetchall()
        audit = self.owner.execute("SELECT count(*) FROM audit_events").fetchone()
        decisions = self.owner.execute("SELECT count(*) FROM decisions").fetchone()
        r = self.row(intent)
        return r["status"], r["version"], r["attempts"], execs, audit, decisions

    # --- events -------------------------------------------------------------------------------------
    def decide(self, i: str, who: str, decision: str, version: int, args_hash: str, comment: str | None = None) -> None:
        """Through the decider's functions (AS-30): the only path to gxg_decide."""
        with mapped_errors():
            self.decide_db(i, who, decision, version, args_hash, comment)

    def approve(self, i: str, who: str = HUMAN) -> None:
        r = self.row(i)
        self.decide(i, who, "approve", r["version"], r["canonical_args_hash"])

    def reject(self, i: str) -> None:
        r = self.row(i)
        self.decide(i, HUMAN, "reject", r["version"], r["canonical_args_hash"], "no")

    def cancel(self, i: str) -> None:
        self.call("api", "SELECT gxg_cancel(%s, 't-alpha', %s, %s)", i, BOT, self.row(i)["version"])

    def reserve(self, i: str) -> None:
        self.call("executor", "SELECT gxg_reserve(%s, 'executor-1')", i)

    def dispatch(self, i: str) -> None:
        self.call("api", "SELECT gxg_dispatch(%s, %s, 'demo', 30)", self.execution(i) or uuid.uuid4(), i)

    def complete(self, i: str, outcome: str) -> None:
        self.call(
            "executor",
            "SELECT gxg_complete(%s, 'executor-1', %s, 'op-1', 'rcpt-1', null)",
            self.execution(i) or uuid.uuid4(),
            outcome,
        )

    def reconcile(self, i: str, result: str) -> None:
        self.call("executor", "SELECT gxg_reconcile(%s, 'executor-1', %s, 300)", i, result)

    def resolve(self, i: str, outcome: str = "executed") -> None:
        self.call(
            "ui",
            "SELECT gxg_resolve_manual(%s, %s, %s, %s, 'checked with the provider')",
            i,
            HUMAN,
            outcome,
            self.row(i)["version"],
        )

    def expire_approval(self, i: str) -> None:
        self.owner.execute("UPDATE intents SET expires_at = now() - interval '1 second' WHERE intent_id = %s", (i,))
        self.call("sweeper", "SELECT gxg_expire_approval(%s)", i)

    def expire_execution(self, i: str) -> None:
        self.owner.execute(
            "UPDATE intents SET execution_deadline = now() - interval '1 second' WHERE intent_id = %s", (i,)
        )
        self.call("sweeper", "SELECT gxg_expire_execution(%s)", i)

    def lease_expired(self, i: str, column: str, function: str) -> None:
        self.owner.execute(
            f"UPDATE executions SET {column} = now() - interval '1 second' WHERE intent_id = %s",
            (i,),
        )
        self.call("sweeper", f"SELECT {function}(%s)", self.execution(i) or uuid.uuid4())

    def apply(self, event: str, i: str) -> None:
        actions: dict[str, Callable[[], None]] = {
            "approve": lambda: self.approve(i),
            "reject": lambda: self.reject(i),
            "expire_approval": lambda: self.expire_approval(i),
            "cancel": lambda: self.cancel(i),
            "reserve": lambda: self.reserve(i),
            "expire_execution": lambda: self.expire_execution(i),
            "dispatch": lambda: self.dispatch(i),
            "reserve_lease_expired": lambda: self.lease_expired(i, "reserve_lease_until", "gxg_release_reservation"),
            "complete_succeeded": lambda: self.complete(i, "succeeded"),
            "complete_failed_before_effect": lambda: self.complete(i, "failed_before_effect"),
            "complete_refused_before_dispatch": lambda: self.complete(i, "refused_before_dispatch"),
            "complete_outcome_unknown": lambda: self.complete(i, "outcome_unknown"),
            "complete_rate_limited": lambda: self.call(
                "executor", "SELECT gxg_defer(%s, 'executor-1', 30)", self.execution(i) or uuid.uuid4()
            ),
            "dispatch_lease_expired": lambda: self.lease_expired(i, "dispatch_lease_until", "gxg_expire_dispatch"),
            "reconcile_executed": lambda: self.reconcile(i, "executed"),
            "reconcile_not_executed": lambda: self.reconcile(i, "not_executed"),
            "reconcile_unknown": lambda: self.reconcile(i, "unknown"),
            "resolve_manual": lambda: self.resolve(i),
        }
        actions[event]()

    def make(self, state: str, *, max_attempts: int = 3, approvals: int = 1) -> tuple[str, str]:
        """Drive a new intent into `state` through real transitions. Returns (intent_id, idempotency_key)."""
        key = f"sm-{uuid.uuid4()}"
        r = self.propose(key=key)
        assert r.status_code == 201, r.text
        i = r.json()["intent_id"]
        self.owner.execute(
            "UPDATE intents SET max_attempts = %s, required_approvals = %s WHERE intent_id = %s",
            (max_attempts, approvals, i),
        )
        path = {
            "pending_approval": [],
            "approved": ["approve"],
            "executing/reserved": ["approve", "reserve"],
            "executing/dispatching": ["approve", "reserve", "dispatch"],
            "succeeded": ["approve", "reserve", "dispatch", "complete_succeeded"],
            "failed": ["approve", "reserve", "dispatch", "complete_failed_before_effect"],
            "outcome_unknown": ["approve", "reserve", "dispatch", "complete_outcome_unknown"],
            "reconciled_succeeded": [
                "approve",
                "reserve",
                "dispatch",
                "complete_outcome_unknown",
                "reconcile_executed",
            ],
            "reconciled_failed": [
                "approve",
                "reserve",
                "dispatch",
                "complete_outcome_unknown",
                "reconcile_not_executed",
            ],
            "rejected": ["reject"],
            "expired": ["expire_approval"],
            "cancelled": ["cancel"],
        }[state]
        if state == "failed":
            self.owner.execute("UPDATE intents SET max_attempts = 1 WHERE intent_id = %s", (i,))
        for event in path:
            self.apply(event, i)
        assert self.row(i)["status"] == state.split("/", maxsplit=1)[0]
        return i, key


@pytest.fixture
def machine(
    pg: Any, owner: psycopg.Connection[Any], propose: Callable[..., Any], decide_db: Callable[..., None]
) -> Iterator[Machine]:
    roles = {r: pg.connect(f"gxg_{r}") for r in ("api", "ui", "executor", "sweeper")}
    try:
        yield Machine(owner, roles, propose, decide_db)
    finally:
        for conn in roles.values():
            conn.close()


@pytest.mark.parametrize("event", sorted(EXPECTED))
@pytest.mark.parametrize("state", STATES)
def test_every_status_event_pair(machine: Machine, state: str, event: str) -> None:
    intent, key = machine.make(state)
    if event == "propose":
        before = machine.snapshot(intent)
        r = machine.propose(key=key, args={"text": "other parameters"})
        assert r.status_code == 409 and r.json()["code"] == "conflict"
        assert machine.snapshot(intent) == before
        return
    targets = EXPECTED[event].get(state)
    before = machine.snapshot(intent)
    audit_before = before[4][0]
    if targets is None:
        with pytest.raises(GxgError) as err:
            machine.apply(event, intent)
        no_execution = event in EXEC_EVENTS and machine.execution(intent) is None
        assert err.value.code == ("not_found" if no_execution else "conflict"), err.value.message
        assert machine.snapshot(intent) == before
        return
    machine.apply(event, intent)
    after = machine.row(intent)
    assert after["status"] in targets
    assert after["version"] == before[1] + 1  # AS-01
    last = machine.owner.execute(
        "SELECT action, from_status, to_status, count(*) OVER () FROM audit_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert last == (event, state.split("/", maxsplit=1)[0], after["status"], audit_before + 1)


@pytest.mark.parametrize("state", STATES)
def test_propose_on_existing_with_same_hash_is_a_no_op(machine: Machine, state: str) -> None:
    intent, key = machine.make(state)
    before = machine.snapshot(intent)
    r = machine.propose(key=key)
    assert (
        r.status_code == 200
        and r.json()["intent_id"] == intent
        and r.json()["status"] == state.split("/", maxsplit=1)[0]
    )
    assert machine.snapshot(intent) == before


def test_t1_targets(machine: Machine, world: Any) -> None:
    """T1: pending_approval (human) or approved (auto / not_required)."""
    assert machine.propose().json()["status"] == "pending_approval"
    assert machine.propose(tool="demo.note.propose").json()["status"] == "approved"


def test_t2_needs_the_quorum_t3_records_the_vote(machine: Machine) -> None:
    intent, _ = machine.make("pending_approval", approvals=2)
    machine.approve(intent, HUMAN)
    assert machine.row(intent)["status"] == "pending_approval"  # T3
    with pytest.raises(GxgError) as err:
        machine.approve(intent, HUMAN)  # one vote per human
    assert err.value.code == "conflict"
    machine.approve(intent, HUMAN2)
    r = machine.row(intent)
    assert r["status"] == "approved" and r["version"] == 3  # T2
    assert r["approved_at"] is not None
    assert (r["execution_deadline"] - r["approved_at"]).total_seconds() == r["execution_window_s"]
    topics = machine.owner.execute("SELECT topic FROM outbox WHERE intent_id = %s ORDER BY id", (intent,)).fetchall()
    # M10: a webhook event per status change, from the trigger: T1 (pending_approval), then T2 (approved)
    assert topics == [("webhook",), ("notify",), ("webhook",), ("execute",)]
    rejected, _ = machine.make("pending_approval", approvals=2)
    machine.approve(rejected, HUMAN)
    rr = machine.row(rejected)
    machine.decide(rejected, HUMAN2, "reject", rr["version"], rr["canonical_args_hash"], "no")
    assert machine.row(rejected)["status"] == "rejected"  # T4 by the second approver
    assert machine.call("api", "SELECT gxg_received_approvals(%s)", rejected) == 1  # a rejection is no approval


def test_t2_deadline_is_the_window_frozen_at_t1(machine: Machine, world: Any) -> None:
    """M7-final #3 (AP-04): T2 uses the intent's own window (10 min here), not 1 h and not today's policy."""
    rule = {"id": "short", "match": {"tools": ["demo.message.propose"]}, "decision": "require_approval"}
    world.set_policy("t-alpha", [{**rule, "execution_window": "10m"}])
    intent, _ = machine.make("pending_approval")
    world.set_policy("t-alpha", [{**rule, "execution_window": "20m"}])  # a later policy does not apply (AP-04)
    machine.approve(intent)
    r = machine.row(intent)
    assert r["execution_window_s"] == 600
    assert (r["execution_deadline"] - r["approved_at"]).total_seconds() == 600


def test_t2_refuses_what_the_ui_must_never_get_through(machine: Machine) -> None:
    intent, _ = machine.make("pending_approval")
    r = machine.row(intent)
    cases = [
        ("agent:t-alpha:bot", r["version"], r["canonical_args_hash"], "permission_denied"),  # requester / machine
        ("agent:t-alpha:other", r["version"], r["canonical_args_hash"], "permission_denied"),  # machine, not requester
        (HUMAN, r["version"] + 1, r["canonical_args_hash"], "conflict"),  # stale screen
        (HUMAN, r["version"], "0" * 64, "conflict"),  # changed args
    ]
    for who, version, args_hash, code in cases:
        with pytest.raises(GxgError) as err:
            machine.decide(intent, who, "approve", version, args_hash)
        assert err.value.code == code, who
    machine.owner.execute(
        "UPDATE intents SET requester_principal = %s, allowed_approvers = %s, step_up_required = true "
        "WHERE intent_id = %s",
        (HUMAN, [HUMAN, HUMAN2], intent),
    )
    for who in (HUMAN, "oidc:https://idp.example.invalid:3"):  # the requester; a human outside the frozen set
        with pytest.raises(GxgError) as err:
            machine.decide(intent, who, "approve", 1, r["canonical_args_hash"])
        assert err.value.code == "permission_denied", who
    machine.decide(intent, HUMAN2, "approve", 1, r["canonical_args_hash"])  # every decision is step-up (AS-30)
    assert machine.row(intent)["status"] == "approved"
    expired, _ = machine.make("pending_approval")
    machine.owner.execute("UPDATE intents SET expires_at = now() WHERE intent_id = %s", (expired,))
    with pytest.raises(GxgError) as err:
        machine.approve(expired)
    assert err.value.code == "conflict"


def test_t5_t8_t10_t14_refuse_before_their_deadline(machine: Machine) -> None:
    cases = [
        ("pending_approval", "SELECT gxg_expire_approval(%s)", False),
        ("approved", "SELECT gxg_expire_execution(%s)", False),
        ("executing/reserved", "SELECT gxg_release_reservation(%s)", True),
        ("executing/dispatching", "SELECT gxg_expire_dispatch(%s)", True),
    ]
    for state, sql, by_execution in cases:
        intent, _ = machine.make(state)
        before = machine.snapshot(intent)
        with pytest.raises(GxgError) as err:
            machine.call("sweeper", sql, machine.execution(intent) if by_execution else intent)
        assert err.value.code == "conflict" and machine.snapshot(intent) == before, state


def test_t7_t9_side_effects_and_guards(machine: Machine) -> None:
    intent, _ = machine.make("approved")
    machine.reserve(intent)
    e = machine.owner.execute(
        "SELECT state, attempt, reserve_lease_until - reserved_at FROM executions WHERE intent_id = %s", (intent,)
    ).fetchone()
    assert e is not None and (e[0], e[1], e[2].total_seconds()) == ("reserved", 1, 60)
    with pytest.raises(GxgError) as err:  # T9 only for the connector of the intent
        machine.call("api", "SELECT gxg_dispatch(%s, %s, 'rogue', 30)", machine.execution(intent), intent)
    assert err.value.code == "not_found"
    other, _ = machine.make("approved")
    with pytest.raises(GxgError) as err:  # token `sub` is another intent of the same connector
        machine.call("api", "SELECT gxg_dispatch(%s, %s, 'demo', 30)", machine.execution(intent), other)
    assert err.value.code == "not_found"
    deadline = machine.call("api", "SELECT gxg_dispatch(%s, %s, 'demo', 30)", machine.execution(intent), intent)
    e = machine.owner.execute(
        "SELECT state, dispatch_lease_until - dispatched_at FROM executions WHERE intent_id = %s", (intent,)
    ).fetchone()
    assert e is not None and (e[0], e[1].total_seconds()) == ("dispatching", 60)
    assert machine.row(intent)["attempts"] == 1 and deadline is not None
    with pytest.raises(GxgError) as err:  # the CAS: a second dispatch of the same execution
        machine.call("api", "SELECT gxg_dispatch(%s, %s, 'demo', 30)", machine.execution(intent), intent)
    assert err.value.code == "conflict"


def test_t7_t9_kill_switch_and_limits(machine: Machine) -> None:
    """AS-05 inside T7 and T9; T7 needs attempts < max_attempts and the execution window."""
    intent, _ = machine.make("approved")
    machine.owner.execute("INSERT INTO switches VALUES ('tenant:t-alpha', true, 'incident', 'op', now())")
    with pytest.raises(GxgError) as err:
        machine.reserve(intent)
    assert err.value.code == "effect_disabled"
    machine.owner.execute("UPDATE switches SET blocked = false")  # cleared, the row stays (audit of the switch)
    machine.reserve(intent)
    machine.owner.execute("INSERT INTO switches VALUES ('tool:t-alpha:demo.message.propose', true, 'x', 'op', now())")
    with pytest.raises(GxgError) as err:
        machine.dispatch(intent)
    assert err.value.code == "effect_disabled"
    machine.owner.execute("DELETE FROM switches")
    machine.owner.execute("UPDATE executions SET reserve_lease_until = now() WHERE intent_id = %s", (intent,))
    with pytest.raises(GxgError) as err:
        machine.dispatch(intent)  # lease over
    assert err.value.code == "conflict"
    other, _ = machine.make("approved")
    machine.owner.execute("UPDATE intents SET attempts = 3 WHERE intent_id = %s", (other,))
    with pytest.raises(GxgError) as err:
        machine.reserve(other)
    assert err.value.code == "conflict"
    late, _ = machine.make("approved")
    machine.owner.execute("UPDATE intents SET execution_deadline = now() WHERE intent_id = %s", (late,))
    with pytest.raises(GxgError) as err:
        machine.reserve(late)
    assert err.value.code == "conflict"


def test_t10_releases_without_counting_and_allows_a_new_attempt(machine: Machine) -> None:
    intent, _ = machine.make("executing/reserved")
    machine.apply("reserve_lease_expired", intent)
    assert machine.row(intent)["attempts"] == 0
    assert machine.owner.execute("SELECT state FROM executions").fetchall() == [("released",)]
    machine.reserve(intent)
    assert [r[0] for r in machine.owner.execute("SELECT attempt FROM executions ORDER BY attempt")] == [1, 2]


def test_t12_retries_until_max_attempts_then_fails(machine: Machine) -> None:
    intent, _ = machine.make("approved", max_attempts=2)
    for expected in ("approved", "failed"):
        machine.reserve(intent)
        machine.dispatch(intent)
        machine.complete(intent, "failed_before_effect")
        assert machine.row(intent)["status"] == expected
    assert machine.row(intent)["terminal_at"] is not None
    late, _ = machine.make("executing/dispatching")
    machine.owner.execute("UPDATE intents SET execution_deadline = now() WHERE intent_id = %s", (late,))
    machine.complete(late, "failed_before_effect")
    assert machine.row(late)["status"] == "failed"  # out of the window


def test_t12a_counts_the_attempt(machine: Machine) -> None:
    """AS-12: a refusal of `execute` before dispatch is counted, so it ends in `failed`, not a loop."""
    intent, _ = machine.make("approved", max_attempts=2)
    machine.reserve(intent)
    machine.complete(intent, "refused_before_dispatch")
    r = machine.row(intent)
    assert (r["status"], r["attempts"]) == ("approved", 1)
    machine.reserve(intent)
    machine.complete(intent, "refused_before_dispatch")
    r = machine.row(intent)
    assert (r["status"], r["attempts"]) == ("failed", 2)
    states = machine.owner.execute("SELECT state FROM executions ORDER BY attempt").fetchall()
    assert states == [("failed_before_effect",), ("failed_before_effect",)]


def test_t13_t17_reconcile_outbox(machine: Machine) -> None:
    intent, _ = machine.make("outcome_unknown")
    machine.reconcile(intent, "unknown")
    rows = machine.owner.execute(
        "SELECT topic, round(extract(epoch FROM available_at - created_at)) FROM outbox WHERE intent_id = %s "
        "AND topic = 'reconcile' ORDER BY id",
        (intent,),
    ).fetchall()
    assert rows == [("reconcile", 0), ("reconcile", 300)]
    machine.call("executor", "SELECT gxg_reconcile(%s, 'executor-1', 'unknown', null)", intent)  # stop: manual review
    assert machine.owner.execute("SELECT count(*) FROM outbox WHERE topic = 'reconcile'").fetchone() == (2,)


def test_t15_t16_t18_mark_the_execution(machine: Machine) -> None:
    cases: list[tuple[Callable[[str], None], str, str]] = [
        (lambda i: machine.reconcile(i, "executed"), "reconciled_succeeded", "reconciled_executed"),
        (lambda i: machine.reconcile(i, "not_executed"), "reconciled_failed", "reconciled_not_executed"),
        (lambda i: machine.resolve(i, "executed"), "reconciled_succeeded", "reconciled_executed"),
        (lambda i: machine.resolve(i, "not_executed"), "reconciled_failed", "reconciled_not_executed"),
    ]
    for action, outcome, state in cases:
        intent, _ = machine.make("outcome_unknown")
        action(intent)
        assert machine.row(intent)["status"] == outcome
        assert machine.owner.execute("SELECT state FROM executions WHERE intent_id = %s", (intent,)).fetchone() == (
            state,
        )


def test_t18_needs_a_reason(machine: Machine) -> None:
    intent, _ = machine.make("outcome_unknown")
    for reason in (None, "", "too short"):
        with pytest.raises(GxgError) as err:
            machine.call(
                "ui",
                "SELECT gxg_resolve_manual(%s, %s, 'executed', %s, %s)",
                intent,
                HUMAN,
                machine.row(intent)["version"],
                reason,
            )
        assert err.value.code == "invalid_input"
    with pytest.raises(GxgError) as err:
        machine.call(
            "ui", "SELECT gxg_resolve_manual(%s, %s, 'executed', 1, 'checked with the provider')", intent, HUMAN
        )
    assert err.value.code == "conflict"  # stale version


def test_unknown_outcomes_are_invalid_input(machine: Machine) -> None:
    intent, _ = machine.make("executing/dispatching")
    for sql, args in (
        ("SELECT gxg_complete(%s, 'x', 'maybe', null, null, null)", (machine.execution(intent),)),
        ("SELECT gxg_reconcile(%s, 'x', 'maybe', null)", (intent,)),
    ):
        with pytest.raises(GxgError) as err:
            machine.call("executor", sql, *args)
        assert err.value.code == "invalid_input"
    with pytest.raises(GxgError) as err:
        machine.decide(intent, HUMAN, "maybe", 1, "0" * 64)
    assert err.value.code == "invalid_input"


def test_terminal_statuses_get_terminal_at(machine: Machine) -> None:
    for state in STATES:
        intent, _ = machine.make(state)
        terminal = state in {
            "succeeded",
            "failed",
            "reconciled_succeeded",
            "reconciled_failed",
            "rejected",
            "expired",
            "cancelled",
        }
        assert (machine.row(intent)["terminal_at"] is not None) == terminal, state


def test_t1_insert_guard(machine: Machine) -> None:
    """gxg_api may INSERT intents, but only rows T1 allows (no `succeeded`, no human intent born approved).

    The trigger fires for every inserter, so a copy of a valid row inserted by the owner shows it.
    """
    intent, _ = machine.make("pending_approval")
    owner = machine.owner
    owner.execute("CREATE TEMP TABLE c AS SELECT * FROM intents WITH NO DATA")
    for change, code in (
        ("status = 'succeeded'", "GX409"),
        ("status = 'approved'", "GX400"),
        ("version = 2", "GX400"),
        ("attempts = 1", "GX400"),
        ("args_ciphertext = null", "GX400"),
        ("preview_ciphertext = null", "GX400"),
        ("status = 'approved', approval_mode = 'not_required', required_approvals = 0", "GX400"),  # no deadline
        ("required_approvals = 0", "GX400"),
        ("terminal_at = now()", "GX400"),
        ("approval_mode = 'auto', required_approvals = 0", "GX400"),  # pending, but not a human intent
        ("status = 'approved', approved_at = now(), execution_deadline = now()", "GX400"),  # human, born approved
    ):
        owner.execute("DELETE FROM c")
        owner.execute("INSERT INTO c SELECT * FROM intents WHERE intent_id = %s", (intent,))
        owner.execute(
            f"UPDATE c SET intent_id = gen_random_uuid(), idempotency_key = 'k-' || gen_random_uuid(), {change}"
        )
        with pytest.raises(psycopg.Error) as err:
            owner.execute("INSERT INTO intents SELECT * FROM c")
        assert err.value.sqlstate == code, change
    owner.execute("DELETE FROM c")
    owner.execute("INSERT INTO c SELECT * FROM intents WHERE intent_id = %s", (intent,))
    owner.execute("UPDATE c SET intent_id = gen_random_uuid(), idempotency_key = 'k-' || gen_random_uuid()")
    owner.execute("INSERT INTO intents SELECT * FROM c")  # the unchanged copy is a valid T1 row
