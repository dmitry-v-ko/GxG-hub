"""AC-M14-09 (OPS-07, SEC-25), the Approval `api` part: the headers `tailscale serve` adds, and a tailnet address, are
not an identity. Without a valid service JWT every route answers 401 whatever they say, and nothing is written."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

FORGED = {
    "Tailscale-User-Login": "operator@example.com",
    "Tailscale-User-Name": "Operator",
    "Tailscale-User-Profile-Pic": "https://example.invalid/pic.png",
    "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers",
    "X-Forwarded-For": "100.64.0.7",
}
ROUTES = [
    ("POST", "/v1/intents"),
    ("GET", f"/v1/intents/{uuid.UUID(int=1)}?tenant_id=t-alpha&principal=agent:t-alpha:bot"),
    ("POST", f"/v1/intents/{uuid.UUID(int=1)}/cancel"),
    ("POST", f"/v1/executions/{uuid.UUID(int=2)}/dispatch"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize(
    "credential", ["none", "basic", "token of another audience", "forged token"], ids=lambda c: c.replace(" ", "-")
)
def test_forged_tailscale_headers_never_authenticate(
    client: TestClient,
    world: Any,
    owner: psycopg.Connection[Any],
    method: str,
    path: str,
    credential: str,
) -> None:
    auth = {
        "none": {},
        "basic": {"Authorization": "Basic b3BlcmF0b3I6cGFzcw=="},
        "token of another audience": world.auth("demo", aud="somebody-else"),
        "forged token": world.auth("demo", signer=Ed25519PrivateKey.generate()),
    }[credential]
    r = client.request(method, path, json=world.body() if method == "POST" else None, headers={**FORGED, **auth})
    assert r.status_code in (401, 403), (r.status_code, r.text)
    assert r.json()["code"] in ("unauthenticated", "permission_denied")
    assert owner.execute("SELECT count(*) FROM intents").fetchone() == (0,)


def test_a_valid_token_still_works_so_the_refusals_above_are_about_the_credential(
    client: TestClient, world: Any, owner: psycopg.Connection[Any]
) -> None:
    r = client.post("/v1/intents", json=world.body(), headers={**FORGED, **world.auth("demo")})
    assert r.status_code == 201, r.text
