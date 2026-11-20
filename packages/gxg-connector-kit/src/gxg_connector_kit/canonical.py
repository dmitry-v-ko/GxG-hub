"""Canonical JSON and hash of effect parameters (EFF-05).

Strings (values and keys) are NFC-normalised, floats are rejected, integers must be
within ±(2**53 - 1), then the value is serialised with RFC 8785 (JCS) and hashed with
SHA-256. Without floats JCS is: object keys sorted by UTF-16 code units, no
whitespace, and the same minimal string escaping that `json.dumps(ensure_ascii=False)`
produces. Vectors: schemas/test-vectors/canonical-hash.json.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from .errors import GxgError

MAX_SAFE_INT = 2**53 - 1


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be")


def _str(value: str) -> str:
    out = unicodedata.normalize("NFC", value)
    try:
        out.encode("utf-8")
    except UnicodeEncodeError:
        raise GxgError("invalid_input", "string is not valid Unicode (lone surrogate)") from None
    return out


def normalize(value: Any) -> Any:
    """Return the value in canonical form (NFC strings, JCS key order) or raise `invalid_input`."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise GxgError("invalid_input", "integer outside ±(2^53-1)")
        return value
    if isinstance(value, float):
        raise GxgError("invalid_input", "floats are not allowed; use integer minor units or strings")
    if isinstance(value, str):
        return _str(value)
    if isinstance(value, list | tuple):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        items: dict[str, Any] = {}
        for key, val in value.items():
            if not isinstance(key, str):
                raise GxgError("invalid_input", "object keys must be strings")
            nkey = _str(key)
            if nkey in items:
                raise GxgError("invalid_input", "duplicate key after NFC normalisation")
            items[nkey] = normalize(val)
        return {k: items[k] for k in sorted(items, key=_utf16_key)}
    raise GxgError("invalid_input", f"unsupported JSON type: {type(value).__name__}")


def dumps(value: Any) -> bytes:
    """Canonical UTF-8 bytes (JCS) of the normalised value."""
    return json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def hash(value: Any) -> str:  # shadows the builtin on purpose: the API is canonical.hash() (AC-M1-01)
    """Lower-case hex SHA-256 of `dumps(value)`."""
    return hashlib.sha256(dumps(value)).hexdigest()
