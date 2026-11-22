"""Signed test bundle (GR-11a) for one connector: its `fixtures/grants/*.yaml` and a generated platform registry.

shortcut: this repeats `gxg_policy.bundle.bundle_doc` for grants without groups, because `gxg_policy` reads
`schemas/` from a repository checkout and does not import from a git-installed wheel (a service in another
repository). tests/test_bundle.py compares the bytes with `gxg-policy build`; drop this module when gxg-policy
ships its schemas as package data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from gxg_connector_kit import _schema, canonical
from gxg_connector_kit._jwt import b64url_encode as b64url
from gxg_connector_kit.exec_token import public_jwk
from gxg_connector_kit.mcp import BUNDLE_FILE, SIG_FILE

TOOL_KEYS = ("name", "version", "effect_class", "action", "executes_via", "dispatch_timeout_s", "preview")
KID = "gxg-check"


def public(key: Ed25519PublicKey) -> str:
    return "ed25519:" + b64url(key.public_bytes_raw())


def grants(root: Path) -> list[dict[str, Any]]:
    docs = [
        yaml.safe_load(f.read_bytes()) for f in sorted((root / "fixtures" / "grants").glob("*.yaml"))
    ]
    if not docs:
        raise ValueError(f"no grants in {root}/fixtures/grants/")
    for d in docs:
        if d.get("groups"):
            raise ValueError("test bundle: groups are not supported, list the principals")  # see the shortcut above
    return docs


def registry(manifest: dict[str, Any], tenants: list[str], *, executor: Ed25519PublicKey, svc: Ed25519PublicKey,
             approval: Ed25519PublicKey) -> dict[str, Any]:  # fmt: skip
    """Executor, Approval Service `api` and the connector with the manifest's tools (AS-26)."""
    tools = [{k: t[k] for k in TOOL_KEYS if k in t} for t in manifest["tools"]]
    return {
        "schema_version": 1,
        "services": [
            {"id": "executor", "kind": "executor", "public_keys": [public(executor)], "tenants": ["*"]},
            {"id": "approval-api", "kind": "approval_api", "public_keys": [public(approval)], "tenants": ["*"]},
            {"id": manifest["connector"], "kind": "connector", "public_keys": [public(svc)], "tenants": sorted(tenants),
             "internal_url": f"https://{manifest['connector']}.internal:8081", "tools": tools},
        ],
    }  # fmt: skip


def write(out: Path, docs: list[dict[str, Any]], platform: dict[str, Any], key: Ed25519PrivateKey,
          *, built_at: datetime) -> None:  # fmt: skip
    """`grants-bundle.json` (JCS bytes) and `grants-bundle.json.sig` (`{alg, kid, sig}`, JCS) into `out`."""
    doc = {
        "schema_version": 1,
        "bundle_version": "0" * 40,
        "built_at": built_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenants": {d["tenant"]: {"grants": d, "approval_policy": None} for d in docs},
        "platform": platform,
    }
    error = next(_schema.validator("grants-bundle").iter_errors(doc), None)
    if error is not None:
        raise ValueError(f"test bundle: {error.message} at /{'/'.join(map(str, error.absolute_path))}")
    data = canonical.dumps(doc)
    sig = canonical.dumps({"alg": "EdDSA", "kid": KID, "sig": b64url(key.sign(data))})
    out.mkdir(parents=True, exist_ok=True)
    (out / BUNDLE_FILE).write_bytes(data)
    (out / SIG_FILE).write_bytes(sig)


def keys_setting(key: Ed25519PrivateKey) -> str:
    return f"{KID}={public(key.public_key())}"


def jwks(*keys: Ed25519PublicKey) -> bytes:
    return json.dumps({"keys": [public_jwk(k) for k in keys]}).encode()
