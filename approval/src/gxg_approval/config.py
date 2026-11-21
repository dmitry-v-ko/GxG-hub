"""Per-process configuration from the environment (AS-28: each process reads only its own secrets).

Secrets are never values of environment variables: a variable ending in `_FILE` names a mounted secret
file (Docker/Kubernetes secrets). Everything else is public configuration.

| Variable | Processes | Secret |
| --- | --- | --- |
| `GXG_DB_DSN_FILE` | migrate (owner), api, sweeper — each its own role | yes |
| `GXG_SVC_KEY_FILE` | mcp (service JWT to `api`) | yes |
| `GXG_PAYLOAD_PUBLIC_KEYS_FILE` | api (encrypt only) | no |
| `GXG_EXEC_JWKS_FILE` | api (AS-09) | no |
| `GXG_BUNDLE_DIR`, `GXG_BUNDLE_KEYS`, `GXG_BUNDLE_STATE_FILE` | api | no |
| `GXG_SERVICE_ID`, `GXG_API_URL`, `GXG_API_AUDIENCE`, `GXG_LISTEN`, `GXG_SWEEP_INTERVAL_S` | as named | no |
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from gxg_connector_kit import _jwt
from gxg_connector_kit.grants import BundleError, BundleStore

log = logging.getLogger("gxg_approval")
BUNDLE_RELOAD_S = 60  # GR-13: a new bundle reaches the service well within 5 minutes


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing configuration: {name}")
    return value


def secret(name: str) -> str:
    """Content of the secret file named by `<name>_FILE`."""
    return Path(env(f"{name}_FILE")).read_text("utf-8").strip()


def signing_key(name: str) -> Ed25519PrivateKey:
    """Ed25519 seed, base64url without padding (the format of the M2 bundle signing key)."""
    return Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(secret(name)))


class JsonFile:
    """A JSON file re-read whenever it changes (rotation without restart, AS-10). A bad file keeps the last good."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stamp: int | None = None
        self._doc: Any = None
        self._lock = threading.Lock()
        self()  # the first read must succeed: fail at start, not at the first call

    def __call__(self) -> Any:
        with self._lock:
            try:
                stamp = self.path.stat().st_mtime_ns
                if stamp != self._stamp:
                    self._doc, self._stamp = json.loads(self.path.read_text("utf-8")), stamp
            except (OSError, ValueError) as exc:
                if self._stamp is None:
                    raise SystemExit(f"cannot read {self.path}: {exc}") from None
                log.warning("keeping the previous %s: %s", self.path.name, exc)
            return self._doc


def bundle_store(environ: Mapping[str, str] = os.environ) -> BundleStore:
    keys: dict[str, Ed25519PublicKey] = {
        kid: _jwt.parse_public_key(k) for kid, k in json.loads(environ["GXG_BUNDLE_KEYS"]).items()
    }
    store = BundleStore(keys, state_file=Path(environ["GXG_BUNDLE_STATE_FILE"]))
    directory = Path(environ["GXG_BUNDLE_DIR"])

    def reload() -> None:
        try:
            store.load(
                (directory / "grants-bundle.json").read_bytes(), (directory / "grants-bundle.json.sig").read_bytes()
            )
        except (OSError, BundleError) as exc:  # keep the previous bundle; /readyz reports it (GR-12)
            log.warning("bundle not loaded: %s", exc)

    def loop() -> None:
        while not threading.Event().wait(BUNDLE_RELOAD_S):
            reload()

    reload()
    threading.Thread(target=loop, name="bundle-reload", daemon=True).start()
    return store
