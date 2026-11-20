"""Log redaction (SEC-18, EFF-21): message texts, payloads and tokens never reach logs."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = frozenset({"text", "body", "query", "args", "token", "authorization"})
JWT_PLACEHOLDER = "[jwt]"
# Compact JWS/JWT: base64url JSON header ("eyJ" = '{"'), payload, signature (may be empty for alg none).
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")


def redact(value: Any) -> Any:
    """Copy of a log record without sensitive keys (any depth, case-insensitive) and with JWTs masked in strings."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if not (isinstance(k, str) and k.lower() in SENSITIVE_KEYS)}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _JWT.sub(JWT_PLACEHOLDER, value)
    return value
