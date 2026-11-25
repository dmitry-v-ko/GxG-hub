"""AC-M14-01/02 (OPS-03/04, RET-04), the part without a database: encryption of the dump and the 35-day rule."""

from __future__ import annotations

import base64
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.ops import approval_backup as ab

KEY = bytes(range(32))
NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


def seal(data: bytes) -> bytes:
    out = io.BytesIO()
    ab.encrypt(io.BytesIO(data), out, KEY)
    return out.getvalue()


def open_(blob: bytes, key: bytes = KEY) -> bytes:
    out = io.BytesIO()
    ab.decrypt(io.BytesIO(blob), out, key)
    return out.getvalue()


def test_roundtrip_over_several_chunks_and_nothing_readable_in_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ab, "CHUNK", 64)
    data = b"payload-MARKER-" * 40  # 600 bytes: ten chunks
    blob = seal(data)
    assert open_(blob) == data and b"MARKER" not in blob


def test_damage_truncation_reordering_and_a_wrong_key_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ab, "CHUNK", 64)
    blob = seal(bytes(300))
    size = 4 + 12 + 64 + 16  # length, nonce, a full chunk, tag
    head = len(ab.MAGIC)
    chunks = [blob[head + i * size : head + (i + 1) * size] for i in range(5)]
    for bad in (
        blob[:-size],  # the last chunks cut off
        blob[:head] + b"".join([chunks[1], chunks[0], *chunks[2:]]),  # reordered
        blob[:-1] + bytes([blob[-1] ^ 1]),  # flipped bit
        b"nope" + blob,
    ):
        with pytest.raises(ab.BackupError):
            open_(bad)
    with pytest.raises(ab.BackupError):
        open_(blob, bytes(32))


def test_key_must_be_32_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GXG_BACKUP_KEY", base64.urlsafe_b64encode(b"short").decode())
    with pytest.raises(ab.BackupError):
        ab.key_from_env()
    monkeypatch.setenv("GXG_BACKUP_KEY", base64.urlsafe_b64encode(KEY).decode())
    assert ab.key_from_env() == KEY


def name(at: datetime) -> str:
    return f"gxg-approval-{at.strftime(ab.STAMP)}.dump.enc"


def test_prune_goes_by_the_name_not_the_mtime_and_spares_other_files(tmp_path: Path) -> None:
    old, edge, fresh = (NOW - timedelta(days=d) for d in (36, 35, 1))
    for at in (old, edge, fresh):
        (tmp_path / name(at)).write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("keep")
    (tmp_path / name(old)).touch()  # a fresh mtime does not make an old backup young
    assert [p.name for p in ab.prune(tmp_path, NOW)] == [name(old)]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([name(edge), name(fresh), "notes.txt"])


def test_check_fails_on_an_old_backup_and_passes_without(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / name(datetime.now(UTC) - timedelta(days=2))).write_bytes(b"x")
    assert ab.main(["check", "--dir", str(tmp_path)]) == 0
    (tmp_path / name(datetime.now(UTC) - timedelta(days=40))).write_bytes(b"x")
    assert ab.main(["check", "--dir", str(tmp_path)]) == 1
    assert "FAIL older than 35 days" in capsys.readouterr().err
