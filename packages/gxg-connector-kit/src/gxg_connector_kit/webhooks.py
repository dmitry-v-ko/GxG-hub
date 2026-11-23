"""Webhook signature of AS-22, for the sender (notifier) and for every receiver (Signals, M12):
`GxG-Signature: t=<unix>,v1=<hex(HMAC-SHA256(secret, t + "." + body))>`. A receiver refuses a bad signature,
`|now - t| > 300 s` and a repeated `event_id` (EX-20). A repeat is acknowledged, not refused: the sender retries
an event whose 2xx was lost, and a refusal would block every later event of that subscriber (head of line).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import MutableSet
from typing import Any

HEADER = "GxG-Signature"
TOLERANCE_S = 300


class WebhookError(ValueError):
    """The delivery is refused: `reason` is signature, stale or malformed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _mac(secret: bytes, t: int, body: bytes) -> str:
    return hmac.new(secret, str(t).encode() + b"." + body, hashlib.sha256).hexdigest()


def sign(secret: bytes, body: bytes, t: int) -> str:
    """The header value for `body` sent at unix time `t`."""
    return f"t={t},v1={_mac(secret, t, body)}"


def verify(secret: bytes, header: str, body: bytes, *, now: float, seen: MutableSet[str]) -> dict[str, Any] | None:
    """The event of a valid new delivery, its `event_id` added to `seen` (the receiver's store of handled ids).
    None for a valid repeat of a handled `event_id`: answer 2xx and do not handle it again."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t, mac = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        raise WebhookError("malformed") from None
    if not hmac.compare_digest(mac, _mac(secret, t, body)):
        raise WebhookError("signature")
    if abs(now - t) > TOLERANCE_S:
        raise WebhookError("stale")
    try:
        event = json.loads(body)
        event_id = str(event["event_id"])
    except (ValueError, KeyError, TypeError):
        raise WebhookError("malformed") from None
    if event_id in seen:
        return None
    seen.add(event_id)
    return dict(event)
