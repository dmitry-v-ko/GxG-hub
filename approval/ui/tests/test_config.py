"""ui configuration (no database): secrets from files, preview keys, the signed bundle loader."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from kit_testing import BUNDLE_KID, BundleKit, format_public_key
from nacl.public import PrivateKey, SealedBox

from gxg_approval_ui import config


def test_env_and_secret(tmp_path: Path) -> None:
    (tmp_path / "s").write_text("  value\n")
    environ = {"A": "x", "B_FILE": str(tmp_path / "s"), "EMPTY": ""}
    assert config.env("A", environ) == "x"
    assert config.secret("B", environ) == "value"
    for name in ("MISSING", "EMPTY"):
        with pytest.raises(SystemExit, match=name):
            config.env(name, environ)


def test_preview_keys(tmp_path: Path) -> None:
    sk = PrivateKey.generate()
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"t-alpha": base64.b64encode(bytes(sk)).decode()}))
    boxes = config.preview_keys(path)
    sealed = SealedBox(sk.public_key).encrypt(b"preview")
    assert set(boxes) == {"t-alpha"} and boxes["t-alpha"].decrypt(sealed) == b"preview"
    path.write_text(json.dumps({"t-alpha": "not base64!"}))
    with pytest.raises(ValueError):
        config.preview_keys(path)


def test_bundle_store_loads_a_signed_bundle(tmp_path: Path) -> None:
    kit = BundleKit(tmp_path / "state.json")
    data, sig = kit.signed(built_at=time.time() - 60)
    (tmp_path / "b").mkdir()
    (tmp_path / "b/grants-bundle.json").write_bytes(data)
    (tmp_path / "b/grants-bundle.json.sig").write_bytes(sig)
    environ = {
        "GXG_BUNDLE_KEYS": json.dumps({BUNDLE_KID: format_public_key(kit.ci_key.public_key())}),
        "GXG_BUNDLE_STATE_FILE": str(tmp_path / "state.json"),
        "GXG_BUNDLE_DIR": str(tmp_path / "b"),
    }
    store = config.bundle_store(environ)
    assert store.readiness().ready and "t-alpha" in store.current.tenants


def test_bundle_store_without_a_bundle_is_not_ready(tmp_path: Path) -> None:
    kit = BundleKit(tmp_path / "state.json")
    environ = {
        "GXG_BUNDLE_KEYS": json.dumps({BUNDLE_KID: format_public_key(kit.ci_key.public_key())}),
        "GXG_BUNDLE_STATE_FILE": str(tmp_path / "state.json"),
        "GXG_BUNDLE_DIR": str(tmp_path / "missing"),
    }
    assert config.bundle_store(environ).readiness().ready is False
