"""Stage C wiring of `demo` (M8): settings, Approval Service client, executor JWKS and fake provider.

demo keeps its own effects.py instead of gxg_connector_kit.effects (docs/backlog.md, M1.1); server.py is the
template's `with_effects: true` render with this module in place of the kit's (tests/toolkit/test_template.py).
Variables beyond the kit's Settings:
  DEMO_PROVIDER_DB        fake provider journal, SQLite in the volume (default /var/lib/demo/provider.sqlite3)
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

import httpx2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import mcp
from gxg_connector_kit._jwt import b64url_decode
from gxg_connector_kit.exec_token import ExecutorJwks
from gxg_connector_kit.mcp import ConfigError

from .effects import Approval
from .provider import Provider


@dataclass(frozen=True)
class Settings(mcp.Settings):
    """The kit's settings with the `DEMO_` prefix; stage C keeps M8's types (None when unset) and the provider DB."""

    PREFIX: ClassVar[str] = "DEMO"
    approval_url: str | None = None  # type: ignore[assignment]
    svc_key_file: Path | None = None  # type: ignore[assignment]
    provider_db: Path = Path("/var/lib/demo/provider.sqlite3")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        s = cls(
            profile=env.get("GXG_PROFILE", "production"),
            bundle_dir=Path(env.get("DEMO_BUNDLE_DIR", "/bundle")),
            bundle_keys=env.get("DEMO_BUNDLE_KEYS", ""),
            state_file=Path(env.get("DEMO_STATE_FILE", "/var/lib/demo/bundle-state.json")),
            test_issuer_key=env.get("DEMO_TEST_ISSUER_KEY") or None,
            test_controls=env.get("DEMO_TEST_CONTROLS") == "1",
            identity_keys=env.get("DEMO_IDENTITY_KEYS", ""),
            approval_url=env.get("DEMO_APPROVAL_URL") or None,
            approval_audience=env.get("DEMO_APPROVAL_AUDIENCE", "approval-api"),
            svc_key_file=Path(env["DEMO_SVC_KEY_FILE"]) if env.get("DEMO_SVC_KEY_FILE") else None,
            provider_db=Path(env.get("DEMO_PROVIDER_DB", "/var/lib/demo/provider.sqlite3")),
        )
        if s.approval_url is not None and s.svc_key_file is None:
            raise ConfigError("DEMO_APPROVAL_URL needs DEMO_SVC_KEY_FILE (service JWT key, AS-06)")
        if s.profile != "test" and (s.test_issuer_key is not None or s.test_controls):
            raise ConfigError("test issuer and test controls are allowed only with GXG_PROFILE=test")
        if s.test_issuer_key is not None and s.identity_keys:
            raise ConfigError("DEMO_TEST_ISSUER_KEY and DEMO_IDENTITY_KEYS are exclusive")
        return s


@dataclass(frozen=True)
class Wiring:
    """How stage C reaches the Approval Service: default TCP/TLS; tests pass in-process transports and a clock."""

    approval: httpx2.AsyncBaseTransport | None = None  # intents, dispatch
    jwks: httpx2.BaseTransport | None = None  # executor JWKS, fetched in a worker thread
    clock: Callable[[], float] = time.time  # JWKS cache age (AS-09)


def connect(settings: mcp.Settings, connector_id: str, wiring: Wiring) -> tuple[Approval, ExecutorJwks]:
    """Approval Service client (service JWT key from a mounted secret file) and the executor JWKS from `api`."""
    url = str(settings.approval_url)
    key = Ed25519PrivateKey.from_private_bytes(b64url_decode(Path(str(settings.svc_key_file)).read_text().strip()))
    jwks_client = httpx2.Client(base_url=url, transport=wiring.jwks)  # httpx timeouts: 5 s
    jwks = ExecutorJwks(
        lambda: jwks_client.get("/.well-known/gxg-exec-jwks.json").raise_for_status().content, now=wiring.clock
    )
    approval = Approval(httpx2.AsyncClient(base_url=url, transport=wiring.approval), key,
                        connector_id=connector_id, audience=settings.approval_audience)  # fmt: skip
    return approval, jwks


def provider(settings: mcp.Settings) -> Provider:
    """The fake provider and its journal (DEMO_PROVIDER_DB)."""
    return Provider(settings.provider_db if isinstance(settings, Settings) else Settings.provider_db)
