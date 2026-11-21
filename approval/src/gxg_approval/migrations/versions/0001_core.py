"""Core schema: data model, roles' privileges (AS-02, AS-27), transitions T1-T18 + T12a as SECURITY DEFINER functions.

Revision ID: 0001
"""

import re

from alembic import op

from gxg_approval import transitions

revision = "0001"
down_revision = None

STATUSES = (
    "pending_approval",
    "approved",
    "executing",
    "succeeded",
    "failed",
    "outcome_unknown",
    "reconciled_succeeded",
    "reconciled_failed",
    "rejected",
    "expired",
    "cancelled",
)  # intent-status
ROLES = ("gxg_api", "gxg_mcp", "gxg_ui", "gxg_executor", "gxg_sweeper", "gxg_notifier", "gxg_admin")
INTENT_COLUMNS = (
    "intent_id",
    "tenant_id",
    "requester_principal",
    "business_task_id",
    "tool_key",
    "tool_version",
    "connector_id",
    "execute_tool",
    "connection_id",
    "resource_id",
    "effect_class",
    "canonical_args_hash",
    "args_ciphertext",
    "preview_ciphertext",
    "idempotency_key",
    "policy_version",
    "rule_ids",
    "approval_mode",
    "required_approvals",
    "step_up_required",
    "allowed_approvers",
    "status",
    "version",
    "attempts",
    "max_attempts",
    "created_at",
    "expires_at",
    "approved_at",
    "execution_deadline",
    "terminal_at",
    "payload_purged_at",
    "execution_window_s",
)


def _in(values: tuple[str, ...]) -> str:
    assert all(re.fullmatch(r"[a-z_]+", v) for v in values)
    return ", ".join(f"'{v}'" for v in values)


def _cols(*without: str) -> str:
    return ", ".join(c for c in INTENT_COLUMNS if c not in without)


DEFINER = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp"


def upgrade() -> None:
    statuses = _in(STATUSES)
    op.execute(f"""
CREATE TABLE intents (
  intent_id            uuid PRIMARY KEY,
  tenant_id            text NOT NULL,
  requester_principal  text NOT NULL,
  business_task_id     text,
  tool_key             text NOT NULL,
  tool_version         text NOT NULL,
  connector_id         text NOT NULL,
  execute_tool         text NOT NULL,
  connection_id        text NOT NULL,
  resource_id          text NOT NULL,
  effect_class         text NOT NULL CHECK (effect_class IN ('write_reversible','external_effect','high_risk')),
  canonical_args_hash  char(64) NOT NULL,
  args_ciphertext      bytea,
  preview_ciphertext   bytea,
  idempotency_key      text NOT NULL,
  policy_version       text NOT NULL,
  rule_ids             text[] NOT NULL DEFAULT '{{}}',
  approval_mode        text NOT NULL CHECK (approval_mode IN ('human','auto','not_required')),
  required_approvals   smallint NOT NULL CHECK (required_approvals BETWEEN 0 AND 2),
  step_up_required     boolean NOT NULL,
  allowed_approvers    text[],
  status               text NOT NULL CHECK (status IN ({statuses})),
  version              integer NOT NULL DEFAULT 1,
  attempts             smallint NOT NULL DEFAULT 0,
  max_attempts         smallint NOT NULL DEFAULT 3,
  created_at           timestamptz NOT NULL,
  expires_at           timestamptz NOT NULL,
  approved_at          timestamptz,
  execution_deadline   timestamptz,
  terminal_at          timestamptz,
  payload_purged_at    timestamptz,
  execution_window_s   integer NOT NULL CHECK (execution_window_s > 0),  -- AP-04: frozen at T1, used by T2
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX ON intents (tenant_id, status, expires_at);
CREATE INDEX ON intents (tenant_id, requester_principal, created_at);
CREATE INDEX ON intents (tenant_id, created_at);  -- AP-03 auto usage window (T1)

CREATE TABLE decisions (
  decision_id      uuid PRIMARY KEY,
  intent_id        uuid NOT NULL REFERENCES intents,
  tenant_id        text NOT NULL,
  decided_by       text NOT NULL,
  decision         text NOT NULL CHECK (decision IN ('approve','reject')),
  intent_version   integer NOT NULL,
  args_hash        char(64) NOT NULL,
  step_up          boolean NOT NULL DEFAULT false,
  comment          text,
  decided_at       timestamptz NOT NULL,
  UNIQUE (intent_id, decided_by)
);

CREATE TABLE executions (
  execution_id          uuid PRIMARY KEY,
  intent_id             uuid NOT NULL REFERENCES intents,
  attempt               smallint NOT NULL,
  state                 text NOT NULL CHECK (state IN ('reserved','dispatching','succeeded',
                          'failed_before_effect','outcome_unknown','reconciled_executed',
                          'reconciled_not_executed','released')),
  reserved_by           text NOT NULL,
  reserved_at           timestamptz NOT NULL,
  reserve_lease_until   timestamptz NOT NULL,
  dispatched_at         timestamptz,
  dispatch_lease_until  timestamptz,
  completed_at          timestamptz,
  provider_operation_id text,
  provider_receipt_ref  text,
  error_code            text,
  UNIQUE (intent_id, attempt)
);
CREATE UNIQUE INDEX one_active_execution ON executions (intent_id)
  WHERE state IN ('reserved','dispatching','outcome_unknown');

CREATE TABLE outbox (
  id bigserial PRIMARY KEY, topic text NOT NULL, intent_id uuid, payload jsonb NOT NULL,
  created_at timestamptz NOT NULL, available_at timestamptz NOT NULL,
  attempts integer NOT NULL DEFAULT 0, delivered_at timestamptz
);

CREATE TABLE switches (
  scope text PRIMARY KEY,
  blocked boolean NOT NULL, reason text NOT NULL, changed_by text NOT NULL, changed_at timestamptz NOT NULL
);

CREATE TABLE approver_links (
  human_principal text NOT NULL, tenant_id text NOT NULL, telegram_user_id bigint NOT NULL,
  linked_at timestamptz NOT NULL, PRIMARY KEY (human_principal, tenant_id)
);

CREATE TABLE audit_events (
  id bigserial PRIMARY KEY, at timestamptz NOT NULL, actor text NOT NULL, action text NOT NULL,
  tenant_id text, intent_id uuid, from_status text, to_status text, details jsonb
);

-- AS-03: the only allowed moves (gxg_approval.transitions.rules()). No process role can read or change it.
CREATE TABLE transition_rules (
  tid text NOT NULL, event text NOT NULL, from_status text NOT NULL, exec_state text NOT NULL,
  to_status text NOT NULL, PRIMARY KEY (event, from_status, exec_state, to_status)
);
""")
    op.execute(transitions.install_sql())

    # ---- helpers (no EXECUTE for process roles; called from the definer functions below) ----
    op.execute(f"""
CREATE FUNCTION _gxg_fail(p_code text, p_message text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION USING ERRCODE = p_code, MESSAGE = p_message; END $$;

CREATE FUNCTION _gxg_blocked(p_tenant text, p_tool text) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM switches WHERE blocked
                 AND scope IN ('global', 'tenant:' || p_tenant, 'tool:' || p_tenant || ':' || p_tool))
$$;

CREATE FUNCTION _gxg_check(p_status text, p_event text, p_exec text, p_to text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM transition_rules WHERE event = p_event AND from_status = p_status
                 AND exec_state = coalesce(p_exec, '') AND to_status = p_to) THEN
    PERFORM _gxg_fail('GX409', format('%s is not allowed from %s/%s', p_event, p_status, coalesce(p_exec, '-')));
  END IF;
END $$;

-- AS-01: checked status move + version++ + audit row, in the caller's transaction.
CREATE FUNCTION _gxg_move(p_row intents, p_event text, p_exec text, p_to text, p_actor text,
                          p_details jsonb DEFAULT NULL) RETURNS intents LANGUAGE plpgsql AS $$
DECLARE r intents;
BEGIN
  PERFORM _gxg_check(p_row.status, p_event, p_exec, p_to);
  UPDATE intents SET status = p_to, version = version + 1,
         terminal_at = CASE WHEN EXISTS (SELECT 1 FROM transition_rules WHERE from_status = p_to)
                            THEN terminal_at ELSE now() END  -- AS-03: no way out = terminal
   WHERE intent_id = p_row.intent_id AND version = p_row.version RETURNING * INTO r;
  IF NOT FOUND THEN PERFORM _gxg_fail('GX409', 'intent version changed'); END IF;
  INSERT INTO audit_events (at, actor, action, tenant_id, intent_id, from_status, to_status, details)
  VALUES (now(), p_actor, p_event, r.tenant_id, r.intent_id, p_row.status, p_to, p_details);
  RETURN r;
END $$;

-- Outbox payload never carries args or preview (SEC-18).
CREATE FUNCTION _gxg_outbox(p_topic text, r intents, p_delay_s integer DEFAULT 0) RETURNS void LANGUAGE sql AS $$
  INSERT INTO outbox (topic, intent_id, payload, created_at, available_at)
  VALUES (p_topic, r.intent_id, jsonb_build_object('intent_id', r.intent_id, 'tenant_id', r.tenant_id,
          'status', r.status, 'version', r.version), now(), now() + make_interval(secs => p_delay_s))
$$;

CREATE FUNCTION _gxg_intent(p_intent uuid) RETURNS intents LANGUAGE plpgsql AS $$
DECLARE r intents;
BEGIN
  SELECT * INTO r FROM intents WHERE intent_id = p_intent FOR UPDATE;
  IF NOT FOUND THEN PERFORM _gxg_fail('GX404', 'intent not found'); END IF;
  RETURN r;
END $$;

-- Lock order everywhere: intent, then execution.
CREATE FUNCTION _gxg_execution(p_execution uuid, OUT r intents, OUT e executions) LANGUAGE plpgsql AS $$
BEGIN
  r := _gxg_intent((SELECT intent_id FROM executions WHERE execution_id = p_execution));
  SELECT * INTO e FROM executions WHERE execution_id = p_execution FOR UPDATE;
END $$;

-- T1 is a plain INSERT by gxg_api; this guard keeps it to a row the T1 rules allow.
CREATE FUNCTION _gxg_intent_insert_guard() RETURNS trigger {DEFINER} AS $$
BEGIN
  PERFORM _gxg_check('', 'propose', NULL, NEW.status);
  IF NEW.version <> 1 OR NEW.attempts <> 0 OR NEW.terminal_at IS NOT NULL OR NEW.payload_purged_at IS NOT NULL
     OR NEW.args_ciphertext IS NULL OR NEW.preview_ciphertext IS NULL
     OR (NEW.status = 'pending_approval') <> (NEW.approval_mode = 'human')
     OR (NEW.approval_mode = 'human') <> (NEW.required_approvals > 0)
     OR (NEW.status = 'approved') <> (NEW.approved_at IS NOT NULL AND NEW.execution_deadline IS NOT NULL) THEN
    PERFORM _gxg_fail('GX400', 'not a valid T1 row');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER intents_insert_guard BEFORE INSERT ON intents FOR EACH ROW EXECUTE FUNCTION _gxg_intent_insert_guard();
""")

    # ---- transitions (one function per event family; EXECUTE granted per role below) ----
    op.execute(f"""
-- EFF-03: the auto-approval decision of T1.
CREATE FUNCTION gxg_record_auto_decision(p_intent uuid) RETURNS void {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent);
BEGIN
  IF r.approval_mode <> 'auto' OR r.status <> 'approved' OR r.version <> 1
     OR EXISTS (SELECT 1 FROM decisions WHERE intent_id = p_intent) THEN
    PERFORM _gxg_fail('GX409', 'not a fresh auto-approved intent');
  END IF;
  INSERT INTO decisions (decision_id, intent_id, tenant_id, decided_by, decision, intent_version, args_hash, decided_at)
  VALUES (gen_random_uuid(), p_intent, r.tenant_id, 'policy:' || r.rule_ids[1] || '@' || r.policy_version,
          'approve', 1, r.canonical_args_hash, now());
END $$;

CREATE FUNCTION gxg_received_approvals(p_intent uuid) RETURNS integer
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT count(*)::integer FROM decisions
   WHERE intent_id = p_intent AND decision = 'approve' AND decided_by NOT LIKE 'policy:%'
$$;

-- T6 by the requester (api on behalf of the agent, AS-11a): another tenant's or requester's intent is not found.
-- No live-execution check: T6 is allowed only from pending_approval/approved (_gxg_move), and an intent with a
-- live execution is always executing/outcome_unknown (T7 moves it in the same transaction that reserves).
CREATE FUNCTION gxg_cancel(p_intent uuid, p_tenant text, p_principal text, p_version integer) RETURNS void {DEFINER} AS $$
DECLARE r intents;
BEGIN
  SELECT * INTO r FROM intents WHERE intent_id = p_intent AND tenant_id = p_tenant
                                 AND requester_principal = p_principal FOR UPDATE;
  IF NOT FOUND THEN PERFORM _gxg_fail('GX404', 'intent not found'); END IF;
  IF r.version <> p_version THEN PERFORM _gxg_fail('GX409', 'intent version changed'); END IF;
  r := _gxg_move(r, 'cancel', NULL, 'cancelled', p_principal);
  PERFORM _gxg_outbox('webhook', r);
END $$;

-- T2, T3, T4 (ui). Role `approver` by bundle is checked by the ui before the call (M9).
CREATE FUNCTION gxg_decide(p_intent uuid, p_approver text, p_decision text, p_version integer, p_args_hash text,
                           p_step_up boolean, p_comment text) RETURNS void {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent); approvals integer; target text;
BEGIN
  IF p_decision NOT IN ('approve', 'reject') THEN PERFORM _gxg_fail('GX400', 'decision must be approve or reject'); END IF;
  PERFORM _gxg_check(r.status, p_decision, NULL, CASE p_decision WHEN 'reject' THEN 'rejected' ELSE r.status END);
  IF r.version <> p_version OR r.canonical_args_hash <> p_args_hash OR now() >= r.expires_at THEN
    PERFORM _gxg_fail('GX409', 'stale decision: version, args hash or deadline');
  END IF;
  IF p_approver = r.requester_principal OR NOT p_approver LIKE 'oidc:%'
     OR (r.allowed_approvers IS NOT NULL AND NOT p_approver = ANY (r.allowed_approvers))
     OR (p_decision = 'approve' AND r.step_up_required AND NOT p_step_up) THEN
    PERFORM _gxg_fail('GX403', 'approver is not allowed to decide this intent');
  END IF;
  BEGIN
    INSERT INTO decisions (decision_id, intent_id, tenant_id, decided_by, decision, intent_version, args_hash,
                           step_up, comment, decided_at)
    VALUES (gen_random_uuid(), p_intent, r.tenant_id, p_approver, p_decision, p_version, p_args_hash,
            p_step_up, p_comment, now());
  EXCEPTION WHEN unique_violation THEN PERFORM _gxg_fail('GX409', 'this approver has already decided');
  END;
  IF p_decision = 'reject' THEN
    r := _gxg_move(r, 'reject', NULL, 'rejected', p_approver);
    PERFORM _gxg_outbox('webhook', r);
    RETURN;
  END IF;
  approvals := gxg_received_approvals(p_intent);
  target := CASE WHEN approvals >= r.required_approvals THEN 'approved' ELSE 'pending_approval' END;
  r := _gxg_move(r, 'approve', NULL, target, p_approver, jsonb_build_object('approvals', approvals));
  IF target = 'approved' THEN
    UPDATE intents SET approved_at = now(), execution_deadline = now() + make_interval(secs => r.execution_window_s)
     WHERE intent_id = p_intent RETURNING * INTO r;
    PERFORM _gxg_outbox('execute', r);
    PERFORM _gxg_outbox('webhook', r);
  END IF;
END $$;

-- T18 (ui): manual outcome of an unresolved outcome_unknown.
CREATE FUNCTION gxg_resolve_manual(p_intent uuid, p_actor text, p_outcome text, p_version integer,
                                   p_reason text) RETURNS void {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent);
BEGIN
  IF p_outcome NOT IN ('executed', 'not_executed') OR length(coalesce(p_reason, '')) < 10 THEN
    PERFORM _gxg_fail('GX400', 'outcome executed|not_executed and a reason of at least 10 characters');
  END IF;
  IF r.version <> p_version THEN PERFORM _gxg_fail('GX409', 'intent version changed'); END IF;
  r := _gxg_move(r, 'resolve_manual', NULL,
                 CASE p_outcome WHEN 'executed' THEN 'reconciled_succeeded' ELSE 'reconciled_failed' END,
                 p_actor, jsonb_build_object('reason', p_reason));
  UPDATE executions SET state = CASE p_outcome WHEN 'executed' THEN 'reconciled_executed'
                                               ELSE 'reconciled_not_executed' END, completed_at = now()
   WHERE intent_id = p_intent AND state = 'outcome_unknown';
  PERFORM _gxg_outbox('webhook', r);
END $$;

-- T7 (executor).
CREATE FUNCTION gxg_reserve(p_intent uuid, p_executor text) RETURNS uuid {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent); e executions;
BEGIN
  PERFORM _gxg_check(r.status, 'reserve', NULL, 'executing');
  IF now() >= r.execution_deadline OR r.attempts >= r.max_attempts THEN
    PERFORM _gxg_fail('GX409', 'execution window or attempts exhausted');
  END IF;
  IF _gxg_blocked(r.tenant_id, r.tool_key) THEN PERFORM _gxg_fail('GX423', 'effects are disabled'); END IF;
  BEGIN
    INSERT INTO executions (execution_id, intent_id, attempt, state, reserved_by, reserved_at, reserve_lease_until)
    VALUES (gen_random_uuid(), p_intent,
            coalesce((SELECT max(attempt) FROM executions WHERE intent_id = p_intent), 0) + 1,
            'reserved', p_executor, now(), now() + interval '60 seconds')
    RETURNING * INTO e;
  EXCEPTION WHEN unique_violation THEN PERFORM _gxg_fail('GX409', 'intent already has a live execution');
  END;
  PERFORM _gxg_move(r, 'reserve', NULL, 'executing', p_executor, jsonb_build_object('execution_id', e.execution_id));
  RETURN e.execution_id;
END $$;

-- T9 (api, for the connector named in the execution token's aud). Returns the dispatch deadline.
CREATE FUNCTION gxg_dispatch(p_execution uuid, p_intent uuid, p_connector text, p_timeout_s integer)
  RETURNS timestamptz {DEFINER} AS $$
DECLARE x record := _gxg_execution(p_execution);
BEGIN
  IF (x.r).intent_id <> p_intent OR (x.r).connector_id <> p_connector THEN PERFORM _gxg_fail('GX404', 'execution not found'); END IF;
  PERFORM _gxg_check((x.r).status, 'dispatch', (x.e).state, 'executing');
  IF now() >= (x.e).reserve_lease_until THEN PERFORM _gxg_fail('GX409', 'reservation lease expired'); END IF;
  IF _gxg_blocked((x.r).tenant_id, (x.r).tool_key) THEN PERFORM _gxg_fail('GX423', 'effects are disabled'); END IF;
  UPDATE executions SET state = 'dispatching', dispatched_at = now(),
                        dispatch_lease_until = now() + make_interval(secs => p_timeout_s + 30)
   WHERE execution_id = p_execution;
  PERFORM _gxg_move(x.r, 'dispatch', (x.e).state, 'executing', 'connector:' || p_connector,
                    jsonb_build_object('execution_id', p_execution));
  UPDATE intents SET attempts = attempts + 1 WHERE intent_id = (x.r).intent_id;
  RETURN now() + make_interval(secs => p_timeout_s);
END $$;

-- T11, T12, T12a, T13 (executor).
CREATE FUNCTION gxg_complete(p_execution uuid, p_executor text, p_outcome text, p_provider_operation_id text,
                             p_receipt_ref text, p_error_code text) RETURNS void {DEFINER} AS $$
DECLARE x record := _gxg_execution(p_execution); r intents; event text; target text; new_state text; used integer;
BEGIN
  r := x.r;
  used := r.attempts + CASE WHEN p_outcome = 'refused_before_dispatch' THEN 1 ELSE 0 END;  -- T12a counts
  CASE p_outcome
    WHEN 'succeeded' THEN event := 'complete_succeeded'; target := 'succeeded'; new_state := 'succeeded';
    WHEN 'outcome_unknown' THEN event := 'complete_outcome_unknown'; target := 'outcome_unknown'; new_state := 'outcome_unknown';
    WHEN 'failed_before_effect', 'refused_before_dispatch' THEN
      event := CASE p_outcome WHEN 'failed_before_effect' THEN 'complete_failed_before_effect'
                              ELSE 'complete_refused_before_dispatch' END;
      new_state := 'failed_before_effect';
      target := CASE WHEN used < r.max_attempts AND now() < r.execution_deadline THEN 'approved' ELSE 'failed' END;
    ELSE PERFORM _gxg_fail('GX400', 'unknown outcome');
  END CASE;
  PERFORM _gxg_check(r.status, event, (x.e).state, target);
  UPDATE executions SET state = new_state, completed_at = now(), provider_operation_id = p_provider_operation_id,
                        provider_receipt_ref = p_receipt_ref, error_code = p_error_code
   WHERE execution_id = p_execution;
  r := _gxg_move(r, event, (x.e).state, target, p_executor, jsonb_build_object('execution_id', p_execution));
  UPDATE intents SET attempts = used WHERE intent_id = r.intent_id;
  PERFORM _gxg_outbox(CASE target WHEN 'approved' THEN 'execute' WHEN 'outcome_unknown' THEN 'reconcile'
                                  ELSE 'webhook' END, r);
END $$;

-- T15, T16, T17 (executor). p_retry_after_s: next reconcile for `unknown` (NULL: stop, manual review).
CREATE FUNCTION gxg_reconcile(p_intent uuid, p_executor text, p_result text, p_retry_after_s integer)
  RETURNS void {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent); target text;
BEGIN
  target := CASE p_result WHEN 'executed' THEN 'reconciled_succeeded' WHEN 'not_executed' THEN 'reconciled_failed'
                          WHEN 'unknown' THEN 'outcome_unknown' END;
  IF target IS NULL THEN PERFORM _gxg_fail('GX400', 'result executed|not_executed|unknown'); END IF;
  r := _gxg_move(r, 'reconcile_' || p_result, NULL, target, p_executor);
  IF p_result = 'unknown' THEN
    IF p_retry_after_s IS NOT NULL THEN PERFORM _gxg_outbox('reconcile', r, p_retry_after_s); END IF;
    RETURN;
  END IF;
  UPDATE executions SET state = CASE p_result WHEN 'executed' THEN 'reconciled_executed'
                                              ELSE 'reconciled_not_executed' END, completed_at = now()
   WHERE intent_id = p_intent AND state = 'outcome_unknown';
  PERFORM _gxg_outbox('webhook', r);
END $$;

-- Sweeper: T5, T8, T10, T14. Each refuses before its deadline.
CREATE FUNCTION gxg_expire_approval(p_intent uuid) RETURNS void {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent);
BEGIN
  PERFORM _gxg_check(r.status, 'expire_approval', NULL, 'expired');
  IF now() < r.expires_at THEN PERFORM _gxg_fail('GX409', 'not expired yet'); END IF;
  PERFORM _gxg_outbox('webhook', _gxg_move(r, 'expire_approval', NULL, 'expired', 'sweeper'));
END $$;

CREATE FUNCTION gxg_expire_execution(p_intent uuid) RETURNS void {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent);
BEGIN
  PERFORM _gxg_check(r.status, 'expire_execution', NULL, 'expired');
  IF now() < r.execution_deadline THEN PERFORM _gxg_fail('GX409', 'not expired yet'); END IF;
  PERFORM _gxg_outbox('webhook', _gxg_move(r, 'expire_execution', NULL, 'expired', 'sweeper'));
END $$;

CREATE FUNCTION gxg_release_reservation(p_execution uuid) RETURNS void {DEFINER} AS $$
DECLARE x record := _gxg_execution(p_execution);
BEGIN
  PERFORM _gxg_check((x.r).status, 'reserve_lease_expired', (x.e).state, 'approved');
  IF now() < (x.e).reserve_lease_until THEN PERFORM _gxg_fail('GX409', 'lease not expired yet'); END IF;
  UPDATE executions SET state = 'released', completed_at = now() WHERE execution_id = p_execution;
  PERFORM _gxg_outbox('execute', _gxg_move(x.r, 'reserve_lease_expired', (x.e).state, 'approved', 'sweeper',
                                           jsonb_build_object('execution_id', p_execution)));
END $$;

CREATE FUNCTION gxg_expire_dispatch(p_execution uuid) RETURNS void {DEFINER} AS $$
DECLARE x record := _gxg_execution(p_execution);
BEGIN
  PERFORM _gxg_check((x.r).status, 'dispatch_lease_expired', (x.e).state, 'outcome_unknown');
  IF now() < (x.e).dispatch_lease_until THEN PERFORM _gxg_fail('GX409', 'lease not expired yet'); END IF;
  UPDATE executions SET state = 'outcome_unknown' WHERE execution_id = p_execution;
  PERFORM _gxg_outbox('reconcile', _gxg_move(x.r, 'dispatch_lease_expired', (x.e).state, 'outcome_unknown',
                                             'sweeper', jsonb_build_object('execution_id', p_execution)));
END $$;

-- One sweeper pass; several sweepers do not block each other (SKIP LOCKED). Returns the number of moves.
-- A refused row is skipped (savepoint), so one bad row cannot stop every expiry.
CREATE FUNCTION gxg_sweep(p_limit integer) RETURNS integer {DEFINER} AS $$
DECLARE n integer := 0; id uuid;
BEGIN
  FOR id IN SELECT intent_id FROM intents WHERE status = 'pending_approval' AND expires_at <= now()
            ORDER BY expires_at LIMIT p_limit FOR UPDATE SKIP LOCKED LOOP
    BEGIN PERFORM gxg_expire_approval(id); n := n + 1; EXCEPTION WHEN SQLSTATE 'GX409' THEN NULL; END;
  END LOOP;
  FOR id IN SELECT intent_id FROM intents WHERE status = 'approved' AND execution_deadline <= now()
            ORDER BY execution_deadline LIMIT p_limit FOR UPDATE SKIP LOCKED LOOP
    BEGIN PERFORM gxg_expire_execution(id); n := n + 1; EXCEPTION WHEN SQLSTATE 'GX409' THEN NULL; END;
  END LOOP;
  FOR id IN SELECT e.execution_id FROM executions e JOIN intents i USING (intent_id)
            WHERE e.state = 'reserved' AND e.reserve_lease_until <= now()
            LIMIT p_limit FOR UPDATE SKIP LOCKED LOOP
    BEGIN PERFORM gxg_release_reservation(id); n := n + 1; EXCEPTION WHEN SQLSTATE 'GX409' THEN NULL; END;
  END LOOP;
  FOR id IN SELECT e.execution_id FROM executions e JOIN intents i USING (intent_id)
            WHERE e.state = 'dispatching' AND e.dispatch_lease_until <= now()
            LIMIT p_limit FOR UPDATE SKIP LOCKED LOOP
    BEGIN PERFORM gxg_expire_dispatch(id); n := n + 1; EXCEPTION WHEN SQLSTATE 'GX409' THEN NULL; END;
  END LOOP;
  RETURN n;
END $$;
""")

    # ---- privileges: AS-02, AS-27 (everything not granted here is denied) ----
    roles = ", ".join(ROLES)
    op.execute(f"""
DO $$ BEGIN EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
           EXECUTE format('GRANT CONNECT ON DATABASE %I TO {roles}', current_database()); END $$;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO {roles};
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

GRANT INSERT ON intents TO gxg_api;
GRANT SELECT ({_cols("args_ciphertext", "preview_ciphertext")}) ON intents TO gxg_api, gxg_sweeper;
GRANT SELECT ({_cols("args_ciphertext")}) ON intents TO gxg_ui;
GRANT SELECT ({_cols("preview_ciphertext")}) ON intents TO gxg_executor;
GRANT SELECT ON decisions TO gxg_ui, gxg_executor;
GRANT SELECT ON executions TO gxg_ui, gxg_executor, gxg_sweeper;
GRANT INSERT ON outbox TO gxg_api, gxg_ui, gxg_sweeper, gxg_admin;
GRANT SELECT, UPDATE (delivered_at, attempts) ON outbox TO gxg_executor, gxg_notifier;
GRANT SELECT ON switches TO gxg_api, gxg_ui, gxg_executor, gxg_sweeper;
GRANT SELECT, INSERT, UPDATE ON switches TO gxg_admin;
GRANT INSERT ON audit_events TO {roles};
GRANT SELECT, INSERT, UPDATE ON approver_links TO gxg_ui;
GRANT SELECT ON approver_links TO gxg_notifier;
GRANT USAGE ON SEQUENCE outbox_id_seq TO gxg_api, gxg_ui, gxg_sweeper, gxg_admin;
GRANT USAGE ON SEQUENCE audit_events_id_seq TO {roles};

GRANT EXECUTE ON FUNCTION gxg_record_auto_decision(uuid), gxg_received_approvals(uuid),
  gxg_cancel(uuid, text, text, integer), gxg_dispatch(uuid, uuid, text, integer) TO gxg_api;
GRANT EXECUTE ON FUNCTION gxg_decide(uuid, text, text, integer, text, boolean, text),
  gxg_resolve_manual(uuid, text, text, integer, text) TO gxg_ui;
GRANT EXECUTE ON FUNCTION gxg_reserve(uuid, text), gxg_complete(uuid, text, text, text, text, text),
  gxg_reconcile(uuid, text, text, integer) TO gxg_executor;
GRANT EXECUTE ON FUNCTION gxg_expire_approval(uuid), gxg_expire_execution(uuid), gxg_release_reservation(uuid),
  gxg_expire_dispatch(uuid), gxg_sweep(integer) TO gxg_sweeper;
""")


def downgrade() -> None:
    raise NotImplementedError("no downgrade: audit and intents are never dropped (AS-02, RET-02)")
