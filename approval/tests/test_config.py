"""AS-28 configuration: secrets from files only (the bundle part is in tests_db/test_config_bundle.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_approval import cli, config
from gxg_connector_kit import _jwt


def test_secret_comes_from_the_named_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "dsn").write_text("host=/run/pg user=gxg_api\n")
    monkeypatch.setenv("GXG_DB_DSN_FILE", str(tmp_path / "dsn"))
    assert config.secret("GXG_DB_DSN") == "host=/run/pg user=gxg_api"
    monkeypatch.delenv("GXG_DB_DSN_FILE")
    monkeypatch.setenv("GXG_DB_DSN", "host=/run/pg password=x")  # a secret as a plain value is never read
    with pytest.raises(SystemExit, match="GXG_DB_DSN_FILE"):
        config.secret("GXG_DB_DSN")


def test_signing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = Ed25519PrivateKey.generate()
    (tmp_path / "k").write_text(_jwt.b64url_encode(key.private_bytes_raw()))
    monkeypatch.setenv("GXG_SVC_KEY_FILE", str(tmp_path / "k"))
    assert config.signing_key("GXG_SVC_KEY").public_key() == key.public_key()


def test_mcp_refuses_to_start_without_a_real_identity_verifier() -> None:
    with pytest.raises(SystemExit, match="IdentityVerifier"):
        cli.main(["mcp"])
