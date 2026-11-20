"""Signed policy bundle, format GR-11a (shared with the M1 loader in gxg-connector-kit)."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from gxg_policy.lint import expand
from gxg_policy.load import Policies

BUNDLE = "grants-bundle.json"
SIG = BUNDLE + ".sig"
MAX_SAFE_INT = 2**53 - 1
BUNDLE_KEYS = {"schema_version", "bundle_version", "built_at", "tenants", "platform"}


class BundleError(Exception):
    pass


def _sorted(value: Any) -> Any:
    if isinstance(value, dict):
        # RFC 8785 §3.2.3: keys ordered by their UTF-16 code units.
        return {k: _sorted(value[k]) for k in sorted(value, key=lambda k: k.encode("utf-16-be"))}
    if isinstance(value, list):
        return [_sorted(v) for v in value]
    if isinstance(value, float) or (
        isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_SAFE_INT
    ):
        raise BundleError(f"not allowed in a bundle (float or unsafe int): {value!r}")
    if value is None or isinstance(value, str | int | bool):
        return value
    raise BundleError(f"unsupported value {value!r}")


def jcs(value: Any) -> bytes:
    """RFC 8785 for float-free JSON: Python's json escaping already matches JCS for strings and ints."""
    return json.dumps(_sorted(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def bundle_doc(pol: Policies, bundle_version: str, built_at: str) -> dict[str, Any]:
    if pol.errors:
        raise BundleError("policies have errors:\n" + "\n".join(map(str, pol.errors)))
    if pol.platform is None:
        raise BundleError("no platform registry under platform/")
    for tenant in pol.approval:
        if tenant not in pol.tenants:
            raise BundleError(f"approval policy for unknown tenant {tenant!r}")
    tenants = {}
    for tenant, (_, doc) in pol.tenants.items():
        grants = copy.deepcopy(doc)
        # GR-11a: groups expanded in grant subjects; the groups map stays for approval `requesters`/`approvers`.
        for g in grants["grants"]:
            g["subjects"] = expand(g["subjects"], grants.get("groups", {}))
        approval = pol.approval.get(tenant)
        tenants[tenant] = {"grants": grants, "approval_policy": approval[1] if approval else None}
    return {
        "schema_version": 1,
        "bundle_version": bundle_version,
        "built_at": built_at,
        "tenants": tenants,
        "platform": pol.platform[1],
    }


def sign(data: bytes, seed: bytes, kid: str) -> bytes:
    if len(seed) != 32:
        raise BundleError("signing key must be a 32-byte Ed25519 seed (base64url)")
    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(data)
    return jcs({"alg": "EdDSA", "kid": kid, "sig": b64url(sig)})


def public_key(seed: bytes) -> str:
    raw = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    return "ed25519:" + b64url(raw)


def build(pol: Policies, out: Path, *, bundle_version: str, built_at: str, seed: bytes, kid: str) -> None:
    data = jcs(bundle_doc(pol, bundle_version, built_at))
    out.mkdir(parents=True, exist_ok=True)
    (out / BUNDLE).write_bytes(data)
    (out / SIG).write_bytes(sign(data, seed, kid))


def verify(data: bytes, sig_file: bytes, keys: dict[str, str]) -> dict[str, Any]:
    """Check the signature over the exact bytes, then the bundle shape. `keys`: kid -> `ed25519:<b64url>`."""
    try:
        sig = json.loads(sig_file)
        if not isinstance(sig, dict) or sig.get("alg") != "EdDSA" or set(sig) != {"alg", "kid", "sig"}:
            raise BundleError("signature file: expected {alg: EdDSA, kid, sig}")
        if sig["kid"] not in keys:
            raise BundleError(f"unknown kid {sig['kid']!r}")
        key = keys[sig["kid"]].removeprefix("ed25519:")
        Ed25519PublicKey.from_public_bytes(unb64url(key)).verify(unb64url(sig["sig"]), data)
        doc = json.loads(data)
    except InvalidSignature as exc:
        raise BundleError("bad signature") from exc
    except (ValueError, TypeError) as exc:
        raise BundleError(f"malformed: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) != BUNDLE_KEYS or doc["schema_version"] != 1:
        raise BundleError("not a v1 bundle")
    if jcs(doc) != data:
        raise BundleError("bundle is not in JCS form")
    return doc
