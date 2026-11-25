"""RET-03, AS-28: api encrypts only; args and preview open with different private keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nacl.exceptions import CryptoError

from gxg_approval import crypto
from gxg_connector_kit.errors import GxgError


def keys() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    public: dict[str, dict[str, str]] = {"t-a": {}}
    private: dict[str, dict[str, str]] = {"t-a": {}}
    for purpose in ("args", "preview"):
        private["t-a"][purpose], public["t-a"][purpose] = crypto.generate()
    return public, private


def test_round_trip_and_separation(tmp_path: Path) -> None:
    public, private = keys()
    path = tmp_path / "public.json"
    path.write_text(json.dumps(public))
    enc = crypto.Encryptor.from_file(path)
    ct = enc.seal("t-a", "args", b"secret text")
    assert b"secret text" not in ct and enc.seal("t-a", "args", b"secret text") != ct  # randomised
    assert crypto.Decryptor("args", {"t-a": private["t-a"]["args"]}).open("t-a", ct) == b"secret text"
    with pytest.raises(CryptoError):  # the preview key (ui) cannot open args
        crypto.Decryptor("preview", {"t-a": private["t-a"]["preview"]}).open("t-a", ct)


def test_unknown_tenant_fails_closed() -> None:
    public, private = keys()
    with pytest.raises(GxgError) as err:
        crypto.Encryptor(public).seal("t-b", "args", b"x")
    assert err.value.code == "temporarily_unavailable"
    with pytest.raises(GxgError):
        crypto.Decryptor("args", {"t-a": private["t-a"]["args"]}).open("t-b", b"x")


def test_bad_key_material() -> None:
    for bad in ("not base64!", "AAAA"):
        with pytest.raises(ValueError):
            crypto.Encryptor({"t": {"args": bad, "preview": bad}})
