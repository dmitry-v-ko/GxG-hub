"""Executor keys (AS-09, AS-10, SEC-10): rotation files, key choice, JSON file reload, reconcile backoff (T17)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_approval import keys
from gxg_approval.config import JsonFile
from gxg_approval.executor import SigningKeys, reconcile_delay_s
from gxg_connector_kit import _jwt
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.exec_token import parse_jwks

T0 = 1_800_000_000.0


def sink(action: str, details: dict[str, Any]) -> None:
    """An audit writer that records nothing: the tests below that are not about the audit row."""


def seed(key: Ed25519PrivateKey) -> str:
    return _jwt.b64url_encode(key.private_bytes_raw())


def kid(key: Ed25519PrivateKey) -> str:
    return _jwt.thumbprint(key.public_key())


def test_rotate_publishes_first_and_activates_15_minutes_later(tmp_path: Path) -> None:
    keys_file, jwks_file = tmp_path / "keys.json", tmp_path / "jwks.json"
    first = keys.rotate(keys_file, jwks_file, lead_s=0, now=T0, audit=sink)
    second = keys.rotate(keys_file, jwks_file, now=T0, audit=sink)
    stored = json.loads(keys_file.read_text())
    assert [k["not_before"] for k in stored] == ["2027-01-15T08:00:00Z", "2027-01-15T08:15:00Z"]
    assert [keys.kid_of(k["seed"]) for k in stored] == [first, second]
    assert set(parse_jwks(jwks_file.read_bytes())) == {first, second}  # the JWKS the connectors load
    assert oct(keys_file.stat().st_mode & 0o777) == "0o600" and oct(jwks_file.stat().st_mode & 0o777) == "0o644"
    signing = SigningKeys(lambda: json.loads(keys_file.read_text()), lambda: T0 + 899)
    assert kid(signing.current()) == first
    assert kid(SigningKeys(lambda: stored, lambda: T0 + 900).current()) == second
    assert not list(tmp_path.glob("*.tmp"))


def test_remove_drops_the_kid_from_both_files(tmp_path: Path) -> None:
    keys_file, jwks_file = tmp_path / "keys.json", tmp_path / "jwks.json"
    a = keys.rotate(keys_file, jwks_file, lead_s=0, now=T0, audit=sink)
    b = keys.rotate(keys_file, jwks_file, lead_s=0, now=T0, audit=sink)
    keys.remove(keys_file, jwks_file, a, audit=sink)
    assert set(parse_jwks(jwks_file.read_bytes())) == {b}
    assert [keys.kid_of(k["seed"]) for k in json.loads(keys_file.read_text())] == [b]
    with pytest.raises(SystemExit, match="not in"):
        keys.remove(keys_file, jwks_file, a, audit=sink)


def test_remove_on_the_api_host_without_the_secret_file(tmp_path: Path) -> None:
    keys_file, jwks_file = tmp_path / "keys.json", tmp_path / "jwks.json"
    a = keys.rotate(keys_file, jwks_file, now=T0, audit=sink)
    keys_file.unlink()
    keys.remove(keys_file, jwks_file, a, audit=sink)
    assert json.loads(jwks_file.read_text()) == {"keys": []} and not keys_file.exists()


def test_signing_keys_choose_the_newest_active_key() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    doc = [
        {"seed": seed(b), "not_before": "2027-01-15T08:00:00Z"},
        {"seed": seed(a), "not_before": "2020-01-01T00:00:00Z"},
    ]
    assert kid(SigningKeys(lambda: doc, lambda: T0).current()) == kid(b)  # newest by not_before, not by position
    assert kid(SigningKeys(lambda: doc, lambda: T0 - 1).current()) == kid(a)
    with pytest.raises(GxgError) as err:
        SigningKeys(lambda: doc[:1], lambda: T0 - 1).current()
    assert err.value.code == "temporarily_unavailable"


def test_json_file_reloads_on_change_and_keeps_the_last_good(tmp_path: Path) -> None:
    f = tmp_path / "jwks.json"
    f.write_text('{"keys": [1]}')
    doc = JsonFile(f)
    assert doc() == {"keys": [1]}
    f.write_text('{"keys": [2]}')
    os.utime(f, ns=(1, 1))  # a different mtime, whatever the clock resolution
    assert doc() == {"keys": [2]}
    f.write_text("not json")
    os.utime(f, ns=(2, 2))
    assert doc() == {"keys": [2]}
    with pytest.raises(SystemExit):
        JsonFile(tmp_path / "missing.json")


@pytest.mark.parametrize(
    ("waited", "delay"), [(0, 5), (3, 5), (40, 40), (7200, 3600), (24 * 3600 - 1, 3600), (24 * 3600, None)]
)
def test_reconcile_backoff(waited: float, delay: int | None) -> None:
    """T17: the next reconcile after the time already waited (doubling), 5 s … 1 h; after 24 h a human (T18)."""
    assert reconcile_delay_s(waited) == delay


def test_rotate_without_now_uses_the_clock_and_remove_keeps_the_modes(tmp_path: Path) -> None:
    import time
    from datetime import datetime

    keys_file, jwks_file = tmp_path / "keys.json", tmp_path / "jwks.json"
    before = time.time()
    a = keys.rotate(keys_file, jwks_file, audit=sink)
    b = keys.rotate(keys_file, jwks_file, audit=sink)
    start = datetime.fromisoformat(json.loads(keys_file.read_text())[0]["not_before"]).timestamp()
    assert before + 900 - 1 <= start <= time.time() + 900
    keys.remove(keys_file, jwks_file, a, audit=sink)
    assert oct(keys_file.stat().st_mode & 0o777) == "0o600" and oct(jwks_file.stat().st_mode & 0o777) == "0o644"
    assert [k["kid"] for k in json.loads(jwks_file.read_text())["keys"]] == [b]


def test_no_active_key_message() -> None:
    with pytest.raises(GxgError) as err:
        SigningKeys(lambda: [], lambda: T0).current()
    assert err.value.to_dict() == {
        "code": "temporarily_unavailable",
        "message": "no executor signing key is active yet",
        "retryable": True,
    }


def test_not_before_is_utc_whatever_the_local_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        keys.rotate(tmp_path / "k.json", tmp_path / "j.json", lead_s=0, now=T0, audit=sink)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert json.loads((tmp_path / "k.json").read_text())[0]["not_before"] == "2027-01-15T08:00:00Z"


def test_the_seed_file_is_created_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-10: the temp file is private from its creation, not only after a chmod (no readable window)."""
    old = os.umask(0o022)
    monkeypatch.setattr(os, "chmod", lambda *_a, **_k: None)  # only the creation mode remains
    try:
        keys.rotate(tmp_path / "k.json", tmp_path / "j.json", now=T0, audit=sink)
    finally:
        os.umask(old)
    assert oct((tmp_path / "k.json").stat().st_mode & 0o777) == "0o600"


def test_rotate_audits_kid_and_the_jwks_hash_before_and_after(tmp_path: Path) -> None:
    """SEC-09: who and when come from the writer (the DB row); the details carry the kid and the hashes."""
    keys_file, jwks_file = tmp_path / "keys.json", tmp_path / "jwks.json"
    rows: list[tuple[str, dict[str, Any]]] = []
    first = keys.rotate(keys_file, jwks_file, lead_s=0, now=T0, audit=lambda a, d: rows.append((a, d)))
    before = hashlib.sha256(b'{"keys":[]}').hexdigest()  # no JWKS yet = an empty one
    after = hashlib.sha256(
        json.dumps(json.loads(jwks_file.read_text()), sort_keys=True, separators=(",", ":")).encode()
    )
    assert rows == [
        ("key_rotated", {"kid": first, "not_before": "2027-01-15T08:00:00Z", "jwks_sha256_before": before,
                         "jwks_sha256_after": after.hexdigest()})
    ]  # fmt: skip
    keys.rotate(keys_file, jwks_file, lead_s=0, now=T0, audit=lambda a, d: rows.append((a, d)))
    assert rows[1][1]["jwks_sha256_before"] == after.hexdigest()  # the chain: the next row starts where this ended


def test_remove_audits_kid_and_the_jwks_hash_before_and_after(tmp_path: Path) -> None:
    keys_file, jwks_file = tmp_path / "keys.json", tmp_path / "jwks.json"
    a = keys.rotate(keys_file, jwks_file, now=T0, audit=sink)
    b = keys.rotate(keys_file, jwks_file, now=T0, audit=sink)
    before = keys.jwks_sha256(json.loads(jwks_file.read_text()))
    rows: list[tuple[str, dict[str, Any]]] = []
    keys.remove(keys_file, jwks_file, a, audit=lambda x, d: rows.append((x, d)))
    after = keys.jwks_sha256(json.loads(jwks_file.read_text()))
    assert before != after
    assert rows == [("key_removed", {"kid": a, "jwks_sha256_before": before, "jwks_sha256_after": after})]
    with pytest.raises(SystemExit, match="not in"):
        keys.remove(keys_file, jwks_file, a, audit=lambda x, d: rows.append((x, d)))
    assert len(rows) == 1 and b  # a kid that is not there changes nothing and is not audited


def _refuse(action: str, details: dict[str, Any]) -> None:
    raise RuntimeError("audit unavailable")


def test_nothing_is_written_when_the_audit_row_cannot_be_written(tmp_path: Path) -> None:
    """The audit row first: a rotation or removal nobody can see in the audit does not happen."""
    keys_file, jwks_file = tmp_path / "keys.json", tmp_path / "jwks.json"
    with pytest.raises(RuntimeError, match="audit unavailable"):
        keys.rotate(keys_file, jwks_file, audit=_refuse)
    assert not keys_file.exists() and not jwks_file.exists()
    a = keys.rotate(keys_file, jwks_file, now=T0, audit=sink)
    stored = keys_file.read_bytes(), jwks_file.read_bytes()
    with pytest.raises(RuntimeError, match="audit unavailable"):
        keys.remove(keys_file, jwks_file, a, audit=_refuse)
    assert (keys_file.read_bytes(), jwks_file.read_bytes()) == stored
