"""A software passkey for tests (ES256, attestation `none`): the JSON a browser's `toJSON()` sends. Shared by the
decider tests (approval/tests_db) and the ui tests."""

from __future__ import annotations

import base64
import json

PUBLIC_URL = "https://ui.example.test"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class SoftAuthenticator:
    """A software passkey (ES256, attestation `none`) producing the JSON the browser's `toJSON()` sends."""

    def __init__(
        self, rp_id: str = "ui.example.test", origin: str = PUBLIC_URL, user_verified: bool = True,
        zero_counter: bool = False,
    ) -> None:  # fmt: skip
        import os

        from cryptography.hazmat.primitives.asymmetric import ec

        self.rp_id, self.origin, self.user_verified, self.zero_counter = rp_id, origin, user_verified, zero_counter
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.count = 0

    def _client_data(self, kind: str, options_json: str, origin: str | None) -> bytes:
        challenge = json.loads(options_json)["challenge"]
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin}).encode()

    def register(self, options_json: str) -> str:
        import hashlib

        import cbor2

        numbers = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        flags = 0x45 if self.user_verified else 0x41  # UP | UV | AT
        auth = (
            hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big")
            + bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cbor2.dumps(cose)
        )  # fmt: skip
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        client = self._client_data("webauthn.create", options_json, None)
        response = {"clientDataJSON": _b64(client), "attestationObject": _b64(attestation)}
        cid = _b64(self.credential_id)
        return json.dumps({"id": cid, "rawId": cid, "type": "public-key", "response": response})

    def assertion(self, options_json: str, origin: str | None = None) -> str:
        import hashlib

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        self.count += 0 if self.zero_counter else 1
        auth = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([0x05 if self.user_verified else 0x01])
            + self.count.to_bytes(4, "big")
        )
        client = self._client_data("webauthn.get", options_json, origin)
        signature = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return json.dumps({"id": _b64(self.credential_id), "rawId": _b64(self.credential_id), "type": "public-key",
                           "response": {"clientDataJSON": _b64(client), "authenticatorData": _b64(auth),
                                        "signature": _b64(signature)}})  # fmt: skip
