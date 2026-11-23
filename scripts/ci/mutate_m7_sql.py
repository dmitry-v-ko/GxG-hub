#!/usr/bin/env python3
"""DoD-4 for M7, M8 and M10, SQL part: each mutation of a key SQL condition must turn `approval/tests_db` red.

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
MIG2 = "approval/src/gxg_approval/migrations/versions/0002_executor.py"
MIG3 = "approval/src/gxg_approval/migrations/versions/0003_idempotency_per_requester.py"
MIG4 = "approval/src/gxg_approval/migrations/versions/0004_ui_notifier.py"  # M9
MIG5 = "approval/src/gxg_approval/migrations/versions/0005_ops.py"  # M10
MIG6 = "approval/src/gxg_approval/migrations/versions/0006_t12b.py"  # M10 scope addition: T12b
EXE = "approval/src/gxg_approval/executor.py"
TESTS = ["approval/tests_db", "approval/ui/tests_db", "approval/notifier/tests_db", "-m", "not e2e"]
M8_TESTS = ["approval/tests_db/test_executor.py", "approval/tests_db/test_roles.py"]  # M8 rows: their tests only
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
    # T2-T4 decide: since M10 the checks live in 0005's _gxg_decide (0001's gxg_decide is dropped)
    (MIG5, "IF p_decision NOT IN ('approve', 'reject') THEN", "IF false THEN", "unknown decision accepted"),
    (MIG5, "IF r.version <> p_version OR r.canonical_args_hash", "IF r.canonical_args_hash", "stale screen decides"),
    (MIG5, "OR r.canonical_args_hash <> p_args_hash OR now()", "OR now()", "decision on changed args"),
    (MIG5, "OR now() >= r.expires_at THEN", " THEN", "decision after the approval deadline"),
    (MIG5, "IF p_approver = r.requester_principal OR", "IF", "requester approves own intent"),
    (MIG5, "OR NOT p_approver LIKE 'oidc:%'\n", "\n", "a machine principal approves"),
    (MIG5, "     OR NOT p_approver = ANY (coalesce(r.allowed_approvers, ARRAY[]::text[])) THEN", " THEN",
     "approver outside allowed_approvers"),
    (MIG5, "coalesce(r.allowed_approvers, ARRAY[]::text[])", "coalesce(r.allowed_approvers, ARRAY[p_approver])",
     "intent without a frozen approver set decided by anyone"),
    (MIG5, "PERFORM _gxg_fail('GX409', 'this approver has already decided')", "NULL", "second vote of one human"),
    (MIG5, "WHEN approvals >= r.required_approvals THEN", "WHEN approvals > r.required_approvals THEN",
     "quorum needs one vote more"),
    (MIG5, "WHEN approvals >= r.required_approvals THEN", "WHEN approvals >= 1 THEN", "quorum of one"),
    (MIG5, "execution_deadline = now() + make_interval(secs => r.execution_window_s)", "execution_deadline = now()",
     "no execution window after approval"),
    (MIG5, "execution_deadline = now() + make_interval(secs => r.execution_window_s)",
     "execution_deadline = now() + interval '1 hour'", "T2 deadline from 1 h, not the window frozen at T1"),
    (MIG5, "    PERFORM _gxg_outbox('execute', r);\n  END IF;\nEND $$;\nDROP", "  END IF;\nEND $$;\nDROP",
     "approved intent never queued for execution"),
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
    (INT, "WHERE tenant_id = %s AND requester_principal = %s AND idempotency_key",
     "WHERE (tenant_id = %s OR true) AND requester_principal = %s AND idempotency_key",
     "idempotency key shared across tenants"),
    (INT, "AND requester_principal = %s AND idempotency_key = %s",
     "AND (requester_principal = %s OR true) AND idempotency_key = %s",
     "idempotency key shared across requesters (EFF-08)"),
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
    # M9: Telegram link codes (0004), narrowing of gxg_ui
    (MIG4, "IF NOT p_human LIKE 'oidc:%' OR p_code", "IF p_code", "a code for a machine principal"),
    (MIG4, "OR p_code !~ '^[0-9]{{6}}$' OR", "OR", "a code that is not six digits"),
    (MIG4, " OR coalesce(cardinality(p_tenants), 0) = 0 THEN", " THEN", "a code for no tenant"),
    (MIG4, "IF EXISTS (SELECT 1 FROM link_codes WHERE code_hash = _gxg_code_hash(p_code) AND used_at IS NULL",
     "IF false AND EXISTS (SELECT 1 FROM link_codes WHERE code_hash = _gxg_code_hash(p_code) AND used_at IS NULL",
     "the same digits live twice"),
    (MIG4, "  UPDATE link_codes SET expires_at = now() WHERE human_principal = p_human AND used_at IS NULL AND "
     "expires_at > now();\n", "", "an older code keeps working"),
    (MIG4, "make_interval(secs => {CODE_TTL_S})", "make_interval(secs => 10 * {CODE_TTL_S})", "code lives too long"),
    (MIG4, "AND at > now() - interval '10 minutes'", "AND at > now()", "wrong guesses never counted"),
    (MIG4, ">= {MAX_FAILED_CLAIMS} THEN", ">= {MAX_FAILED_CLAIMS} + 1 THEN", "one more guess allowed"),
    (MIG4, "AND details->>'telegram_user_id' = p_telegram_user::text", "", "one sender's guesses block everyone"),
    (MIG4, "AND used_at IS NULL\n                                     AND expires_at > now() FOR UPDATE",
     "AND expires_at > now() FOR UPDATE", "a used code works again"),
    (MIG4, "\n                                     AND expires_at > now() FOR UPDATE", " FOR UPDATE",
     "an expired code works"),
    (MIG4, "  UPDATE link_codes SET used_at = now(), telegram_user_id = p_telegram_user WHERE id = c.id;\n", "",
     "a code is never used up"),
    (MIG4, "DO UPDATE SET telegram_user_id = excluded.telegram_user_id,", "DO UPDATE SET telegram_user_id = "
     "approver_links.telegram_user_id,", "a new link does not replace the old Telegram account"),
    (MIG4, "FOREACH t IN ARRAY c.tenant_ids LOOP", "FOREACH t IN ARRAY c.tenant_ids[1:1] LOOP",
     "a code links only the first tenant"),
    (MIG4, "REVOKE INSERT ON outbox FROM gxg_ui;", "", "ui writes outbox rows"),
    (MIG4, "REVOKE INSERT, UPDATE ON approver_links FROM gxg_ui;", "", "ui writes links directly"),
    (MIG4, "ON FUNCTION gxg_link_claim(text, bigint) TO gxg_notifier;",
     "ON FUNCTION gxg_link_claim(text, bigint) TO gxg_notifier, gxg_ui;", "ui claims codes"),
    (MIG4, "GRANT SELECT, INSERT, UPDATE (sign_count) ON webauthn_credentials TO gxg_ui;",
     "GRANT SELECT, INSERT, UPDATE ON webauthn_credentials TO gxg_ui;",
     EQUIVALENT + "0005 revokes INSERT and UPDATE on webauthn_credentials from gxg_ui (table and column level)"),
    # migration 0003 (M8, EFF-08 per requester): pinned by test_propose.py, so it runs with every DB test
    (MIG3, "UNIQUE (tenant_id, requester_principal, idempotency_key)", "UNIQUE (tenant_id, idempotency_key)",
     "idempotency unique per tenant, not per requester (EFF-08)"),
]  # fmt: skip
# M8: AS-10 release (migration 0002) and the executor's SQL (outbox claim, delivery, reconcile backoff).
M8_MUTATIONS: list[tuple[str, str, str, str]] = [
    (MIG2, "    UPDATE executions SET state = 'released', completed_at = now() WHERE execution_id = id;\n", "",
     "AS-10 release leaves the execution reserved"),
    (MIG2, "PERFORM _gxg_outbox('execute', _gxg_move(", "PERFORM (_gxg_move(", "released intent never re-queued"),
    (MIG2, "GRANT EXECUTE ON FUNCTION gxg_release_reserved(text) TO gxg_executor;", "",
     "executor cannot release after a key removal"),
    (MIG2, "WHERE state = 'reserved' ORDER BY reserved_at", "WHERE true ORDER BY reserved_at",
     EQUIVALENT + "the CONTINUE WHEN after the row lock skips every execution that is not reserved"),
    (MIG2, "    CONTINUE WHEN (x.e).state <> 'reserved';  -- dispatched or released meanwhile\n", "",
     "release aborts on an execution dispatched between the loop's SELECT and the lock"),
    (EXE, "WHERE o.delivered_at IS NULL AND o.topic", "WHERE o.topic", "delivered outbox rows handled again"),
    (EXE, "o.topic IN ('execute', 'reconcile')", "o.topic IN ('execute')", "outcome_unknown never reconciled"),
    (EXE, "o.topic IN ('execute', 'reconcile')", "o.topic IN ('execute', 'reconcile', 'notify', 'webhook')",
     "executor eats the notifier's rows"),
    (EXE, "make_interval(secs => least(5 * 2 ^ (o.attempts - 1), 300))", "interval '0'",
     "a kill-switched row is retried without backoff"),
    (EXE, "UPDATE outbox SET delivered_at = now() WHERE", "UPDATE outbox SET delivered_at = NULL WHERE",
     "rows are never delivered"),
    (EXE, "SET attempts = attempts + 1 WHERE", "SET attempts = attempts WHERE",
     "kill-switched row without backoff count"),
    (EXE, "extract(epoch FROM now() - e.dispatched_at)", "0", "reconcile backoff ignores the time waited"),
]  # fmt: skip


# M10 (migration 0005): decider functions (AS-30), webhook events, purge (RET-01), kill switch, grants.
M10_TESTS = [
    "approval/tests_db/test_decider.py",
    "approval/tests_db/test_purge.py",
    "approval/tests_db/test_admin.py",
    "approval/tests_db/test_roles.py",
    "approval/tests_db/test_state_machine.py",
    "approval/tests_db/test_metrics.py",
    "approval/ui/tests_db/test_decisions.py",
    "approval/notifier/tests_db/test_webhooks.py",
    "approval/tests_db/test_executor.py",
    "-m",
    "not e2e",
]
M10_MUTATIONS: list[tuple[str, str, str, str]] = [
    # gxg_decision_challenge
    (MIG5, "IF r.status <> 'pending_approval' OR r.version <> p_version OR", "IF r.version <> p_version OR",
     "a challenge for a decided intent"),
    (MIG5, "OR r.version <> p_version OR r.canonical_args_hash <> p_args_hash THEN",
     "OR r.canonical_args_hash <> p_args_hash THEN", "a challenge for a stale version"),
    (MIG5, "OR r.version <> p_version OR r.canonical_args_hash <> p_args_hash THEN", "OR r.version <> p_version THEN",
     "a challenge for other args"),
    (MIG5, "p_intent::text || p_args_hash || p_version::text || p_decision", "p_intent::text || p_args_hash",
     "the challenge does not bind version and decision"),
    (MIG5, "IF p_human IS NULL OR NOT p_human LIKE 'oidc:%' THEN", "IF p_human IS NULL THEN",
     "a challenge for a machine principal"),
    # gxg_decide
    (MIG5, "IF ch.used_at IS NOT NULL THEN PERFORM _gxg_fail('GX409', 'assertion already used'); END IF;", "",
     "a used decision challenge accepted again"),
    (MIG5, "  IF ch.created_at < now() - {CHALLENGE_MAX_AGE} THEN PERFORM _gxg_fail('GX403', 'challenge older than 5 "
     "minutes'); END IF;\n  SELECT * INTO cr", "  SELECT * INTO cr", "a decision challenge older than 5 minutes"),
    (MIG5, "  UPDATE decider_challenges SET used_at = now() WHERE challenge = p_challenge;\n"
     "  UPDATE webauthn_credentials", "  UPDATE webauthn_credentials", "the decision challenge is never used up"),
    (MIG5, "IF NOT FOUND OR cr.enrolled_by IS NULL OR cr.human_principal <> ch.human_principal THEN",
     "IF NOT FOUND OR cr.human_principal <> ch.human_principal THEN", "a passkey written around the decider"),
    (MIG5, "IF NOT FOUND OR cr.enrolled_by IS NULL OR cr.human_principal <> ch.human_principal THEN",
     "IF NOT FOUND OR cr.enrolled_by IS NULL THEN", "another human's passkey decides"),
    (MIG5, "AND p_sign_count <= cr.sign_count THEN", "AND p_sign_count < cr.sign_count THEN",
     "a repeated signature counter accepted"),
    (MIG5, "     OR NOT p_assertion ?& ARRAY['credential_id', 'authenticator_data', 'client_data_json', "
     "'signature'] THEN",
     " THEN", "a decision stored without its assertion"),
    (MIG5, "  UPDATE webauthn_credentials SET sign_count = p_sign_count WHERE credential_id = p_credential_id;\n", "",
     "the counter is never stored"),
    # enrollment
    (MIG5, "AND used_at IS NULL AND expires_at > now()) THEN", "AND expires_at > now()) THEN",
     "a used invite gives a new registration challenge"),
    (MIG5, "IF NOT FOUND OR inv.used_at IS NOT NULL OR inv.expires_at <= now()",
     "IF NOT FOUND OR inv.expires_at <= now()",
     "a used invite enrolls again"),
    (MIG5, "OR inv.expires_at <= now() OR inv.human_principal", "OR inv.human_principal", "an expired invite enrolls"),
    (MIG5, "IF ch.used_at IS NOT NULL THEN PERFORM _gxg_fail('GX409', 'registration already used'); END IF;", "",
     "a registration challenge used twice"),
    (MIG5, "  UPDATE passkey_invites SET used_at = now() WHERE token_hash = inv.token_hash;\n", "",
     "an invite is never used up"),
    (MIG5, "VALUES (p_credential_id, ch.human_principal, p_public_key, p_sign_count, now(), inv.created_by);",
     "VALUES (p_credential_id, ch.human_principal, p_public_key, p_sign_count, now(), NULL);",
     "an enrolled passkey is not marked as enrolled"),
    (MIG5, "OR p_ttl_s IS NULL OR p_ttl_s NOT BETWEEN 60 AND 86400 THEN", " THEN", "an invite valid for ever"),
    # webhook events
    (MIG5, "  WHEN (OLD.status IS DISTINCT FROM NEW.status) EXECUTE", "  EXECUTE", "an event without a status change"),
    (MIG5, "   WHERE p_topic <> 'webhook'\n", "\n", "two webhook events for one change"),
    (MIG5, "CREATE TRIGGER intents_created_event AFTER INSERT ON intents FOR EACH ROW EXECUTE FUNCTION "
     "_gxg_intent_event();\n", "", "no event for T1"),
    # purge
    (MIG5, "terminal_at < now() - make_interval(days => p_days)", "terminal_at < now()", "payload purged at once"),
    (MIG5, "WHERE status IN {TERMINAL} AND terminal_at", "WHERE terminal_at",
     EQUIVALENT + "only terminal moves set terminal_at (_gxg_move), and outcome_unknown is never terminal"),
    (MIG5, "IF p_days IS NULL OR p_days NOT BETWEEN 1 AND 90 THEN", "IF p_days IS NULL THEN",
     "retention outside 1…90 days"),
    (MIG5, "DELETE FROM outbox WHERE delivered_at < now() - interval '1 day';", "",
     "delivered notify rows (title, requester) kept for ever"),
    (MIG5, "DELETE FROM outbox WHERE delivered_at < now() - interval '1 day';",
     "DELETE FROM outbox WHERE delivered_at IS NULL OR delivered_at < now() - interval '1 day';",
     "undelivered outbox rows deleted"),
    (MIG5, "coalesce(delivered_at, dead_at) < now() - interval '7 days'", "coalesce(delivered_at, dead_at) < now()",
     "finished deliveries deleted at once"),
    # kill switch (EX-13)
    (MIG5, "     OR p_blocked IS NULL OR length(coalesce(p_reason, '')) NOT BETWEEN 3 AND 500",
     "     OR p_blocked IS NULL",
     "a switch without a reason"),
    (MIG5, "CASE WHEN p_blocked THEN 'admin_switch_set' ELSE 'admin_switch_clear' END", "'admin_switch_set'",
     "a cleared switch audited as set"),
    # metrics (critic M10-1 #1), outbox guard
    (MIG5, "THEN ARRAY['outcome_unknown', e.state] ELSE", "THEN ARRAY[e.state] ELSE",
     "a reconcile lowers gxg_executions_total{outcome_unknown}"),
    (MIG5, " AND coalesce(e.error_code, '') <> 'rate_limited'\n", "\n", "a deferral counted as a finished attempt"),
    (MIG5, "  IF NEW.topic = 'webhook' AND current_user", "  IF false AND current_user",
     "a process role forges a webhook event"),
    # T12b (0006)
    (MIG6, "  PERFORM _gxg_check((x.r).status, 'complete_rate_limited', (x.e).state, 'approved');\n", "",
     "T12b from any status or execution state"),
    (MIG6, "error_code = 'rate_limited'\n", "error_code = 'released'\n", "deferral without its reason"),
    (MIG6, "greatest({MIN_DELAY_S}, p_retry_after_s)", "p_retry_after_s", "retry sooner than 5 s"),
    (MIG6, "  IF now() + make_interval(secs => delay) < r.execution_deadline THEN\n"
     "    PERFORM _gxg_outbox('execute', r, ceil(delay)::integer);\n  END IF;",
     "  PERFORM _gxg_outbox('execute', r, ceil(delay)::integer);", "a task queued past the execution deadline"),
    (MIG6, "  IF p_retry_after_s IS NULL OR p_retry_after_s < 1 THEN", "  IF p_retry_after_s IS NULL THEN",
     "a deferral without a positive retry_after_s"),
    (MIG6, "AND delivered_at IS NULL AND available_at <= now()) THEN", "AND delivered_at IS NULL) THEN",
     "T7 before the task is due"),
    (MIG6, "AND delivered_at IS NULL AND available_at <= now()) THEN", "AND available_at <= now()) THEN",
     "T7 from a delivered task"),
    (MIG6, "  WHEN (OLD.state = 'reserved' AND NEW.state = 'released' AND NEW.error_code IS NULL)\n", "",
     "every state change overwrites the error code"),
    (MIG6, "GRANT EXECUTE ON FUNCTION gxg_defer(uuid, text, integer) TO gxg_executor;", "",
     "the executor cannot defer"),
    # grants
    (MIG5, "REVOKE INSERT, UPDATE ON webauthn_credentials FROM gxg_ui;", "", "ui writes passkeys (AS-30)"),
    (MIG5, "GRANT EXECUTE ON FUNCTION gxg_purge(integer, integer) TO gxg_sweeper;",
     "GRANT EXECUTE ON FUNCTION gxg_purge(integer, integer) TO gxg_sweeper, gxg_ui;", "ui purges payload"),
]  # fmt: skip


def run_tests(tests: list[str] = TESTS) -> bool:
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, check=False).returncode == 0


def main() -> int:
    only = sys.argv[1:]  # optional substrings of `what`: re-check a few mutations
    if not run_tests():
        print("baseline: tests are red, nothing to measure")
        return 2
    bad = 0
    listed = (
        [(m, TESTS) for m in MUTATIONS]
        + [(m, M8_TESTS) for m in M8_MUTATIONS]
        + [(m, M10_TESTS) for m in M10_MUTATIONS]
    )
    for (file, old, new, what), tests in listed:
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
            green = run_tests(tests)
        finally:
            path.write_text(text, encoding="utf-8")
        equivalent = what.startswith(EQUIVALENT)
        verdict = ("equivalent" if green else "KILLED, listed as equivalent") if equivalent else (
            "SURVIVED" if green else "killed")  # fmt: skip
        bad += green != equivalent
        print(f"{verdict:<10}  {Path(file).name}: {what}")
    print(f"{len(listed)} mutations listed: {bad} not as expected (survivor, stale text, killed 'equivalent')")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
