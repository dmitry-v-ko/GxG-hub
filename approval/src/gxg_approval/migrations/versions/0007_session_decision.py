"""M13 (owner decision 2026-11-24): AS-30a, EX-26: a decision without a passkey for an intent without step-up.

`gxg_decide_session(principal, intent, version, args_hash, decision, comment)`: `SECURITY DEFINER`, EXECUTE only for
`gxg_ui`. The same checks as the decider's decision (T2, T3, T4: status, version, args hash, deadline, the intent's
frozen `allowed_approvers`, never the requester), and it **refuses an intent with `step_up_required`**: those still go
through the decider with a passkey (AS-30). It writes `decisions` with `step_up = false`, no assertion, and
`decided_by = oidc:…`. `gxg_decide` (the decider's) stays out of `gxg_ui`'s reach.

Additive: nothing existing is changed or dropped.

Revision ID: 0007
"""

from alembic import op

revision = "0007"
down_revision = "0006"

DEFINER = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp"


def upgrade() -> None:
    op.execute(f"""
CREATE FUNCTION gxg_decide_session(p_approver text, p_intent uuid, p_version integer, p_args_hash text,
                                   p_decision text, p_comment text DEFAULT NULL) RETURNS void {DEFINER} AS $$
DECLARE r intents := _gxg_intent(p_intent); approvals integer; target text;
BEGIN
  IF p_decision IS NULL OR p_decision NOT IN ('approve', 'reject') THEN
    PERFORM _gxg_fail('GX400', 'decision must be approve or reject');
  END IF;
  IF r.step_up_required THEN
    PERFORM _gxg_fail('GX403', 'this intent needs a passkey decision (step-up)');
  END IF;
  PERFORM _gxg_check(r.status, p_decision, NULL, CASE p_decision WHEN 'reject' THEN 'rejected' ELSE r.status END);
  IF r.version <> p_version OR r.canonical_args_hash <> p_args_hash OR now() >= r.expires_at THEN
    PERFORM _gxg_fail('GX409', 'stale decision: version, args hash or deadline');
  END IF;
  IF p_approver IS NULL OR p_approver = r.requester_principal OR NOT p_approver LIKE 'oidc:%'
     OR NOT p_approver = ANY (coalesce(r.allowed_approvers, ARRAY[]::text[])) THEN
    PERFORM _gxg_fail('GX403', 'approver is not allowed to decide this intent');
  END IF;
  BEGIN
    INSERT INTO decisions (decision_id, intent_id, tenant_id, decided_by, decision, intent_version, args_hash,
                           step_up, comment, decided_at)
    VALUES (gen_random_uuid(), p_intent, r.tenant_id, p_approver, p_decision, p_version, p_args_hash,
            false, p_comment, now());
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
GRANT EXECUTE ON FUNCTION gxg_decide_session(text, uuid, integer, text, text, text) TO gxg_ui;
""")


def downgrade() -> None:
    raise NotImplementedError("no downgrade (AS-02, RET-02)")
