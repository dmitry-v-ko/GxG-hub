"""AS-02, AS-27: one database role per process. AC-M7-11 and the database part of AC-M7-13.

The expected matrix is written from the spec table. Write cells whose consistency matters (UPDATE of
`intents`/`executions`, INSERT of `decisions`/`executions`) are reachable only through the role's
SECURITY DEFINER transition functions (plan M7-cycle-1, decision 1), so they appear under FUNCTIONS,
not as table privileges. Everything not listed must be denied: the catalogue check is exhaustive over
tables, columns, privileges and functions; the statement checks run real SQL as each role.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

ROLES = ("gxg_api", "gxg_mcp", "gxg_ui", "gxg_executor", "gxg_sweeper", "gxg_notifier", "gxg_admin", "gxg_decider")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
ALL = "*"
CIPHER = {"args_ciphertext", "preview_ciphertext"}

# role → table → privilege → columns (ALL = every column)
MATRIX: dict[str, dict[str, dict[str, set[str] | str]]] = {
    "gxg_api": {
        "intents": {"INSERT": ALL, "SELECT": "-" + ",".join(sorted(CIPHER))},
        "outbox": {"INSERT": ALL},
        "switches": {"SELECT": ALL},
        "audit_events": {"INSERT": ALL},
    },
    "gxg_mcp": {"audit_events": {"INSERT": ALL}},
    # M9 narrowing of the AS-27 row: no outbox INSERT and no direct approver_links write (gxg_link_claim writes
    # links for the notifier); passkeys (M9, a table AS-27 does not list, pending_owner_decision).
    "gxg_ui": {
        "intents": {"SELECT": "-args_ciphertext"},
        "decisions": {"SELECT": ALL},
        "executions": {"SELECT": ALL},
        "switches": {"SELECT": ALL},
        "audit_events": {"INSERT": ALL},
        "approver_links": {"SELECT": ALL},
        "webauthn_credentials": {"SELECT": ALL},  # M10 (AS-30): the decider enrolls, the ui only shows
    },
    "gxg_executor": {
        "intents": {"SELECT": "-preview_ciphertext"},
        "decisions": {"SELECT": ALL},
        "executions": {"SELECT": ALL},
        "outbox": {"SELECT": ALL, "UPDATE": {"delivered_at", "attempts"}},
        "switches": {"SELECT": ALL},
        "audit_events": {"INSERT": ALL},
    },
    "gxg_sweeper": {
        "intents": {"SELECT": "-" + ",".join(sorted(CIPHER))},
        "executions": {"SELECT": ALL},
        "outbox": {"INSERT": ALL},
        "switches": {"SELECT": ALL},
        "audit_events": {"INSERT": ALL},
    },
    "gxg_notifier": {
        "outbox": {"SELECT": ALL, "UPDATE": {"delivered_at", "attempts"}},
        "audit_events": {"INSERT": ALL},
        "approver_links": {"SELECT": ALL},
        # M10 (AS-23): one row per (event, subscriber), a table AS-27 does not list (pending_owner_decision)
        "webhook_deliveries": {
            "SELECT": ALL, "INSERT": ALL, "UPDATE": {"next_at", "attempts", "last_status", "delivered_at", "dead_at"}
        },
    },
    "gxg_admin": {
        "outbox": {"INSERT": ALL},
        "switches": {"SELECT": ALL, "INSERT": ALL, "UPDATE": ALL},
        "audit_events": {"INSERT": ALL},
    },
    # M10 (AS-30): reads passkeys and challenges; every write is one of its functions
    "gxg_decider": {
        "webauthn_credentials": {"SELECT": "-created_at"},
        "decider_challenges": {"SELECT": ALL},
    },
}
FUNCTIONS = {
    # T1, T6, T9; /metrics (M10)
    "gxg_api": {"gxg_record_auto_decision", "gxg_received_approvals", "gxg_cancel", "gxg_dispatch", "gxg_metrics"},
    "gxg_ui": {"gxg_resolve_manual", "gxg_link_code"},  # T18; Telegram link code (M9). T2-T4: the decider (M10)
    "gxg_decider": {"gxg_decision_challenge", "gxg_decide", "gxg_enroll_challenge", "gxg_enroll"},  # AS-30
    "gxg_admin": {"gxg_switch", "gxg_passkey_invite"},  # AS-24, EX-13
    "gxg_notifier": {"gxg_link_claim"},  # `/link <code>` (M9)
    "gxg_executor": {
        "gxg_reserve",
        "gxg_complete",
        "gxg_reconcile",
        "gxg_release_reserved",
    },  # T7, T11-T13, T12a, T15-T17; T10 at AS-10
    "gxg_sweeper": {
        "gxg_expire_approval",
        "gxg_expire_execution",
        "gxg_release_reservation",
        "gxg_expire_dispatch",
        "gxg_sweep",
        "gxg_purge",
    },  # T5, T8, T10, T14; RET-01 (M10)
}
TABLES = (
    "intents",
    "decisions",
    "executions",
    "outbox",
    "switches",
    "approver_links",
    "audit_events",
    "transition_rules",
    "link_codes",
    "webauthn_credentials",
    "decider_challenges",
    "passkey_invites",
    "webhook_deliveries",
)


def expected_columns(spec: set[str] | str, columns: list[str]) -> set[str]:
    if spec == ALL:
        return set(columns)
    if isinstance(spec, str):  # "-a,b": all but these
        return set(columns) - set(spec[1:].split(","))
    return spec


def test_table_and_column_privileges_match_the_matrix(owner: psycopg.Connection[Any]) -> None:
    mismatches = []
    for table in TABLES:
        columns = [
            r[0]
            for r in owner.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
        ]
        for role in ROLES:
            granted = MATRIX[role].get(table, {})
            for priv in TABLE_PRIVILEGES:
                want = expected_columns(granted[priv], columns) if priv in granted else set()
                if priv in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    have = {
                        c
                        for c in columns
                        if owner.execute(
                            "SELECT has_column_privilege(%s, %s, %s, %s)", (role, table, c, priv)
                        ).fetchone()
                        == (True,)
                    }
                else:
                    on = owner.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, priv)).fetchone()
                    have = set(columns) if on == (True,) else set()
                if have != want:
                    mismatches.append((role, table, priv, sorted(have ^ want)))
    assert mismatches == []


def test_function_privileges_match_the_matrix(owner: psycopg.Connection[Any]) -> None:
    functions = owner.execute(
        "SELECT p.oid::regprocedure::text, p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'"
    ).fetchall()
    assert len(functions) >= 20
    mismatches = []
    for signature, name in functions:
        for role in ROLES:
            have = owner.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, signature)).fetchone()
            if (have == (True,)) != (name in FUNCTIONS.get(role, set())):
                mismatches.append((role, signature))
    assert mismatches == []
    definer = owner.execute(
        "SELECT proname FROM pg_proc WHERE proname LIKE 'gxg\\_%' AND NOT (prosecdef AND "
        "'search_path=public, pg_temp' = ANY(proconfig))"
    ).fetchall()
    assert definer == []  # every entry point is SECURITY DEFINER with a fixed search_path


def test_no_create_temp_or_role_powers(owner: psycopg.Connection[Any], pg: Any) -> None:
    for role in ROLES:
        row = owner.execute(
            "SELECT has_schema_privilege(%(r)s, 'public', 'CREATE'), has_database_privilege(%(r)s, %(d)s, 'CREATE'),"
            " has_database_privilege(%(r)s, %(d)s, 'TEMP'), has_database_privilege(%(r)s, %(d)s, 'CONNECT'),"
            " (SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls FROM pg_roles WHERE rolname = %(r)s)",
            {"r": role, "d": pg.dbname},
        ).fetchone()
        assert row == (False, False, False, True, False), role
    owns = owner.execute(
        "SELECT count(*) FROM pg_class WHERE relowner <> (SELECT oid FROM pg_roles "
        "WHERE rolname = 'gxg_owner') AND relnamespace = 'public'::regnamespace"
    ).fetchone()
    assert owns == (0,)  # AS-27: the owner is a separate role


@pytest.fixture
def rows(owner: psycopg.Connection[Any], propose: Any, pg: Any) -> dict[str, str]:
    intent = propose(tool="demo.note.propose").json()["intent_id"]
    with pg.connect("gxg_executor") as ex:
        ex.execute("SELECT gxg_reserve(%s, 'x')", (intent,))
    owner.execute("INSERT INTO switches VALUES ('global', false, 'r', 'op', now())")
    owner.execute(f"INSERT INTO approver_links VALUES ('{HUMAN}', 't-alpha', 42, now())")
    return {"intent": intent}


HUMAN = "oidc:https://idp.example.invalid:1"
STATEMENTS = {
    "update_audit": "UPDATE audit_events SET actor = 'x'",
    "delete_audit": "DELETE FROM audit_events",
    "delete_intents": "DELETE FROM intents",
    "delete_decisions": "DELETE FROM decisions",
    "delete_executions": "DELETE FROM executions",
    "update_status": "UPDATE intents SET status = 'succeeded'",
    "insert_execution": "INSERT INTO executions (execution_id, intent_id, attempt, state, reserved_by, reserved_at, "
    "reserve_lease_until) SELECT gen_random_uuid(), intent_id, 9, 'reserved', 'x', now(), now() "
    "FROM intents",
    "update_execution": "UPDATE executions SET state = 'dispatching'",
    "insert_decision": f"INSERT INTO decisions SELECT gen_random_uuid(), intent_id, tenant_id, '{HUMAN}', 'approve', "
    f"1, canonical_args_hash, false, null, now(), null FROM intents",
    "read_args": "SELECT args_ciphertext FROM intents",
    "read_preview": "SELECT preview_ciphertext FROM intents",
    "read_intents": "SELECT intent_id, status FROM intents",
    "read_decisions": "SELECT decided_by FROM decisions",
    "read_executions": "SELECT state FROM executions",
    "insert_audit": "INSERT INTO audit_events (at, actor, action) VALUES (now(), 'test', 'probe')",
    "insert_outbox": "INSERT INTO outbox (topic, payload, created_at, available_at) VALUES ('t', '{}', now(), now())",
    "read_outbox": "SELECT topic FROM outbox",
    "deliver_outbox": "UPDATE outbox SET delivered_at = now(), attempts = attempts + 1",
    "retarget_outbox": "UPDATE outbox SET topic = 'x'",
    "read_switches": "SELECT scope FROM switches",
    "set_switch": "INSERT INTO switches VALUES ('tenant:t-x', true, 'r', 'op', now())",
    "clear_switch": "UPDATE switches SET blocked = false",
    "link_approver": "INSERT INTO approver_links VALUES ('oidc:x', 't-alpha', 43, now())",
    "relink_approver": "UPDATE approver_links SET telegram_user_id = 44",
    "read_links": "SELECT telegram_user_id FROM approver_links",
    "read_rules": "SELECT * FROM transition_rules",
    "call_decide": "SELECT gxg_decide('\\x01', '\\x01', 0, null, '{}')",
    "call_challenge": "SELECT gxg_decision_challenge('oidc:x', gen_random_uuid(), 'approve', 1, '')",
    "add_passkey": "INSERT INTO webauthn_credentials VALUES ('\\x01', 'oidc:x', '\\x02', 0, now(), 'oidc:op')",
    "count_passkey": "UPDATE webauthn_credentials SET sign_count = 9",
    "read_passkeys": "SELECT public_key FROM webauthn_credentials",
    "read_challenges": "SELECT challenge FROM decider_challenges",
    "call_enroll": "SELECT gxg_enroll('\\x01', '\\x01', '\\x02', 0)",
    "call_invite": "SELECT gxg_passkey_invite('oidc:x', repeat('a', 64), 'oidc:op', 600)",
    "read_invites": "SELECT token_hash FROM passkey_invites",
    "call_switch": "SELECT gxg_switch('global', true, 'probe', 'oidc:op')",
    "call_purge": "SELECT gxg_purge(30, 1)",
    "call_metrics": "SELECT * FROM gxg_metrics()",
    "read_deliveries": "SELECT event FROM webhook_deliveries",
    "add_delivery": "INSERT INTO webhook_deliveries (outbox_id, subscriber_id, tenant_id, event, created_at, next_at) "
    "VALUES (1, 's', 't-alpha', '{}', now(), now())",
    "retarget_delivery": "UPDATE webhook_deliveries SET subscriber_id = 'x'",
    "read_link_codes": "SELECT code_hash FROM link_codes",
    "call_link_code": "SELECT gxg_link_code('oidc:x', '{t-alpha}', '123456')",
    "call_link_claim": "SELECT gxg_link_claim('123456', 42)",
    "call_sweep": "SELECT gxg_sweep(1)",
    "call_reserve": "SELECT gxg_reserve(gen_random_uuid(), 'x')",
    "call_move_helper": "SELECT _gxg_blocked('t', 'x')",
}
ALLOWED = {
    "gxg_api": {"read_intents", "insert_audit", "insert_outbox", "read_switches", "call_metrics"},
    "gxg_mcp": {"insert_audit"},
    "gxg_ui": {
        "read_intents",
        "read_preview",
        "read_decisions",
        "read_executions",
        "insert_audit",
        "read_switches",
        "read_links",
        "read_passkeys",
        "call_link_code",
    },
    "gxg_executor": {
        "read_intents",
        "read_args",
        "read_decisions",
        "read_executions",
        "insert_audit",
        "read_outbox",
        "deliver_outbox",
        "read_switches",
        "call_reserve",
    },
    "gxg_sweeper": {
        "read_intents", "read_executions", "insert_audit", "insert_outbox", "read_switches", "call_sweep", "call_purge",
    },
    "gxg_notifier": {
        "insert_audit", "read_outbox", "deliver_outbox", "read_links", "call_link_claim", "read_deliveries",
        "add_delivery",
    },
    "gxg_admin": {
        "insert_audit", "insert_outbox", "read_switches", "set_switch", "clear_switch", "call_switch", "call_invite",
    },
    "gxg_decider": {"call_decide", "call_challenge", "read_passkeys", "read_challenges", "call_enroll"},
}


@pytest.mark.parametrize("role", ROLES)
def test_statements_as_each_role(role: str, pg: Any, rows: dict[str, str]) -> None:
    """AC-M7-11 (UPDATE/DELETE audit_events, DELETE intents → permission error) and AC-M7-13 for every role."""
    wrong = []
    with pg.connect(role) as conn:
        for name, sql in STATEMENTS.items():
            try:
                with conn.transaction(force_rollback=True):
                    conn.execute(sql)
                allowed = True
            except psycopg.errors.InsufficientPrivilege:
                allowed = False
            except psycopg.Error:  # got past the privilege check (not_found of a random id, a foreign key, …)
                allowed = True
            if allowed != (name in ALLOWED[role]):
                wrong.append((name, "allowed" if allowed else "denied"))
    assert wrong == []
