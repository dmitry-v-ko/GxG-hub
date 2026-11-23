"""MCP server of `demo`: the kit's FastMCP plumbing (gxg_connector_kit.mcp) over Streamable HTTP, /healthz, /readyz.

Made from templates/python-fastmcp (scripts/new-connector); `copier update` keeps this file in step with the template.
Configuration: `gxg_connector_kit.mcp.Settings` with the `DEMO_` prefix (no secrets, only public keys and paths);
DEMO_TEST_CONTROLS=1 (test profile only) serves testctl.py on 127.0.0.1:8090.
Stage C (M8, effects.py; without DEMO_APPROVAL_URL the connector is read-only: no propose tool, no port 8081):
  DEMO_APPROVAL_URL       base URL of the Approval Service `api` (intents, dispatch, executor JWKS)
  DEMO_APPROVAL_AUDIENCE  its service ID in the platform registry (default approval-api)
  DEMO_SVC_KEY_FILE       secret file: Ed25519 seed (base64url) of this connector's service JWT key (AS-06)
  DEMO_PROVIDER_DB        fake provider journal, SQLite in the volume (default /var/lib/demo/provider.sqlite3)
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

import httpx2
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from starlette.applications import Starlette

from gxg_connector_kit import mcp
from gxg_connector_kit._jwt import b64url_decode
from gxg_connector_kit.exec_token import ExecutorJwks
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.mcp import ConfigError, NoVerifier, serve

from .backend import Backend
from .effects import PROPOSE, Approval, Effects
from .provider import Provider
from .tools import MANIFEST, Connector, logs

__all__ = ["ConfigError", "NoVerifier", "Settings", "Wiring", "build", "configs", "main", "parse_keys", "serve"]
CONNECTOR_ID = "demo"  # `aud` of identity assertions; id in the platform registry


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


def parse_keys(value: str, name: str = "DEMO_BUNDLE_KEYS") -> dict[str, Ed25519PublicKey]:
    return mcp.parse_keys(value, name)


@dataclass(frozen=True)
class Wiring:
    """How stage C reaches the Approval Service: default TCP/TLS; tests pass in-process transports and a clock."""

    approval: httpx2.AsyncBaseTransport | None = None  # intents, dispatch
    jwks: httpx2.BaseTransport | None = None  # executor JWKS, fetched in a worker thread
    clock: Callable[[], float] = time.time  # JWKS cache age (AS-09)


def build(
    settings: mcp.Settings, backend: Backend | None = None, wiring: Wiring = Wiring()
) -> tuple[Starlette, Connector]:
    store, files = mcp.bundle_store(settings, logs.startup)
    connector = Connector(backend or Backend(), store)
    if isinstance(settings, Settings) and settings.approval_url is not None and settings.svc_key_file is not None:
        connector.effects = effects(settings, connector.backend, store, files.refresh, wiring)
    tools = connector.tools(mcp.verifier(settings, CONNECTOR_ID), files.refresh)
    # MCC-09: backend + valid bundle; with effect tools also the Approval Service's executor JWKS (fetched from
    # `api`: both reachable). A read-only connector does not depend on it.
    parts = [store.readiness, connector.backend.readiness]
    if connector.effects is not None:
        parts.append(connector.effects.jwks.readiness)
    ready = mcp.readiness_of(*parts)
    return mcp.create_app(CONNECTOR_ID, tools, files.refresh, ready, logs.startup), connector


def effects(
    settings: Settings, backend: Backend, store: BundleStore, refresh: Callable[[], None], wiring: Wiring
) -> Effects:
    """Stage C wiring: Approval Service client (service JWT), executor JWKS from `api` (AS-09), fake provider."""
    url = str(settings.approval_url)
    key = Ed25519PrivateKey.from_private_bytes(b64url_decode((settings.svc_key_file or Path()).read_text().strip()))
    jwks_client = httpx2.Client(base_url=url, transport=wiring.jwks)  # httpx timeouts: 5 s
    jwks = ExecutorJwks(
        lambda: jwks_client.get("/.well-known/gxg-exec-jwks.json").raise_for_status().content, now=wiring.clock
    )
    approval = Approval(httpx2.AsyncClient(base_url=url, transport=wiring.approval), key,
                        connector_id=CONNECTOR_ID, audience=settings.approval_audience)  # fmt: skip
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    tool = next(t for t in manifest["tools"] if t["name"] == PROPOSE)
    provider = Provider(settings.provider_db)
    return Effects(backend, store, approval, provider=provider, jwks=jwks, tool=tool, refresh=refresh)


def configs(settings: mcp.Settings, app: Starlette, connector: Connector) -> list[uvicorn.Config]:
    out = [uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning")]  # noqa: S104
    if connector.effects is not None:  # SEC-14: the network lets only the executor reach this port
        out.append(uvicorn.Config(connector.effects.internal_app(), host="0.0.0.0", port=8081, log_level="warning"))  # noqa: S104
    if settings.test_controls:
        from .testctl import create_app as test_app  # noqa: PLC0415 - absent in the production image

        provider = connector.effects.provider if connector.effects else None
        out.append(uvicorn.Config(test_app(connector.backend, provider), host="127.0.0.1", port=8090))
    return out


def main(env: Mapping[str, str] = os.environ) -> int:
    def start() -> list[uvicorn.Config]:
        settings = Settings.from_env(env)
        return configs(settings, *build(settings))

    return mcp.run_main(start, lambda c: serve(c), logs)  # noqa: PLW0108 - looked up at call time: tests replace it
