#!/usr/bin/env python3
"""Backup and restore of the Approval Service database (OPS-03, OPS-04, RET-04).
Runbook: docs/runbooks/approval-backup.md

    GXG_BACKUP_KEY=... approval_backup.py backup  --dir DIR          # pg_dump -Fc, encrypted, then prune
    GXG_BACKUP_KEY=... approval_backup.py restore --file F --target DB  # into an EMPTY database, then RET-01 purge
    approval_backup.py prune --dir DIR                               # delete backups older than 35 days
    approval_backup.py check --dir DIR                               # exit 1 if one is older than 35 days (AC-M14-02)

The database is reached through the libpq environment (PGHOST, PGUSER, PGDATABASE, PGPASSWORD, ...): nothing secret
goes into argv. The dump never touches the disk unencrypted (pipe: pg_dump | AES-256-GCM | file). The key is a
32-byte base64url value in GXG_BACKUP_KEY, kept apart from the backups.
Backup age is the UTC time in the file name, not the mtime: a copied or synced file keeps its age.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import struct
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gxg_approval.sweeper import purge_once

KEEP_DAYS = 35  # RET-04
CHUNK = 16 * 1024 * 1024
MAGIC = b"GXGBK1\n"
NAME = re.compile(r"^gxg-approval-(\d{8}T\d{6}Z)\.dump\.enc$")
STAMP = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    pass


def key_from_env() -> bytes:
    raw = os.environ.get("GXG_BACKUP_KEY", "")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        key = b""
    if len(key) != 32:
        raise BackupError("GXG_BACKUP_KEY must be 32 bytes, base64url")
    return key


def _aad(index: int, last: bool) -> bytes:
    return struct.pack(">QB", index, last)  # a dropped, reordered or truncated chunk fails to authenticate


def encrypt(src: IO[bytes], dst: IO[bytes], key: bytes) -> None:
    aead, index = AESGCM(key), 0
    dst.write(MAGIC)
    block = src.read(CHUNK)
    while True:
        following = src.read(CHUNK)
        last = not following
        nonce = os.urandom(12)
        ct = aead.encrypt(nonce, block, _aad(index, last))
        dst.write(struct.pack(">I", len(ct)) + nonce + ct)
        if last:
            return
        block, index = following, index + 1


def decrypt(src: IO[bytes], dst: IO[bytes], key: bytes) -> None:
    if src.read(len(MAGIC)) != MAGIC:
        raise BackupError("not a GxG backup")
    aead, index = AESGCM(key), 0
    head = src.read(4)
    while head:
        if len(head) != 4:
            raise BackupError("backup is truncated")
        (size,) = struct.unpack(">I", head)
        nonce, ct = src.read(12), src.read(size)
        head = src.read(4)
        try:
            dst.write(aead.decrypt(nonce, ct, _aad(index, not head)))
        except InvalidTag:
            raise BackupError("backup is damaged, truncated or the key is wrong") from None
        index += 1
    if index == 0:
        raise BackupError("backup is truncated")


def backups(directory: Path) -> list[tuple[datetime, Path]]:
    found = [(m, p) for p in directory.iterdir() if (m := NAME.match(p.name))]
    return sorted((datetime.strptime(m.group(1), STAMP).replace(tzinfo=UTC), p) for m, p in found)


def stale(directory: Path, now: datetime) -> list[Path]:
    return [p for at, p in backups(directory) if now - at > timedelta(days=KEEP_DAYS)]


def prune(directory: Path, now: datetime) -> list[Path]:
    old = stale(directory, now)
    for p in old:
        p.unlink()
    return old


def backup(directory: Path, key: bytes, now: datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"gxg-approval-{now.strftime(STAMP)}.dump.enc"
    tmp = final.with_suffix(".part")
    dump = subprocess.Popen(["pg_dump", "--format=custom", "--no-password"], stdout=subprocess.PIPE)  # noqa: S607
    if dump.stdout is None:
        raise BackupError("pg_dump has no output")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            encrypt(dump.stdout, out, key)
        if dump.wait() != 0:
            raise BackupError("pg_dump failed")
        tmp.rename(final)  # a half-written file never looks like a backup
    finally:
        dump.kill()
        tmp.unlink(missing_ok=True)
    return final


def restore(file: Path, target: str, key: bytes, retention_days: int) -> dict[str, int]:
    """Into an empty database `target` (created by the owner, roles of approval/deploy/roles.sql present), then the
    RET-01 purge, so that a backup older than the retention window does not bring purged payload back."""
    with psycopg.connect(dbname=target, autocommit=True) as conn:
        if conn.execute("SELECT to_regclass('public.intents')").fetchone() != (None,):
            raise BackupError(f"database {target} is not empty: restore only onto a clean stand")
    cmd = ["pg_restore", "--no-owner", "--role=gxg_owner", "--exit-on-error", "--no-password", f"--dbname={target}"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)  # noqa: S603
    if proc.stdin is None:
        raise BackupError("pg_restore has no input")
    try:
        with file.open("rb") as src:
            decrypt(src, proc.stdin, key)
    except BrokenPipeError:
        pass  # pg_restore stopped early: its exit code below says why
    finally:
        proc.stdin.close()
    if proc.wait() != 0:
        raise BackupError("pg_restore failed")
    with psycopg.connect(dbname=target, autocommit=True) as conn:
        purged = purge_once(conn, retention_days)
        rows = conn.execute("SELECT status, count(*) FROM intents GROUP BY status").fetchall()
    return {"purged": purged, **{str(s): int(n) for s, n in rows}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("backup", "prune", "check"):
        sub.add_parser(name).add_argument("--dir", type=Path, required=True)
    rs = sub.add_parser("restore")
    rs.add_argument("--file", type=Path, required=True)
    rs.add_argument("--target", required=True, help="name of the empty database")
    rs.add_argument("--retention-days", type=int, default=int(os.environ.get("GXG_RETENTION_DAYS", "30")))
    args = ap.parse_args(argv)
    now = datetime.now(UTC)
    try:
        if args.cmd == "backup":
            path = backup(args.dir, key_from_env(), now)
            print(f"backup: {path.name}")
            for p in prune(args.dir, now):
                print(f"pruned: {p.name}")
        elif args.cmd == "prune":
            for p in prune(args.dir, now):
                print(f"pruned: {p.name}")
        elif args.cmd == "check":
            old = stale(args.dir, now)
            for p in old:
                print(f"FAIL older than {KEEP_DAYS} days: {p.name}", file=sys.stderr)
            print(f"{'FAIL' if old else 'PASS'}: {len(backups(args.dir)) - len(old)} backups within {KEEP_DAYS} days")
            return 1 if old else 0
        else:
            summary = restore(args.file, args.target, key_from_env(), args.retention_days)
            print("restored: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
