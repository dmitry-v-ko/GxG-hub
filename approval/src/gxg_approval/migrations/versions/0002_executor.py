"""Executor (M8): AS-10 release of every reservation when an executor key is removed; index of pending outbox rows.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"

DEFINER = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp"


def upgrade() -> None:
    op.execute(f"""
-- AS-10: a compromised executor key is removed from the JWKS, and every `reserved` execution is released at once:
-- the T10 move (exec released, intent approved, attempts unchanged, outbox `execute`) without waiting for the lease.
-- Lock order as everywhere: intent, then execution (_gxg_execution).
CREATE FUNCTION gxg_release_reserved(p_actor text) RETURNS integer {DEFINER} AS $$
DECLARE n integer := 0; id uuid; x record;
BEGIN
  FOR id IN SELECT execution_id FROM executions WHERE state = 'reserved' ORDER BY reserved_at LOOP
    x := _gxg_execution(id);
    CONTINUE WHEN (x.e).state <> 'reserved';  -- dispatched or released meanwhile
    UPDATE executions SET state = 'released', completed_at = now() WHERE execution_id = id;
    PERFORM _gxg_outbox('execute', _gxg_move(x.r, 'reserve_lease_expired', (x.e).state, 'approved', p_actor,
                        jsonb_build_object('execution_id', id, 'reason', 'executor key removed (AS-10)')));
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;
GRANT EXECUTE ON FUNCTION gxg_release_reserved(text) TO gxg_executor;

-- The executor and the notifier scan only undelivered rows.
CREATE INDEX outbox_pending ON outbox (id) WHERE delivered_at IS NULL;
""")


def downgrade() -> None:
    raise NotImplementedError("no downgrade (AS-02, RET-02)")
