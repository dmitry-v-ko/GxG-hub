"""Helpers for gxg-connector-kit tests. Every key is generated at test time; none is stored."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from gxg_connector_kit import _jwt, canonical

# First parent with uv.lock: also right from mutmut's `mutants/` copy of the package.
REPO = next(p for p in Path(__file__).resolve().parents if (p / "uv.lock").exists())
T0 = 1_800_000_000  # 2027-01-15T08:00:00Z, before the example grant expires (2027-03-01)
BUNDLE_KID = "ci-2026-11"


def format_public_key(key: Ed25519PublicKey) -> str:
    """Platform registry format `ed25519:<base64url>` (the registry is built by M2, not by the kit)."""
    return "ed25519:" + _jwt.b64url_encode(_jwt.raw_public_key(key))


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def load_yaml(rel: str) -> Any:
    return yaml.safe_load((REPO / rel).read_text(encoding="utf-8"))


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign_bundle(key: Ed25519PrivateKey, data: bytes, kid: str = BUNDLE_KID) -> bytes:
    """`grants-bundle.json.sig` per GR-11a."""
    return json.dumps({"alg": "EdDSA", "kid": kid, "sig": _jwt.b64url_encode(key.sign(data))}).encode()


class BundleKit:
    """Builds GR-11a bundles from the repository examples, signed with a fresh CI key."""

    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file  # GR-13a: BundleStore keeps the newest built_at here
        self.ci_key = Ed25519PrivateKey.generate()
        self.keys: dict[str, Ed25519PublicKey] = {BUNDLE_KID: self.ci_key.public_key()}
        self.service_keys: dict[str, Ed25519PrivateKey] = {}
        registry = load_yaml("policies/platform/registry.example.yaml")
        for s in registry["services"]:  # replace synthetic keys with real ones we can sign with
            self.service_keys[s["id"]] = Ed25519PrivateKey.generate()
            s["public_keys"] = [format_public_key(self.service_keys[s["id"]].public_key())]
        self.platform: dict[str, Any] = registry
        self.tenants: dict[str, Any] = {
            "t-alpha": {
                "grants": load_yaml("policies/tenants/example.yaml"),
                "approval_policy": load_yaml("policies/approval/example.yaml"),
            }
        }

    def data(self, *, built_at: float = T0, version: str = "a" * 40, **overrides: Any) -> bytes:
        doc = {
            "schema_version": 1,
            "bundle_version": version,
            "built_at": iso(built_at),
            "tenants": self.tenants,
            "platform": self.platform,
        }
        doc.update(overrides)
        return canonical.dumps(doc)

    def signed(self, **kw: Any) -> tuple[bytes, bytes]:
        data = self.data(**kw)
        return data, sign_bundle(self.ci_key, data)


def jws(signer: Ed25519PrivateKey | None, header: dict[str, Any], claims: dict[str, Any]) -> str:
    """Compact JWS with an arbitrary header; `signer=None` leaves the signature empty (alg none)."""
    h = _jwt.b64url_encode(json.dumps(header).encode())
    p = _jwt.b64url_encode(json.dumps(claims).encode())
    sig = _jwt.b64url_encode(signer.sign(f"{h}.{p}".encode())) if signer else ""
    return f"{h}.{p}.{sig}"


def hs256(header: dict[str, Any], claims: dict[str, Any]) -> str:
    """HMAC-signed token: must be rejected by alg, whatever the secret."""
    import hashlib
    import hmac

    h = _jwt.b64url_encode(json.dumps({**header, "alg": "HS256"}).encode())
    p = _jwt.b64url_encode(json.dumps(claims).encode())
    mac = hmac.new(b"k" * 32, f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_jwt.b64url_encode(mac)}"
