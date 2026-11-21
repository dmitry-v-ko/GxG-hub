"""Alembic environment: the caller (gxg_approval.db.migrate) passes an open connection of the owner role."""

from alembic import context

connection = context.config.attributes["connection"]
context.configure(connection=connection, transaction_per_migration=True)
with context.begin_transaction():
    context.run_migrations()
