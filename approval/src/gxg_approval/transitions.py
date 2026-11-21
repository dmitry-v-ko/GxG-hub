"""The transition table T1-T18 + T12a (docs/specs/approval-service-v1.md (intent statuses and transitions)).

`rules()` is the single source of truth: `install_sql()` puts it into `transition_rules`, and every
transition function in the database refuses a move that is not a row of it (AS-03). A status without
outgoing rows is terminal (`terminal_at` is set on entry). Functions, not module constants, so mutation
testing reaches the table.
"""

from __future__ import annotations

import re

Rule = tuple[str, str, str, str, str]  # (tid, event, from_status, exec_state, to_status); "" = not applicable


def rules() -> tuple[Rule, ...]:
    """Every allowed move. A conditional target (T1, T2/T3, T12, T12a, T18) has one row per target."""
    return (
        ("T1", "propose", "", "", "pending_approval"),
        ("T1", "propose", "", "", "approved"),
        ("T2", "approve", "pending_approval", "", "approved"),
        ("T3", "approve", "pending_approval", "", "pending_approval"),
        ("T4", "reject", "pending_approval", "", "rejected"),
        ("T5", "expire_approval", "pending_approval", "", "expired"),
        ("T6", "cancel", "pending_approval", "", "cancelled"),
        ("T6", "cancel", "approved", "", "cancelled"),
        ("T7", "reserve", "approved", "", "executing"),
        ("T8", "expire_execution", "approved", "", "expired"),
        ("T9", "dispatch", "executing", "reserved", "executing"),
        ("T10", "reserve_lease_expired", "executing", "reserved", "approved"),
        ("T11", "complete_succeeded", "executing", "dispatching", "succeeded"),
        ("T12", "complete_failed_before_effect", "executing", "dispatching", "approved"),
        ("T12", "complete_failed_before_effect", "executing", "dispatching", "failed"),
        ("T12a", "complete_refused_before_dispatch", "executing", "reserved", "approved"),
        ("T12a", "complete_refused_before_dispatch", "executing", "reserved", "failed"),
        ("T13", "complete_outcome_unknown", "executing", "dispatching", "outcome_unknown"),
        ("T14", "dispatch_lease_expired", "executing", "dispatching", "outcome_unknown"),
        ("T15", "reconcile_executed", "outcome_unknown", "", "reconciled_succeeded"),
        ("T16", "reconcile_not_executed", "outcome_unknown", "", "reconciled_failed"),
        ("T17", "reconcile_unknown", "outcome_unknown", "", "outcome_unknown"),
        ("T18", "resolve_manual", "outcome_unknown", "", "reconciled_succeeded"),
        ("T18", "resolve_manual", "outcome_unknown", "", "reconciled_failed"),
    )


def install_sql() -> str:
    """Replace the installed table with `rules()` (migration; DB tests re-run it so mutants reach the DB)."""
    rows = rules()
    if not all(re.fullmatch(r"[A-Za-z0-9_]*", v) for row in rows for v in row):
        raise ValueError("transition rules must be plain identifiers")  # so no quoting is needed below
    values = ", ".join("(" + ", ".join(f"'{v}'" for v in row) + ")" for row in rows)
    return f"DELETE FROM transition_rules; INSERT INTO transition_rules VALUES {values}"  # noqa: S608
