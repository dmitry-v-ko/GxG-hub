"""AC-M1-08: log redaction (SEC-18)."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import svc_jwt
from gxg_connector_kit.redact import JWT_PLACEHOLDER, redact

JWT = svc_jwt.sign(Ed25519PrivateKey.generate(), iss="demo", aud="approval-api")
UNSIGNED = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0."

CASES: list[tuple[str, Any, Any]] = [
    ("text", {"event": "call", "text": "Привет, секрет"}, {"event": "call"}),
    ("body", {"body": "raw http body", "status": 200}, {"status": 200}),
    ("query", {"tool": "kb.documents.search", "query": "salary of Ivan"}, {"tool": "kb.documents.search"}),
    ("args", {"args": {"recipient": "alice", "text": "x"}, "ahs": "a" * 64}, {"ahs": "a" * 64}),
    ("token", {"token": "opaque-token-value", "kid": "k1"}, {"kid": "k1"}),
    ("authorization", {"authorization": "Basic dXNlcjpwYXNz"}, {}),
    ("case_insensitive", {"Authorization": "x", "TEXT": "y", "Body": "z", "ok": 1}, {"ok": 1}),
    (
        "nested",
        {"req": {"headers": {"authorization": "x", "accept": "json"}}},
        {"req": {"headers": {"accept": "json"}}},
    ),
    ("in_list", {"items": [{"text": "a", "id": 1}, {"text": "b", "id": 2}]}, {"items": [{"id": 1}, {"id": 2}]}),
    ("jwt_value", {"execution_token": JWT}, {"execution_token": JWT_PLACEHOLDER}),
    ("bearer_in_string", {"hdr": f"Bearer {JWT}"}, {"hdr": f"Bearer {JWT_PLACEHOLDER}"}),
    ("jwt_in_message", {"msg": f"rejected {JWT} at dispatch"}, {"msg": f"rejected {JWT_PLACEHOLDER} at dispatch"}),
    ("unsigned_jwt", {"assertion": UNSIGNED}, {"assertion": JWT_PLACEHOLDER}),
    ("jwt_in_list", {"tokens": [JWT, "plain"]}, {"tokens": [JWT_PLACEHOLDER, "plain"]}),
    ("two_jwts", {"m": f"{JWT} {JWT}"}, {"m": f"{JWT_PLACEHOLDER} {JWT_PLACEHOLDER}"}),
    (
        "scalars_kept",
        {"n": 1, "b": True, "none": None, "id": "int_01"},
        {"n": 1, "b": True, "none": None, "id": "int_01"},
    ),
    ("not_a_jwt", {"note": "eyJ is a prefix, a.b is not"}, {"note": "eyJ is a prefix, a.b is not"}),
    ("top_level_list", [{"token": "t"}, "x"], [{}, "x"]),
]


def test_at_least_15_cases() -> None:
    assert len(CASES) >= 15


@pytest.mark.parametrize(("record", "expected"), [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES])
def test_redact(record: Any, expected: Any) -> None:
    assert redact(record) == expected


def test_input_is_not_mutated() -> None:
    record = {"text": "x", "nested": {"token": "y"}}
    redact(record)
    assert record == {"text": "x", "nested": {"token": "y"}}
