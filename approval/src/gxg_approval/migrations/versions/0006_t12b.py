"""M10 scope addition (orchestrator, 2026-11-23): T12b, AS-12a, EX-25 — a 429 `rate_limited` of `execute` before
dispatch defers the attempt without spending it.

- T12b `gxg_defer`: CAS on the execution (`reserved`, by the transition table); exec → `released`,
  `error_code = rate_limited`; intent → `approved`, `attempts` unchanged; an outbox `execute` task at
  now + clamp(retry_after_s, 5 s, 3600 s) plus 0-20 % jitter, none if that is past the execution deadline (T8).
- T7 only from a due outbox task: a new execution needs an undelivered `execute` row with `available_at ≤ now`.
- T10 (and any other release of a reservation without a reason): `error_code = reserve_lease_expired`.
A refused CAS (T12a, T12b) is a `conflict`; the executor then ignores the late answer and audits it.

Revision ID: 0006
"""

from alembic import op

from gxg_approval import transitions

revision = "0006"
down_revision = "0005"

DEFINER = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp"
MIN_DELAY_S, MAX_DELAY_S, JITTER = 5, 3600, 0.2  # AS-12a


def upgrade() -> None:
    op.execute(transitions.install_sql())  # adds the T12b row
    op.execute(f"""
CREATE FUNCTION gxg_defer(p_execution uuid, p_executor text, p_retry_after_s integer) RETURNS void {DEFINER} AS $$
DECLARE x record := _gxg_execution(p_execution); r intents; delay double precision;
BEGIN
  IF p_retry_after_s IS NULL OR p_retry_after_s < 1 THEN PERFORM _gxg_fail('GX400', 'retry_after_s >= 1'); END IF;
  PERFORM _gxg_check((x.r).status, 'complete_rate_limited', (x.e).state, 'approved');
  UPDATE executions SET state = 'released', completed_at = now(), error_code = 'rate_limited'
   WHERE execution_id = p_execution;
  r := _gxg_move(x.r, 'complete_rate_limited', (x.e).state, 'approved', p_executor,
                 jsonb_build_object('execution_id', p_execution, 'retry_after_s', p_retry_after_s));
  delay := least({MAX_DELAY_S}, greatest({MIN_DELAY_S}, p_retry_after_s)) * (1 + random() * {JITTER});
  IF now() + make_interval(secs => delay) < r.execution_deadline THEN
    PERFORM _gxg_outbox('execute', r, ceil(delay)::integer);
  END IF;  -- otherwise T8 closes it at the deadline
END $$;
GRANT EXECUTE ON FUNCTION gxg_defer(uuid, text, integer) TO gxg_executor;

-- T7 only from a due outbox task (approval-service-v1 T7).
CREATE FUNCTION _gxg_execution_guard() RETURNS trigger {DEFINER} AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM outbox WHERE intent_id = NEW.intent_id AND topic = 'execute'
                 AND delivered_at IS NULL AND available_at <= now()) THEN
    PERFORM _gxg_fail('GX409', 'no due execute task for this intent');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER executions_from_a_task BEFORE INSERT ON executions FOR EACH ROW
  EXECUTE FUNCTION _gxg_execution_guard();

-- T10 (gxg_release_reservation, AS-10 release): a release without a reason is an expired reservation lease.
CREATE FUNCTION _gxg_release_reason() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.error_code := 'reserve_lease_expired';
  RETURN NEW;
END $$;
CREATE TRIGGER executions_release_reason BEFORE UPDATE OF state ON executions FOR EACH ROW
  WHEN (OLD.state = 'reserved' AND NEW.state = 'released' AND NEW.error_code IS NULL)
  EXECUTE FUNCTION _gxg_release_reason();
""")


def downgrade() -> None:
    raise NotImplementedError("no downgrade (AS-02, RET-02)")
