"""PostgreSQL access: migrations (Alembic, owner role) and mapping of the transition functions' errors."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from gxg_connector_kit.errors import ErrorCode, GxgError

# SQLSTATEs raised by _gxg_fail() in the migration → canonical error codes.
_CODES: dict[str, ErrorCode] = {
    "GX400": "invalid_input",
    "GX403": "permission_denied",
    "GX404": "not_found",
    "GX409": "conflict",
    "GX423": "effect_disabled",
}


def migrate(owner_dsn: str, revision: str = "head") -> None:
    """Upgrade the schema as the owner role (AS-27: not a process role)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(files("gxg_approval") / "migrations"))
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(owner_dsn), poolclass=NullPool)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


@contextmanager
def mapped_errors() -> Iterator[None]:
    """A refusal of a transition function becomes the canonical error; anything else propagates."""
    try:
        yield
    except psycopg.Error as exc:
        code = _CODES.get(exc.sqlstate or "")
        if code is None:
            raise
        message = exc.diag.message_primary or code
        raise GxgError(code, message[:200]) from None
