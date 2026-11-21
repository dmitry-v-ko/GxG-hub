"""Payload encryption (RET-03, AS-28): libsodium sealed boxes per tenant and purpose.

Each tenant has two X25519 key pairs: `args` (private key only in `executor`) and `preview` (private
key only in `ui`). `api` holds the public keys and can only encrypt. Deleting a tenant's private keys
makes its payload unreadable (crypto-shredding).
shortcut: private keys come from per-process secret files, no KMS wrapping; add it when a KMS is chosen.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from nacl.public import PrivateKey, PublicKey, SealedBox

from gxg_connector_kit.errors import GxgError

Purpose = Literal["args", "preview"]


def _key(value: str) -> bytes:
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32:
        raise ValueError("X25519 key must be 32 bytes")
    return raw


class Encryptor:
    """`api`: encrypt-only. Keys file: `{"<tenant>": {"args": "<base64>", "preview": "<base64>"}}`."""

    def __init__(self, public_keys: Mapping[str, Mapping[str, str]]) -> None:
        self._boxes = {
            (tenant, purpose): SealedBox(PublicKey(_key(keys[purpose])))
            for tenant, keys in public_keys.items()
            for purpose in ("args", "preview")
        }

    @classmethod
    def from_file(cls, path: Path) -> Encryptor:
        return cls(json.loads(path.read_text("utf-8")))

    def seal(self, tenant: str, purpose: Purpose, data: bytes) -> bytes:
        box = self._boxes.get((tenant, purpose))
        if box is None:  # fail closed: never store a payload in clear
            raise GxgError("temporarily_unavailable", "no payload key for this tenant")
        return bytes(box.encrypt(data))


class Decryptor:
    """`executor` (args) and `ui` (preview): private keys of one purpose only."""

    def __init__(self, purpose: Purpose, private_keys: Mapping[str, str]) -> None:
        self.purpose = purpose
        self._boxes = {t: SealedBox(PrivateKey(_key(k))) for t, k in private_keys.items()}

    def open(self, tenant: str, ciphertext: bytes) -> bytes:
        box = self._boxes.get(tenant)
        if box is None:
            raise GxgError("temporarily_unavailable", "no payload key for this tenant")
        return bytes(box.decrypt(ciphertext))


def generate() -> tuple[str, str]:
    """(private, public) base64 key pair, for provisioning and tests."""
    sk = PrivateKey.generate()
    return base64.b64encode(bytes(sk)).decode(), base64.b64encode(bytes(sk.public_key)).decode()
