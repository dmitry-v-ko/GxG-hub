"""`gxg-approval keys rotate|remove`: executor signing keys and their JWKS (AS-09, AS-10, SEC-10).

Two files: the executor's secret key file (`[{"seed", "not_before"}]`, only the executor mounts it, AS-28) and the
public JWKS file that `api` serves at `/.well-known/gxg-exec-jwks.json`. `rotate` publishes a new key at least
15 minutes before its first use; `remove` drops a key from both files (after 24 h unused, or at once when
compromised: then `keys release` in the executor releases every `reserved` execution, AS-10).

SEC-09: every change is audited (kid, SHA-256 of the public JWKS before and after). `audit(action, details)` runs
before any file is touched, so a refused audit row (a DB error) leaves both files as they were; author and time are
added by the writer of the row (cli: `operator:<os user>`, `now()`).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import _jwt, exec_token

PUBLISH_LEAD_S = 15 * 60  # AS-10
Audit = Callable[[str, dict[str, Any]], None]


def _write(path: Path, doc: Any, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)  # O_CREAT applies `mode` only to a new file
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)  # the seed is never readable by others
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(doc).encode())
    os.chmod(tmp, mode)  # whatever the umask
    os.replace(tmp, path)  # readers (JsonFile) never see a half-written file


def kid_of(seed: str) -> str:
    return _jwt.thumbprint(Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(seed)).public_key())


def jwks_sha256(jwks: Any) -> str:
    """SEC-09: hash of the public JWKS document (canonical JSON), the 'before' and 'after' of an audit row."""
    return hashlib.sha256(json.dumps(jwks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def rotate(
    keys_file: Path, jwks_file: Path, *, audit: Audit, lead_s: int = PUBLISH_LEAD_S, now: float | None = None
) -> str:
    """Add a new key: in the JWKS now, used by the executor from `now + lead_s`. Returns its `kid`."""
    key = Ed25519PrivateKey.generate()
    start = datetime.fromtimestamp((time.time() if now is None else now) + lead_s, UTC)
    not_before = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    jwks = json.loads(jwks_file.read_bytes()) if jwks_file.exists() else {"keys": []}
    kid = _jwt.thumbprint(key.public_key())
    published = {**jwks, "keys": [*jwks["keys"], exec_token.public_jwk(key.public_key())]}
    audit(
        "key_rotated",
        {"kid": kid, "not_before": not_before, "jwks_sha256_before": jwks_sha256(jwks),
         "jwks_sha256_after": jwks_sha256(published)},
    )  # fmt: skip
    _write(jwks_file, published, 0o644)  # first publish, then the executor may use it
    keys = json.loads(keys_file.read_bytes()) if keys_file.exists() else []
    keys.append({"seed": _jwt.b64url_encode(key.private_bytes_raw()), "not_before": not_before})
    _write(keys_file, keys, 0o600)
    return kid


def remove(keys_file: Path, jwks_file: Path, kid: str, *, audit: Audit) -> None:
    """Drop `kid` from the JWKS first (connectors stop trusting it), then from the executor's key file."""
    jwks = json.loads(jwks_file.read_bytes())
    if kid not in {k["kid"] for k in jwks["keys"]}:
        raise SystemExit(f"kid {kid} is not in {jwks_file}")
    remaining = {**jwks, "keys": [k for k in jwks["keys"] if k["kid"] != kid]}
    audit(
        "key_removed",
        {"kid": kid, "jwks_sha256_before": jwks_sha256(jwks), "jwks_sha256_after": jwks_sha256(remaining)},
    )
    _write(jwks_file, remaining, 0o644)
    if keys_file.exists():  # the api host has only the JWKS
        _write(keys_file, [k for k in json.loads(keys_file.read_bytes()) if kid_of(k["seed"]) != kid], 0o600)
