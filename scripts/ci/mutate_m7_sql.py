#!/usr/bin/env python3
"""DoD-4 for M7, SQL part: each mutation of a key SQL condition must turn `approval/tests_db` red.

mutmut mutates Python only: it leaves the PL/pgSQL of the migration and the SQL string constants of `intents.py`
alone, and no maintained mutation tool exists for application SQL (reports/plans/M7-cycle-2.md, DoD-6; the
cycle-1 critic confirmed). So this hand list applies one textual mutation at a time, runs the DB tests, restores
the file. Not in the list: locking clauses (FOR UPDATE, SKIP LOCKED, advisory lock other than T1) — races are not
one-line conditions a deterministic test can pin down; T1's lock is in (the parallel AC-M7-04/06 tests kill it).
Needs PostgreSQL: GXG_TEST_PG_SOCKET_DIR (scripts/ci/mutmut.sh starts one) or Docker (tests_db/conftest.py).
Run from the repository root: `uv run python scripts/ci/mutate_m7_sql.py [SUBSTRING_OF_WHAT ...]`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG = "approval/src/gxg_approval/migrations/versions/0001_core.py"
INT = "approval/src/gxg_approval/intents.py"
TESTS = ["approval/tests_db"]
EQUIVALENT = "equivalent: "  # prefix of `what` for a mutant that cannot change behaviour (reason follows)

# (file, exact text, replacement, what the mutation breaks)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # helpers: switches, the transition table, the checked move
    (MIG, "'tenant:' || p_tenant, 'tool:' || p_tenant || ':' || p_tool)", "'tenant:' || p_tenant)",
     "tool kill switch ignored at T7/T9"),
    (MIG, "FROM switches WHERE blocked\n", "FROM switches WHERE true\n", "a cleared switch still blocks T7/T9"),
    (MIG, "WHERE event = p_event AND from_status = p_status", "WHERE event = p_event", "move from any status"),
    (MIG, "AND exec_state = coalesce(p_exec, '') AND to_status", "AND to_status", "execution state ignored"),
    (MIG, "AND to_status = p_to) THEN", ") THEN", "any target status"),
    (MIG, "AND version = p_row.version RETURNING", "RETURNING",
     EQUIVALENT + "every caller passes a row it locked FOR UPDATE (_gxg_intent/_gxg_execution or gxg_cancel), so "
     "the version cannot differ; callers with a client version compare it before the move"),
    (MIG, "THEN terminal_at ELSE now() END", "THEN now() ELSE now() END", "non-terminal status gets terminal_at"),
    # T1 insert guard
    (MIG, "PERFORM _gxg_check('', 'propose', NULL, NEW.status);", "NULL;", "intent born in any status"),
    (MIG, "IF NEW.version <> 1 OR NEW.attempts <> 0 OR", "IF NEW.attempts <> 0 OR", "born with version > 1"),
    (MIG, "IF NEW.version <> 1 OR NEW.attempts <> 0 OR", "IF NEW.version <> 1 OR", "born with attempts used"),
    (MIG, "OR NEW.terminal_at IS NOT NULL OR", "OR", "born terminal"),
    (MIG, "OR NEW.args_ciphertext IS NULL OR NEW.preview_ciphertext IS NULL", "OR NEW.args_ciphertext IS NULL",
     "born without encrypted preview"),
    (MIG, "     OR (NEW.status = 'pending_approval') <> (NEW.approval_mode = 'human')\n", "",
     "status and approval mode disagree"),
    (MIG, "     OR (NEW.approval_mode = 'human') <> (NEW.required_approvals > 0)\n", "",
     "human intent without approvals"),
    (MIG, "\n     OR (NEW.status = 'approved') <> (NEW.approved_at IS NOT NULL AND NEW.execution_deadline IS NOT NULL)"
     " THEN", " THEN", "approved without execution deadline"),
    # EFF-03 policy decision, received approvals
    (MIG, "IF r.approval_mode <> 'auto' OR r.status", "IF r.status", "policy decision for a non-auto intent"),
    (MIG, "OR r.status <> 'approved' OR r.version <> 1", "OR r.version <> 1",
     EQUIVALENT + "version 1 means no move yet, and the insert guard makes every auto intent approved at T1"),
    (MIG, "OR r.version <> 1\n     OR EXISTS", "OR EXISTS",
     EQUIVALENT + "T1 writes the policy decision in the transaction that inserts the auto intent, so a moved auto "
     "intent always has one and the EXISTS check refuses it"),
    (MIG, "     OR EXISTS (SELECT 1 FROM decisions WHERE intent_id = p_intent) THEN", "     THEN",
     "second policy decision"),
    (MIG, "AND decided_by NOT LIKE 'policy:%'", "", "policy decision counted as a human approval"),
    (MIG, "WHERE intent_id = p_intent AND decision = 'approve' AND", "WHERE intent_id = p_intent AND",
     "rejections counted as approvals"),
    # T6 cancel (AS-11a)
    (MIG, "WHERE intent_id = p_intent AND tenant_id = p_tenant\n", "WHERE intent_id = p_intent\n",
     "cancel another tenant's intent (same principal ID)"),
    (MIG, "AND requester_principal = p_principal FOR UPDATE", "FOR UPDATE", "cancel another requester's intent"),
    (MIG, "IF r.version <> p_version THEN PERFORM _gxg_fail('GX409', 'intent version changed'); END IF;\n"
     "  r := _gxg_move(r, 'cancel'", "r := _gxg_move(r, 'cancel'", "cancel with a stale version"),
    # T2-T4 decide
    (MIG, "IF p_decision NOT IN ('approve', 'reject') THEN", "IF false THEN", "unknown decision accepted"),
    (MIG, "IF r.version <> p_version OR r.canonical_args_hash", "IF r.canonical_args_hash", "stale screen decides"),
    (MIG, "OR r.canonical_args_hash <> p_args_hash OR", "OR", "decision on changed args"),
    (MIG, "OR now() >= r.expires_at THEN", " THEN", "decision after the approval deadline"),
    (MIG, "IF p_approver = r.requester_principal OR", "IF", "requester approves own intent"),
    (MIG, "OR NOT p_approver LIKE 'oidc:%'", "", "a machine principal approves"),
    (MIG, "     OR NOT p_approver = ANY (coalesce(r.allowed_approvers, ARRAY[]::text[]))\n", "",
     "approver outside allowed_approvers"),
    (MIG, "coalesce(r.allowed_approvers, ARRAY[]::text[])", "coalesce(r.allowed_approvers, ARRAY[p_approver])",
     "intent without a frozen approver set decided by anyone"),
    (MIG, "\n     OR (p_decision = 'approve' AND r.step_up_required AND NOT p_step_up) THEN", " THEN",
     "approve without required step-up"),
    (MIG, "PERFORM _gxg_fail('GX409', 'this approver has already decided')", "NULL", "second vote of one human"),
    (MIG, "WHEN approvals >= r.required_approvals THEN", "WHEN approvals > r.required_approvals THEN",
     "quorum needs one vote more"),
    (MIG, "WHEN approvals >= r.required_approvals THEN", "WHEN approvals >= 1 THEN", "quorum of one"),
    (MIG, "execution_deadline = now() + make_interval(secs => r.execution_window_s)", "execution_deadline = now()",
     "no execution window after approval"),
    (MIG, "    PERFORM _gxg_outbox('execute', r);\n    PERFORM _gxg_outbox('webhook', r);\n",
     "    PERFORM _gxg_outbox('webhook', r);\n", "approved intent never queued for execution"),
    # T18 manual resolution
    (MIG, "IF NOT p_actor LIKE 'oidc:%' THEN PERFORM _gxg_fail('GX403', 'only a human resolves manually'); END IF;",
     "", "a machine principal resolves manually"),
    (MIG, "OR length(coalesce(p_reason, '')) < 10 THEN", " THEN", "manual outcome without a reason"),
    (MIG, "IF r.version <> p_version THEN PERFORM _gxg_fail('GX409', 'intent version changed'); END IF;\n"
     "  r := _gxg_move(r, 'resolve_manual'", "r := _gxg_move(r, 'resolve_manual'", "manual outcome, stale version"),
    # T7 reserve, T9 dispatch
    (MIG, "IF now() >= r.execution_deadline OR r.attempts", "IF r.attempts", "reserve after the execution window"),
    (MIG, "OR r.attempts >= r.max_attempts THEN", " THEN", "reserve after the last attempt"),
    (MIG, "IF _gxg_blocked(r.tenant_id, r.tool_key) THEN", "IF false THEN", "kill switch ignored at T7"),
    (MIG, "(x.r).intent_id <> p_intent OR", "", "token sub of another intent dispatches"),
    (MIG, "OR (x.r).connector_id <> p_connector THEN", " THEN", "another connector dispatches"),
    (MIG, "IF now() >= (x.e).reserve_lease_until THEN", "IF false THEN", "dispatch after the reservation lease"),
    (MIG, "IF _gxg_blocked((x.r).tenant_id, (x.r).tool_key) THEN", "IF false THEN", "kill switch ignored at T9"),
    (MIG, "SET attempts = attempts + 1 WHERE", "SET attempts = attempts WHERE", "dispatch does not count"),
    # T11-T13 complete, T15-T17 reconcile
    (MIG, "THEN 1 ELSE 0 END;  -- T12a counts", "THEN 0 ELSE 0 END;  -- T12a counts", "T12a does not count"),
    (MIG, "WHEN used < r.max_attempts AND now() < r.execution_deadline THEN",
     "WHEN now() < r.execution_deadline THEN", "retry after the last attempt"),
    (MIG, "WHEN used < r.max_attempts AND now() < r.execution_deadline THEN", "WHEN used < r.max_attempts THEN",
     "retry after the execution window"),
    (MIG, "IF p_retry_after_s IS NOT NULL THEN", "IF false THEN", "unknown outcome never reconciled again"),
    # T5, T8, T10, T14 sweeper deadlines
    (MIG, "IF now() < r.expires_at THEN", "IF false THEN", "approval expired early"),
    (MIG, "IF now() < r.execution_deadline THEN", "IF false THEN", "execution window expired early"),
    (MIG, "IF now() < (x.e).reserve_lease_until THEN", "IF false THEN", "reservation released early"),
    (MIG, "IF now() < (x.e).dispatch_lease_until THEN", "IF false THEN", "dispatch declared unknown early"),
    # constraints and privileges (AS-02, AS-27)
    (MIG, "CREATE INDEX ON intents (tenant_id, created_at);", "", "auto usage count without its index"),
    (MIG, "WHERE state IN ('reserved','dispatching','outcome_unknown');",
     "WHERE state IN ('reserved','dispatching');",
     EQUIVALENT + "an execution in outcome_unknown belongs to an intent in outcome_unknown, and T7 (the only "
     "insert into executions) is allowed only from approved: the index is a second guard"),
    (MIG, "  decided_at       timestamptz NOT NULL,\n  UNIQUE (intent_id, decided_by)\n",
     "  decided_at       timestamptz NOT NULL\n", "two votes of one human"),
    (MIG, 'GRANT SELECT ({_cols("args_ciphertext")}) ON intents TO gxg_ui;', "GRANT SELECT ON intents TO gxg_ui;",
     "ui reads args"),
    (MIG, 'GRANT SELECT ({_cols("preview_ciphertext")}) ON intents TO gxg_executor;',
     "GRANT SELECT ON intents TO gxg_executor;", "executor reads preview"),
    (MIG, "GRANT SELECT ON executions TO gxg_ui,", "GRANT SELECT, INSERT ON executions TO gxg_ui,",
     "ui writes executions"),
    (MIG, "GRANT INSERT ON audit_events TO {roles};", "GRANT INSERT, UPDATE ON audit_events TO {roles};",
     "audit rows can be changed"),
    (MIG, "integer) TO gxg_api;", "integer) TO gxg_api, gxg_ui;", "ui runs T9 and T6"),
    (MIG, "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;\n", "", "every role runs every transition"),
    # SQL constants of intents.py (T1, read)
    (INT, "WHERE intent_id = %s AND tenant_id = %s AND", "WHERE intent_id = %s AND (tenant_id = %s OR true) AND",
     "read another tenant's intent (same principal ID)"),
    (INT, "AND requester_principal = %s\"", "AND (requester_principal = %s OR true)\"", "read another requester's"),
    (INT, "WHERE tenant_id = %s AND idempotency_key = %s", "WHERE (tenant_id = %s OR true) AND idempotency_key = %s",
     "idempotency key shared across tenants"),
    (INT, "FROM switches WHERE blocked AND scope", "FROM switches WHERE scope", "a cleared switch blocks T1"),
    (INT, "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", "SELECT %s", "T1 without the tenant lock"),
    (INT, "WHERE tenant_id = %(t)s AND requester_principal = %(r)s AND created_at",
     "WHERE requester_principal = %(r)s AND created_at", "hourly quota counts another tenant's intents"),
    (INT, "WHERE tenant_id = %(t)s AND requester_principal = %(r)s AND created_at",
     "WHERE tenant_id = %(t)s AND created_at", "hourly quota counts other requesters"),
    (INT, "AND created_at > now() - interval '1 hour') h", ") h", "hourly quota counts all history"),
    (INT, "WHERE tenant_id = %(t)s AND status = 'pending_approval') p", "WHERE tenant_id = %(t)s) p",
     "pending caps count every status"),
    (INT, "WHERE tenant_id = %(t)s AND status = 'pending_approval') p", "WHERE status = 'pending_approval') p",
     "pending caps count another tenant's intents"),
    (INT, "count(*) FILTER (WHERE requester_principal = %(r)s) AS mine", "count(*) AS mine",
     "requester pending cap counts the whole tenant"),
    (INT, "WHERE tenant_id = %s AND created_at > now() - interval '1 day'",
     "WHERE created_at > now() - interval '1 day'", "auto limit counts another tenant's intents"),
    (INT, "interval '1 day'", "interval '2 days'", "auto day limit window too long"),
    (INT, "interval '1 day'", "interval '12 hours'", "auto day limit window too short (fail open)"),
    (INT, "interval '1 hour'), count(*)", "interval '30 minutes'), count(*)", "auto hour window too short (fail open)"),
    (INT, "now() - min(created_at)", "now() - max(created_at)", "retry_after_s from the newest intent"),
    (INT, "count(*) FILTER (WHERE created_at > now() - interval '1 hour'), count(*)", "count(*), count(*)",
     "auto hour limit counts the day"),
    (INT, "AND approval_mode = 'auto' AND %s", "AND %s", "auto limit counts human intents of the rule"),
    (INT, "AND %s = ANY(rule_ids)", "AND (%s = ANY(rule_ids) OR true)", "auto limit counts other rules"),
]  # fmt: skip


def run_tests() -> bool:
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *TESTS]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, check=False).returncode == 0


def main() -> int:
    only = sys.argv[1:]  # optional substrings of `what`: re-check a few mutations
    if not run_tests():
        print("baseline: tests are red, nothing to measure")
        return 2
    bad = 0
    for file, old, new, what in MUTATIONS:
        if only and not any(o in what for o in only):
            continue
        path = ROOT / file
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"STALE     {file}: {what} (text found {text.count(old)} times)")
            bad += 1
            continue
        try:
            path.write_text(text.replace(old, new), encoding="utf-8")
            green = run_tests()
        finally:
            path.write_text(text, encoding="utf-8")
        equivalent = what.startswith(EQUIVALENT)
        verdict = ("equivalent" if green else "KILLED, listed as equivalent") if equivalent else (
            "SURVIVED" if green else "killed")  # fmt: skip
        bad += green != equivalent
        print(f"{verdict:<10}  {Path(file).name}: {what}")
    print(f"{len(MUTATIONS)} mutations listed: {bad} not as expected (survivor, stale text, killed 'equivalent')")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
