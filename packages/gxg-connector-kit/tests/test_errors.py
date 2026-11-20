"""MCC-05: the error envelope matches schemas/error.schema.json."""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest
from kit_testing import REPO

from gxg_connector_kit.errors import ERROR_CODES, GxgError

SCHEMA = json.loads((REPO / "schemas/error.schema.json").read_text(encoding="utf-8"))


def test_codes_are_the_schema_enum() -> None:
    assert set(SCHEMA["properties"]["code"]["enum"]) == ERROR_CODES


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_every_code_serialises_to_a_valid_envelope(code: str) -> None:
    err = GxgError(code, "m" * 600, retry_after_s=3, request_id="r-1", details={"k": 1})  # type: ignore[arg-type]
    out = err.to_dict()
    jsonschema.validate(out, SCHEMA)
    assert len(out["message"]) == 500


def test_minimal_envelope() -> None:
    assert GxgError("not_found", "no such chat").to_dict() == {
        "code": "not_found",
        "message": "no such chat",
        "retryable": False,
    }


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("rate_limited", True),
        ("temporarily_unavailable", True),
        ("timeout_before_start", True),
        ("outcome_unknown", False),
        ("internal_error", False),
        ("unauthenticated", False),
    ],
)
def test_default_retryable(code: str, retryable: bool) -> None:
    assert GxgError(code, "x").retryable is retryable  # type: ignore[arg-type]


def test_outcome_unknown_is_never_retryable() -> None:
    with pytest.raises(ValueError, match="never retryable"):
        GxgError("outcome_unknown", "x", retryable=True)


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        GxgError("forbidden", "x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kw",
    [
        {"retry_after_s": -1},
        {"retry_after_s": 86401},
        {"retry_after_s": 1.5},
        {"retry_after_s": True},
        {"request_id": ""},
        {"request_id": "r" * 257},
        {"details": {f"k{i}": i for i in range(21)}},
    ],
)
def test_values_outside_the_schema_are_rejected(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GxgError("rate_limited", "x", **kw)


def test_schema_bounds_are_accepted() -> None:
    for kw in (
        {"retry_after_s": 0, "request_id": "r"},
        {"retry_after_s": 86400, "request_id": "r" * 256, "details": {f"k{i}": i for i in range(20)}},
    ):
        jsonschema.validate(GxgError("rate_limited", "x", **kw).to_dict(), SCHEMA)  # type: ignore[arg-type]
