"""AS-22, EX-20: the webhook signature a receiver checks."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from kit_testing import BundleKit

from gxg_connector_kit import grants, webhooks

SECRET = b"s" * 32
BODY = json.dumps({"event_id": "e-1", "status": "approved"}).encode()
T = 1_800_000_000


def test_header_is_the_spec_formula() -> None:
    mac = hmac.new(SECRET, f"{T}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert webhooks.sign(SECRET, BODY, T) == f"t={T},v1={mac}"
    assert webhooks.HEADER == "GxG-Signature" and webhooks.TOLERANCE_S == 300


def test_valid_delivery_once() -> None:
    seen: set[str] = set()
    header = webhooks.sign(SECRET, BODY, T)
    assert webhooks.verify(SECRET, header, BODY, now=T + 300, seen=seen) == {"event_id": "e-1", "status": "approved"}
    assert seen == {"e-1"}
    assert webhooks.verify(SECRET, header, BODY, now=T, seen=seen) is None  # a repeat: acknowledged, not handled
    with pytest.raises(webhooks.WebhookError) as err:  # a repeat is still checked first
        webhooks.verify(SECRET, header, BODY, now=T + 301, seen=seen)
    assert err.value.reason == "stale"


@pytest.mark.parametrize(
    ("header", "body", "now", "reason"),
    [
        (webhooks.sign(b"other", BODY, T), BODY, T, "signature"),
        (webhooks.sign(SECRET, BODY, T), BODY + b" ", T, "signature"),
        (webhooks.sign(SECRET, BODY, T).replace(f"t={T}", f"t={T + 1}"), BODY, T, "signature"),  # t is signed
        (webhooks.sign(SECRET, BODY, T), BODY, T + 301, "stale"),
        (webhooks.sign(SECRET, BODY, T), BODY, T - 301, "stale"),
        ("t=x,v1=00", BODY, T, "malformed"),
        ("v1=00", BODY, T, "malformed"),
        ("garbage", BODY, T, "malformed"),
        (webhooks.sign(SECRET, b"[]", T), b"[]", T, "malformed"),
        (webhooks.sign(SECRET, b"{}", T), b"{}", T, "malformed"),
        (webhooks.sign(SECRET, b"{x", T), b"{x", T, "malformed"),
    ],
)
def test_refusals(header: str, body: bytes, now: float, reason: str) -> None:
    seen: set[str] = set()
    with pytest.raises(webhooks.WebhookError) as err:
        webhooks.verify(SECRET, header, body, now=now, seen=seen)
    assert err.value.reason == reason and str(err.value) == reason
    assert seen == set()


def test_bundle_lists_webhook_subscribers(tmp_path: Path) -> None:
    kit = BundleKit(tmp_path / "state.json")
    kit.platform["webhooks"] = [
        {"id": "signals-alpha", "tenant": "t-alpha", "url": "https://signals.internal/h", "secret_ref": "secret://a/s",
         "business_task_prefix": "sig-"},
        {"id": "signals-beta", "tenant": "t-beta", "url": "https://b.internal/h", "secret_ref": "secret://b/s"},
    ]  # fmt: skip
    data, sig = kit.signed()
    bundle = grants.load_bundle(data, sig, kit.keys)
    assert bundle.webhooks == (
        grants.Webhook("signals-alpha", "t-alpha", "https://signals.internal/h", "secret://a/s", "sig-"),
        grants.Webhook("signals-beta", "t-beta", "https://b.internal/h", "secret://b/s", None),
    )
    alpha = grants.load_bundle(data, sig, kit.keys, tenant="t-alpha")
    assert [w.id for w in alpha.webhooks] == ["signals-alpha"]  # GR-12: a slice sees its own tenant only
    del kit.platform["webhooks"]
    assert grants.load_bundle(*kit.signed(), kit.keys).webhooks == ()
