"""M10: signed decisions through the decider (AS-30), webhook events and deliveries (AS-21…AS-23), payload purge
(RET-01), metrics (approval-service-v1 «Наблюдаемость»), kill switch and passkey invites of the admin (AS-24, EX-13).

Decisions of reports/plans/M10-cycle-1.md, recorded as pending_owner_decision in reports/M10-PENDING-OWNER.md:
- the decision challenge is SHA-256(intent_id ‖ args_hash ‖ version ‖ decision) ‖ 16 random bytes; the row is the
  server-side freshness (5 min, AS-16) and single use of the assertion;
- a passkey is enrolled through the decider with a one-time operator invite; credentials without `enrolled_by`
  (written by the M9 ui) are refused;
- one webhook event per status change, written by a trigger (T1 included); the 0001 `_gxg_outbox('webhook', …)`
  calls become no-ops so that there is one source of events.

Revision ID: 0005
"""

from alembic import op

revision = "0005"
down_revision = "0004"

DEFINER = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp"
CHALLENGE_MAX_AGE = "interval '5 minutes'"  # AS-16, AS-30
TERMINAL = "('succeeded','failed','reconciled_succeeded','reconciled_failed','rejected','expired','cancelled')"
LATENCY_BUCKETS_S = (60, 300, 900, 3600, 4 * 3600, 24 * 3600)  # human decisions: minutes to a day (TTL 24 h)


def upgrade() -> None:
    op.execute("""
ALTER TABLE decisions ADD COLUMN webauthn_assertion jsonb;  -- AS-30: verifiable later; NULL for policy decisions
ALTER TABLE webauthn_credentials ADD COLUMN enrolled_by text;  -- operator of the invite; NULL: not via the decider

CREATE TABLE decider_challenges (
  challenge       bytea PRIMARY KEY,
  purpose         text NOT NULL CHECK (purpose IN ('decide', 'enroll')),
  human_principal text NOT NULL,
  intent_id       uuid,
  intent_version  integer,
  decision        text,
  args_hash       char(64),
  invite_hash     char(64),
  created_at      timestamptz NOT NULL,
  used_at         timestamptz
);

CREATE TABLE passkey_invites (
  token_hash      char(64) PRIMARY KEY,   -- sha256 of the token; the token itself is never stored
  human_principal text NOT NULL,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL,
  expires_at      timestamptz NOT NULL,
  used_at         timestamptz
);

CREATE TABLE webhook_deliveries (
  id            bigserial PRIMARY KEY,
  outbox_id     bigint NOT NULL,
  subscriber_id text NOT NULL,
  tenant_id     text NOT NULL,
  event         jsonb NOT NULL,           -- intent-event.schema.json: no args, no preview
  created_at    timestamptz NOT NULL,     -- of the event: AS-23 counts 24 h from here
  next_at       timestamptz NOT NULL,
  attempts      integer NOT NULL DEFAULT 0,
  last_status   text,
  delivered_at  timestamptz,
  dead_at       timestamptz,
  UNIQUE (outbox_id, subscriber_id)
);
CREATE INDEX webhook_pending ON webhook_deliveries (subscriber_id, id) WHERE delivered_at IS NULL AND dead_at IS NULL;
CREATE INDEX outbox_webhook_pending ON outbox (id) WHERE delivered_at IS NULL AND topic = 'webhook';
""")

    # ---- decider (AS-30) ----
    op.execute(f"""
-- The 0001 gxg_decide without its grant: the checks of T2, T3, T4 for a decision whose assertion the decider has
-- verified. Every human decision is a passkey decision now, so step_up is always true.
CREATE FUNCTION _gxg_decide(p_intent uuid, p_approver text, p_decision text, p_version integer, p_args_hash text,
                            p_comment text, p_assertion jsonb) RETURNS void {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent); approvals integer; target text;
BEGIN
  IF p_decision NOT IN ('approve', 'reject') THEN PERFORM _gxg_fail('GX400', 'decision must be approve or reject'); END IF;
  PERFORM _gxg_check(r.status, p_decision, NULL, CASE p_decision WHEN 'reject' THEN 'rejected' ELSE r.status END);
  IF r.version <> p_version OR r.canonical_args_hash <> p_args_hash OR now() >= r.expires_at THEN
    PERFORM _gxg_fail('GX409', 'stale decision: version, args hash or deadline');
  END IF;
  IF p_approver = r.requester_principal OR NOT p_approver LIKE 'oidc:%'
     OR NOT p_approver = ANY (coalesce(r.allowed_approvers, ARRAY[]::text[])) THEN
    PERFORM _gxg_fail('GX403', 'approver is not allowed to decide this intent');
  END IF;
  BEGIN
    INSERT INTO decisions (decision_id, intent_id, tenant_id, decided_by, decision, intent_version, args_hash,
                           step_up, comment, decided_at, webauthn_assertion)
    VALUES (gen_random_uuid(), p_intent, r.tenant_id, p_approver, p_decision, p_version, p_args_hash,
            true, p_comment, now(), p_assertion);
  EXCEPTION WHEN unique_violation THEN PERFORM _gxg_fail('GX409', 'this approver has already decided');
  END;
  IF p_decision = 'reject' THEN
    PERFORM _gxg_move(r, 'reject', NULL, 'rejected', p_approver);
    RETURN;
  END IF;
  approvals := gxg_received_approvals(p_intent);
  target := CASE WHEN approvals >= r.required_approvals THEN 'approved' ELSE 'pending_approval' END;
  r := _gxg_move(r, 'approve', NULL, target, p_approver, jsonb_build_object('approvals', approvals));
  IF target = 'approved' THEN
    UPDATE intents SET approved_at = now(), execution_deadline = now() + make_interval(secs => r.execution_window_s)
     WHERE intent_id = p_intent RETURNING * INTO r;
    PERFORM _gxg_outbox('execute', r);
  END IF;
END $$;
DROP FUNCTION gxg_decide(uuid, text, text, integer, text, boolean, text);  -- its EXECUTE was gxg_ui's

-- decider: a challenge for one decision of one human on the screen's version and args hash.
CREATE FUNCTION gxg_decision_challenge(p_human text, p_intent uuid, p_decision text, p_version integer,
                                       p_args_hash text) RETURNS bytea {DEFINER} AS $$
DECLARE r intents; c bytea;
BEGIN
  IF p_decision IS NULL OR p_decision NOT IN ('approve', 'reject') THEN
    PERFORM _gxg_fail('GX400', 'decision must be approve or reject');
  END IF;
  IF p_human IS NULL OR NOT p_human LIKE 'oidc:%' THEN PERFORM _gxg_fail('GX403', 'only a human decides'); END IF;
  SELECT * INTO r FROM intents WHERE intent_id = p_intent;
  IF NOT FOUND THEN PERFORM _gxg_fail('GX404', 'intent not found'); END IF;
  IF r.status <> 'pending_approval' OR r.version <> p_version OR r.canonical_args_hash <> p_args_hash THEN
    PERFORM _gxg_fail('GX409', 'stale decision: version, args hash or status');
  END IF;
  c := sha256(convert_to(p_intent::text || p_args_hash || p_version::text || p_decision, 'UTF8'))
       || decode(replace(gen_random_uuid()::text, '-', ''), 'hex');
  INSERT INTO decider_challenges (challenge, purpose, human_principal, intent_id, intent_version, decision, args_hash,
                                  created_at)
  VALUES (c, 'decide', p_human, p_intent, p_version, p_decision, p_args_hash, now());
  RETURN c;
END $$;

-- decider: the decision, after it verified the assertion's signature over p_challenge with the stored public key.
-- Here: the challenge is known, fresh and unused, the passkey was enrolled through the decider and belongs to the
-- human of the challenge, the counter grew (or both are zero), then T2/T3/T4 with the assertion stored.
CREATE FUNCTION gxg_decide(p_challenge bytea, p_credential_id bytea, p_sign_count bigint, p_comment text,
                           p_assertion jsonb) RETURNS void {DEFINER} AS $$
DECLARE ch decider_challenges; cr webauthn_credentials;
BEGIN
  SELECT * INTO ch FROM decider_challenges WHERE challenge = p_challenge AND purpose = 'decide' FOR UPDATE;
  IF NOT FOUND THEN PERFORM _gxg_fail('GX403', 'unknown challenge'); END IF;
  IF ch.used_at IS NOT NULL THEN PERFORM _gxg_fail('GX409', 'assertion already used'); END IF;
  IF ch.created_at < now() - {CHALLENGE_MAX_AGE} THEN PERFORM _gxg_fail('GX403', 'challenge older than 5 minutes'); END IF;
  SELECT * INTO cr FROM webauthn_credentials WHERE credential_id = p_credential_id FOR UPDATE;
  IF NOT FOUND OR cr.enrolled_by IS NULL OR cr.human_principal <> ch.human_principal THEN
    PERFORM _gxg_fail('GX403', 'passkey is not enrolled through the decider for this human');
  END IF;
  IF (p_sign_count <> 0 OR cr.sign_count <> 0) AND p_sign_count <= cr.sign_count THEN
    PERFORM _gxg_fail('GX403', 'signature counter did not grow');
  END IF;
  IF jsonb_typeof(p_assertion) IS DISTINCT FROM 'object'
     OR NOT p_assertion ?& ARRAY['credential_id', 'authenticator_data', 'client_data_json', 'signature'] THEN
    PERFORM _gxg_fail('GX400', 'assertion must carry credential_id, authenticator_data, client_data_json, signature');
  END IF;
  UPDATE decider_challenges SET used_at = now() WHERE challenge = p_challenge;
  UPDATE webauthn_credentials SET sign_count = p_sign_count WHERE credential_id = p_credential_id;
  PERFORM _gxg_decide(ch.intent_id, ch.human_principal, ch.decision, ch.intent_version, ch.args_hash, p_comment,
                      p_assertion);
END $$;

-- admin: a one-time passkey invite for a human (AS-30 «одноразовая ссылка оператора»).
CREATE FUNCTION gxg_passkey_invite(p_human text, p_token_hash text, p_operator text, p_ttl_s integer)
  RETURNS void {DEFINER} AS $$
BEGIN
  IF p_human IS NULL OR NOT p_human LIKE 'oidc:%' OR p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{{64}}$'
     OR p_operator IS NULL OR NOT p_operator LIKE 'oidc:%' OR p_ttl_s IS NULL OR p_ttl_s NOT BETWEEN 60 AND 86400 THEN
    PERFORM _gxg_fail('GX400', 'a human, a sha256 token hash, an operator and a ttl of 60 s to 24 h');
  END IF;
  INSERT INTO passkey_invites (token_hash, human_principal, created_by, created_at, expires_at)
  VALUES (p_token_hash, p_human, p_operator, now(), now() + make_interval(secs => p_ttl_s));
  INSERT INTO audit_events (at, actor, action, details)
  VALUES (now(), p_operator, 'admin_passkey_invite', jsonb_build_object('human', p_human));
END $$;

-- decider: a registration challenge for the human of a valid invite.
CREATE FUNCTION gxg_enroll_challenge(p_human text, p_token_hash text) RETURNS bytea {DEFINER} AS $$
DECLARE c bytea;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM passkey_invites WHERE token_hash = p_token_hash AND human_principal = p_human
                 AND used_at IS NULL AND expires_at > now()) THEN
    PERFORM _gxg_fail('GX403', 'no valid invite for this human');
  END IF;
  c := decode(replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''), 'hex');
  INSERT INTO decider_challenges (challenge, purpose, human_principal, invite_hash, created_at)
  VALUES (c, 'enroll', p_human, p_token_hash, now());
  RETURN c;
END $$;

-- decider: store the verified credential; the invite and the challenge are used once. Returns the human.
CREATE FUNCTION gxg_enroll(p_challenge bytea, p_credential_id bytea, p_public_key bytea, p_sign_count bigint)
  RETURNS text {DEFINER} AS $$
DECLARE ch decider_challenges; inv passkey_invites;
BEGIN
  SELECT * INTO ch FROM decider_challenges WHERE challenge = p_challenge AND purpose = 'enroll' FOR UPDATE;
  IF NOT FOUND THEN PERFORM _gxg_fail('GX403', 'unknown challenge'); END IF;
  IF ch.used_at IS NOT NULL THEN PERFORM _gxg_fail('GX409', 'registration already used'); END IF;
  IF ch.created_at < now() - {CHALLENGE_MAX_AGE} THEN PERFORM _gxg_fail('GX403', 'challenge older than 5 minutes'); END IF;
  SELECT * INTO inv FROM passkey_invites WHERE token_hash = ch.invite_hash FOR UPDATE;
  IF NOT FOUND OR inv.used_at IS NOT NULL OR inv.expires_at <= now() OR inv.human_principal <> ch.human_principal THEN
    PERFORM _gxg_fail('GX403', 'no valid invite for this human');
  END IF;
  UPDATE decider_challenges SET used_at = now() WHERE challenge = p_challenge;
  UPDATE passkey_invites SET used_at = now() WHERE token_hash = inv.token_hash;
  BEGIN
    INSERT INTO webauthn_credentials (credential_id, human_principal, public_key, sign_count, created_at, enrolled_by)
    VALUES (p_credential_id, ch.human_principal, p_public_key, p_sign_count, now(), inv.created_by);
  EXCEPTION WHEN unique_violation THEN PERFORM _gxg_fail('GX409', 'this passkey is already registered');
  END;
  INSERT INTO audit_events (at, actor, action, details)
  VALUES (now(), ch.human_principal, 'passkey_enrolled', jsonb_build_object('invited_by', inv.created_by));
  RETURN ch.human_principal;
END $$;
""")

    # ---- webhook events (AS-21, EFF-28): one per status change, from one place ----
    op.execute(f"""
CREATE FUNCTION _gxg_intent_event() RETURNS trigger {DEFINER} AS $$
BEGIN
  INSERT INTO outbox (topic, intent_id, payload, created_at, available_at)
  VALUES ('webhook', NEW.intent_id, jsonb_strip_nulls(jsonb_build_object(
            'event_id', gen_random_uuid(), 'type', 'intent.status_changed', 'intent_id', NEW.intent_id,
            'tenant_id', NEW.tenant_id, 'business_task_id', NEW.business_task_id, 'status', NEW.status,
            'version', NEW.version,
            'occurred_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))),
          now(), now());
  RETURN NULL;
END $$;
CREATE TRIGGER intents_created_event AFTER INSERT ON intents FOR EACH ROW EXECUTE FUNCTION _gxg_intent_event();
CREATE TRIGGER intents_status_event AFTER UPDATE OF status ON intents FOR EACH ROW
  WHEN (OLD.status IS DISTINCT FROM NEW.status) EXECUTE FUNCTION _gxg_intent_event();

-- The trigger is the only source of webhook events now; other topics unchanged (SEC-18: no args, no preview).
-- Only the trigger writes webhook events: a process role with INSERT on outbox (api, sweeper, admin) cannot forge one
-- for the notifier to sign. Not SECURITY DEFINER: current_user is the owner only inside the definer functions.
CREATE FUNCTION _gxg_outbox_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.topic = 'webhook' AND current_user <> (SELECT pg_get_userbyid(relowner) FROM pg_class
                                                 WHERE oid = 'outbox'::regclass) THEN
    RAISE EXCEPTION USING ERRCODE = 'GX403', MESSAGE = 'webhook events come from the status trigger only';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER outbox_webhook_guard BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION _gxg_outbox_guard();

CREATE OR REPLACE FUNCTION _gxg_outbox(p_topic text, r intents, p_delay_s integer DEFAULT 0) RETURNS void
  LANGUAGE sql AS $$
  INSERT INTO outbox (topic, intent_id, payload, created_at, available_at)
  SELECT p_topic, r.intent_id, jsonb_build_object('intent_id', r.intent_id, 'tenant_id', r.tenant_id,
         'status', r.status, 'version', r.version), now(), now() + make_interval(secs => p_delay_s)
   WHERE p_topic <> 'webhook'
$$;
""")

    # ---- purge (RET-01; outbox of M9 F-2), admin switch (EX-13, AS-24), metrics ----
    buckets = ", ".join(str(b) for b in LATENCY_BUCKETS_S)
    op.execute(f"""
-- sweeper: payload of intents terminal for p_days (30 by default, RET-01) → NULL, metadata kept; outcome_unknown is
-- not terminal and never purged. Delivered outbox rows (notify: title, requester) go after a day, finished webhook
-- deliveries after 7 days, used or old challenges, invites and link codes after a day. Returns purged intents.
CREATE FUNCTION gxg_purge(p_days integer, p_limit integer) RETURNS integer {DEFINER} AS $$
DECLARE n integer;
BEGIN
  IF p_days IS NULL OR p_days NOT BETWEEN 1 AND 90 THEN PERFORM _gxg_fail('GX400', 'retention 1 to 90 days'); END IF;
  WITH c AS (
    SELECT intent_id FROM intents
     WHERE status IN {TERMINAL} AND terminal_at < now() - make_interval(days => p_days) AND payload_purged_at IS NULL
     ORDER BY terminal_at LIMIT p_limit FOR UPDATE SKIP LOCKED
  ), done AS (
    UPDATE intents i SET args_ciphertext = NULL, preview_ciphertext = NULL, payload_purged_at = now()
      FROM c WHERE i.intent_id = c.intent_id RETURNING i.intent_id, i.tenant_id
  )
  INSERT INTO audit_events (at, actor, action, tenant_id, intent_id, details)
  SELECT now(), 'sweeper', 'payload_purged', tenant_id, intent_id, jsonb_build_object('retention_days', p_days)
    FROM done;
  GET DIAGNOSTICS n = ROW_COUNT;
  DELETE FROM outbox WHERE delivered_at < now() - interval '1 day';
  DELETE FROM webhook_deliveries WHERE coalesce(delivered_at, dead_at) < now() - interval '7 days';
  DELETE FROM decider_challenges WHERE created_at < now() - interval '1 day';
  DELETE FROM passkey_invites WHERE expires_at < now() - interval '1 day';
  DELETE FROM link_codes WHERE expires_at < now() - interval '1 day';
  RETURN n;
END $$;

-- admin: set or clear a kill switch; audited in the same transaction (AS-05 reads it in T1, T7, T9).
CREATE FUNCTION gxg_switch(p_scope text, p_blocked boolean, p_reason text, p_actor text) RETURNS void {DEFINER} AS $$
BEGIN
  IF p_scope IS NULL OR p_scope !~ '^(global|tenant:[a-z0-9][a-z0-9_-]{{0,62}}|tool:[a-z0-9][a-z0-9_-]{{0,62}}:[a-z0-9][a-z0-9._-]{{0,127}})$'
     OR p_blocked IS NULL OR length(coalesce(p_reason, '')) NOT BETWEEN 3 AND 500
     OR p_actor IS NULL OR NOT p_actor LIKE 'oidc:%' THEN
    PERFORM _gxg_fail('GX400', 'scope global|tenant:<id>|tool:<tenant>:<tool>, a reason of 3 to 500 characters');
  END IF;
  INSERT INTO switches (scope, blocked, reason, changed_by, changed_at) VALUES (p_scope, p_blocked, p_reason, p_actor, now())
  ON CONFLICT (scope) DO UPDATE SET blocked = excluded.blocked, reason = excluded.reason,
                                    changed_by = excluded.changed_by, changed_at = excluded.changed_at;
  INSERT INTO audit_events (at, actor, action, details)
  VALUES (now(), p_actor, CASE WHEN p_blocked THEN 'admin_switch_set' ELSE 'admin_switch_clear' END,
          jsonb_build_object('scope', p_scope, 'reason', p_reason));
END $$;

-- api /metrics: aggregates only; labels are tenant, tool, mode, outcome, scope, action, subscriber (no people).
CREATE FUNCTION gxg_metrics() RETURNS TABLE (name text, labels jsonb, value double precision)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT 'gxg_intents_created_total', jsonb_build_object('tenant', tenant_id, 'tool', tool_key, 'mode', approval_mode),
         count(*)::float8 FROM intents GROUP BY tenant_id, tool_key, approval_mode
  UNION ALL
  SELECT 'gxg_intents_pending', jsonb_build_object('tenant', tenant_id), count(*)::float8
    FROM intents WHERE status = 'pending_approval' GROUP BY tenant_id
  UNION ALL
  SELECT 'gxg_intents_outcome_unknown', jsonb_build_object('tenant', tenant_id), count(*)::float8
    FROM intents WHERE status = 'outcome_unknown' GROUP BY tenant_id
  UNION ALL
  -- monotonic per label (a counter): an execution counts under every outcome it reached, so a reconciled one stays
  -- under outcome_unknown too; released rows: by reason (T12b rate_limited is gxg_execute_deferred_total)
  SELECT 'gxg_executions_total', jsonb_build_object('tenant', i.tenant_id, 'tool', i.tool_key, 'outcome', o.outcome),
         count(*)::float8
    FROM executions e JOIN intents i USING (intent_id)
   CROSS JOIN LATERAL unnest(CASE WHEN e.state IN ('reconciled_executed', 'reconciled_not_executed')
                                  THEN ARRAY['outcome_unknown', e.state] ELSE ARRAY[e.state] END) AS o(outcome)
   WHERE e.state NOT IN ('reserved', 'dispatching') AND coalesce(e.error_code, '') <> 'rate_limited'
   GROUP BY i.tenant_id, i.tool_key, o.outcome
  UNION ALL
  SELECT 'gxg_execute_deferred_total', jsonb_build_object('tenant', i.tenant_id, 'tool', i.tool_key), count(*)::float8
    FROM executions e JOIN intents i USING (intent_id) WHERE e.state = 'released' AND e.error_code = 'rate_limited'
   GROUP BY i.tenant_id, i.tool_key
  UNION ALL  -- AS-12a alert: expired while the connector kept answering 429
  SELECT 'gxg_intents_expired_after_rate_limit', jsonb_build_object('tenant', i.tenant_id), count(*)::float8
    FROM intents i
   WHERE i.status = 'expired' AND (SELECT e.error_code FROM executions e WHERE e.intent_id = i.intent_id
                                   ORDER BY e.attempt DESC LIMIT 1) = 'rate_limited'
   GROUP BY i.tenant_id
  UNION ALL
  SELECT 'gxg_switch_blocked', jsonb_build_object('scope', scope), CASE WHEN blocked THEN 1 ELSE 0 END FROM switches
  UNION ALL
  SELECT 'gxg_admin_actions_total', jsonb_build_object('action', action), count(*)::float8
    FROM audit_events WHERE action LIKE 'admin\\_%' GROUP BY action
  UNION ALL
  SELECT 'gxg_webhook_dead', jsonb_build_object('subscriber', subscriber_id), count(*)::float8
    FROM webhook_deliveries WHERE dead_at IS NOT NULL GROUP BY subscriber_id
  UNION ALL
  SELECT 'gxg_webhook_pending', jsonb_build_object('subscriber', subscriber_id), count(*)::float8
    FROM webhook_deliveries WHERE delivered_at IS NULL AND dead_at IS NULL GROUP BY subscriber_id
  UNION ALL  -- histogram of human approval latency: cumulative buckets, then sum and count
  SELECT 'gxg_approval_latency_seconds_bucket', jsonb_build_object('tenant', tenant_id, 'tool', tool_key, 'le', le),
         count(*) FILTER (WHERE extract(epoch FROM approved_at - created_at) <= le)::float8
    FROM intents CROSS JOIN unnest(ARRAY[{buckets}]) AS le
   WHERE approval_mode = 'human' AND approved_at IS NOT NULL GROUP BY tenant_id, tool_key, le
  UNION ALL
  SELECT 'gxg_approval_latency_seconds_sum', jsonb_build_object('tenant', tenant_id, 'tool', tool_key),
         sum(extract(epoch FROM approved_at - created_at))::float8
    FROM intents WHERE approval_mode = 'human' AND approved_at IS NOT NULL GROUP BY tenant_id, tool_key
  UNION ALL
  SELECT 'gxg_approval_latency_seconds_count', jsonb_build_object('tenant', tenant_id, 'tool', tool_key),
         count(*)::float8
    FROM intents WHERE approval_mode = 'human' AND approved_at IS NOT NULL GROUP BY tenant_id, tool_key
$$;
""")

    # ---- privileges (AS-27; everything not granted is denied) ----
    op.execute("""
DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO gxg_decider', current_database()); END $$;
GRANT USAGE ON SCHEMA public TO gxg_decider;
GRANT SELECT (credential_id, human_principal, public_key, sign_count, enrolled_by) ON webauthn_credentials
  TO gxg_decider;
GRANT SELECT ON decider_challenges TO gxg_decider;
GRANT EXECUTE ON FUNCTION gxg_decision_challenge(text, uuid, text, integer, text),
  gxg_decide(bytea, bytea, bigint, text, jsonb), gxg_enroll_challenge(text, text),
  gxg_enroll(bytea, bytea, bytea, bigint) TO gxg_decider;
-- AS-30: the ui no longer writes passkeys or decides; it reads the public part to show it.
REVOKE INSERT, UPDATE ON webauthn_credentials FROM gxg_ui;
GRANT EXECUTE ON FUNCTION gxg_passkey_invite(text, text, text, integer), gxg_switch(text, boolean, text, text)
  TO gxg_admin;
GRANT EXECUTE ON FUNCTION gxg_purge(integer, integer) TO gxg_sweeper;
GRANT EXECUTE ON FUNCTION gxg_metrics() TO gxg_api;
GRANT SELECT, INSERT, UPDATE (next_at, attempts, last_status, delivered_at, dead_at) ON webhook_deliveries
  TO gxg_notifier;
GRANT USAGE ON SEQUENCE webhook_deliveries_id_seq TO gxg_notifier;
""")


def downgrade() -> None:
    raise NotImplementedError("no downgrade (AS-02, RET-02)")
