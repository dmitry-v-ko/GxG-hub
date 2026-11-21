"""Bundle from disk for the processes (GR-12): signature checked, a bad bundle leaves the service not ready."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_approval import config


def test_bundle_store_from_disk(world: Any, tmp_path: Path) -> None:
    data, sig = world.signed
    (tmp_path / "grants-bundle.json").write_bytes(data)
    (tmp_path / "grants-bundle.json.sig").write_bytes(sig)
    environ = {
        "GXG_BUNDLE_DIR": str(tmp_path),
        "GXG_BUNDLE_STATE_FILE": str(tmp_path / "state.json"),
        "GXG_BUNDLE_KEYS": world.bundle_keys(),
    }
    assert config.bundle_store(environ).readiness().ready
    wrong = world.bundle_keys(Ed25519PrivateKey.generate())
    other = {**environ, "GXG_BUNDLE_KEYS": wrong, "GXG_BUNDLE_STATE_FILE": str(tmp_path / "s2")}
    assert not config.bundle_store(other).readiness().ready  # starts, but is not ready and refuses calls (GR-12)
