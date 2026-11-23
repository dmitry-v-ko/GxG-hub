"""Passkeys and decisions through the decider (AS-30, M10): the ui only relays.

Every decision (approve and reject) is signed by the approver's passkey over a challenge the decider issues for this
intent, version, args hash and decision (5 minutes, once). Enrollment needs a one-time operator invite. The ui
verifies nothing and writes nothing here: the decider checks the assertion and records the decision through
`gxg_decide`, which the ui's database role cannot call.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import psycopg

_SQL_ENROLLED = "SELECT 1 FROM webauthn_credentials WHERE human_principal = %s AND enrolled_by IS NOT NULL"
DECIDER_TIMEOUT_S = 10


class Decider(Protocol):
    """The decider's internal HTTP API: an httpx-style client with `base_url` (TestClient in tests)."""

    def post(self, url: str, *, json: Any, timeout: float = ...) -> Any: ...


class Unavailable(Exception):
    """The decider did not answer."""


def has_passkey(conn: psycopg.Connection[Any], human: str) -> bool:
    """A passkey enrolled through the decider (AS-17): the only kind a decision accepts."""
    return conn.execute(_SQL_ENROLLED, (human,)).fetchone() is not None


def relay(decider: Decider, path: str, body: dict[str, Any]) -> tuple[int, str]:
    """POST to the decider; (status, body text). A transport failure is Unavailable, never a decision."""
    try:
        r = decider.post(path, json=body, timeout=DECIDER_TIMEOUT_S)
    except Exception:  # connection refused, timeout, TLS: the decider's answer is unknown → nothing was decided here
        raise Unavailable from None
    return int(r.status_code), str(r.text)


def message(text: str) -> str:
    """The decider's refusal text (its own or a _gxg_fail message)."""
    try:
        return str(json.loads(text)["message"])[:300]
    except (ValueError, KeyError, TypeError):
        return "отказ"
