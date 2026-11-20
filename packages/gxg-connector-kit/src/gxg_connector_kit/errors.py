"""GxG error envelope (MCC-05, schemas/error.schema.json)."""

from __future__ import annotations

from typing import Any, Literal, get_args

ErrorCode = Literal[
    "invalid_input",
    "unsupported_version",
    "unauthenticated",
    "permission_denied",
    "resource_forbidden",
    "not_found",
    "conflict",
    "effect_disabled",
    "rate_limited",
    "temporarily_unavailable",
    "timeout_before_start",
    "outcome_unknown",
    "internal_error",
]
ERROR_CODES: frozenset[str] = frozenset(get_args(ErrorCode))

# "Повторять?" column of the MCC error table. internal_error is retryable only for
# `read` and only in a limited way, so the caller opts in explicitly.
_RETRYABLE_BY_DEFAULT: frozenset[str] = frozenset({"rate_limited", "temporarily_unavailable", "timeout_before_start"})
_MESSAGE_MAX = 500


class GxgError(Exception):
    """A stable, machine-readable error. `to_dict()` is the MCP `structuredContent`."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        retry_after_s: int | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        if retryable is None:
            retryable = code in _RETRYABLE_BY_DEFAULT
        if code == "outcome_unknown" and retryable:
            raise ValueError("outcome_unknown is never retryable (MCC-05)")
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message[:_MESSAGE_MAX]
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.request_id = request_id
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.retry_after_s is not None:
            out["retry_after_s"] = self.retry_after_s
        if self.request_id is not None:
            out["request_id"] = self.request_id
        if self.details is not None:
            out["details"] = self.details
        return out
