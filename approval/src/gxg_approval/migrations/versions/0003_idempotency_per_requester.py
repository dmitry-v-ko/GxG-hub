"""EFF-08 (owner decision 2026-11-21): idempotency per (tenant_id, requester_principal, idempotency_key).

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    # Each requester has its own key space: another agent's key can be neither taken nor read.
    op.execute("""
ALTER TABLE intents DROP CONSTRAINT intents_tenant_id_idempotency_key_key;
ALTER TABLE intents ADD CONSTRAINT intents_idempotency_per_requester UNIQUE (tenant_id, requester_principal, idempotency_key);
""")


def downgrade() -> None:
    raise NotImplementedError("no downgrade (AS-02, RET-02)")
