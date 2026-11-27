"""Log redaction (SEC-18, SEC-23, EFF-21): message texts, payloads, tokens, phones and photos never reach logs."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = frozenset({"text", "body", "query", "args", "token", "authorization"})
# SEC-23 (`pii`): phone, profile photos and their file names never reach a log, audit line or metric. A count
# (`photo_count`, `fields_returned`) is allowed; the value is not. Keys of the service's `person_candidate.v1`.
PII_KEYS = frozenset(
    {
        "phone",
        "phone_number",
        "photo",
        "photos",
        "photo_ref",
        "photo_refs",
        "file_name",
        "filename",
        "bytes",
        "photo_bytes",
    }
)
JWT_PLACEHOLDER = "[jwt]"
# Compact JWS/JWT: base64url JSON header ("eyJ" = '{"'), payload, signature (may be empty for alg none).
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
PHONE_PLACEHOLDER = "[phone]"
# An international number in free text (an error that echoes input): `+`, 7 to 15 digits, spaces or dashes inside.
_PHONE = re.compile(r"(?<![\w+])\+\d(?:[ -]?\d){6,14}(?!\d)")


_DROPPED = SENSITIVE_KEYS | PII_KEYS


def redact(value: Any) -> Any:
    """Copy of a log record without sensitive keys (any depth, case-insensitive) and with JWTs masked in strings."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if not (isinstance(k, str) and k.lower() in _DROPPED)}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _PHONE.sub(PHONE_PLACEHOLDER, _JWT.sub(JWT_PLACEHOLDER, value))
    return value
