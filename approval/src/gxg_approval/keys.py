"""`gxg-approval keys rotate|remove`: executor signing keys and their JWKS (AS-09, AS-10, SEC-10).

Two files: the executor's secret key file (`[{"seed", "not_before"}]`, only the executor mounts it, AS-28) and the
public JWKS file that `api` serves at `/.well-known/gxg-exec-jwks.json`. `rotate` publishes a new key at least
15 minutes before its first use; `remove` drops a key from both files (after 24 h unused, or at once when
compromised: then every `reserved` execution is released as well, see `Executor.release_reserved`).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import _jwt, exec_token

PUBLISH_LEAD_S = 15 * 60  # AS-10


def _write(path: Path, doc: Any, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n", "utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)  # readers (JsonFile) never see a half-written file


def _read(path: Path, default: Any) -> Any:
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def kid_of(seed: str) -> str:
    return _jwt.thumbprint(Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(seed)).public_key())


def rotate(keys_file: Path, jwks_file: Path, *, lead_s: int = PUBLISH_LEAD_S, now: float | None = None) -> str:
    """Add a new key: in the JWKS now, used by the executor from `now + lead_s`. Returns its `kid`."""
    seed = secrets.token_bytes(32)
    key = Ed25519PrivateKey.from_private_bytes(seed)
    start = datetime.fromtimestamp((time.time() if now is None else now) + lead_s, UTC)
    jwks = _read(jwks_file, {"keys": []})
    jwks["keys"].append(exec_token.public_jwk(key.public_key()))
    _write(jwks_file, jwks, 0o644)  # first publish, then the executor may use it
    keys = _read(keys_file, [])
    keys.append({"seed": _jwt.b64url_encode(seed), "not_before": start.isoformat().replace("+00:00", "Z")})
    _write(keys_file, keys, 0o600)
    return _jwt.thumbprint(key.public_key())


def remove(keys_file: Path, jwks_file: Path, kid: str) -> None:
    """Drop `kid` from the JWKS first (connectors stop trusting it), then from the executor's key file."""
    jwks = _read(jwks_file, {"keys": []})
    if kid not in {k["kid"] for k in jwks["keys"]}:
        raise SystemExit(f"kid {kid} is not in {jwks_file}")
    _write(jwks_file, {**jwks, "keys": [k for k in jwks["keys"] if k["kid"] != kid]}, 0o644)
    if keys_file.exists():
        _write(keys_file, [k for k in _read(keys_file, []) if kid_of(k["seed"]) != kid], 0o600)
