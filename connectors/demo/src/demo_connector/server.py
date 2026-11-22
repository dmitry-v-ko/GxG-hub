"""MCP server of `demo`: FastMCP over Streamable HTTP plus /healthz and /readyz (MCC-09).

Configuration (environment; no secrets, only public keys and paths):
  GXG_PROFILE             `test` enables the test identity issuer and test controls; anything else is production
  DEMO_BUNDLE_DIR         directory with grants-bundle.json and grants-bundle.json.sig (GR-11a)
  DEMO_BUNDLE_KEYS        `kid=ed25519:<base64url>[,kid=...]`: bundle verification keys (never from the bundle)
  DEMO_STATE_FILE         newest loaded `built_at`, survives restarts (GR-13a)
  DEMO_IDENTITY_KEYS      `kid=ed25519:<base64url>[,kid=...]`: ContextForge identity adapter keys (ADR-0001 C);
                          without them every call is refused
  DEMO_TEST_ISSUER_KEY    test profile only: public key of the test identity issuer
  DEMO_TEST_CONTROLS=1    test profile only: POST /_test/index/fail, provider modes on 127.0.0.1
Stage C (effects; without DEMO_APPROVAL_URL the connector is read-only: no propose tool, no port 8081):
  DEMO_APPROVAL_URL       base URL of the Approval Service `api` (intents, dispatch, executor JWKS)
  DEMO_APPROVAL_AUDIENCE  its service ID in the platform registry (default approval-api)
  DEMO_SVC_KEY_FILE       secret file: Ed25519 seed (base64url) of this connector's service JWT key (AS-06)
  DEMO_PROVIDER_DB        fake provider journal, SQLite in the volume (default /var/lib/demo/provider.sqlite3)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from gxg_connector_kit import TokenError, parse_public_key
from gxg_connector_kit._jwt import b64url_decode
from gxg_connector_kit.cf_identity import ContextForgeVerifier
from gxg_connector_kit.exec_token import ExecutorJwks
from gxg_connector_kit.grants import BundleError, BundleStore
from gxg_connector_kit.identity import IdentityVerifier, StubVerifier, VerifiedIdentity
from gxg_connector_kit.readiness import all_ready

from .backend import Backend
from .effects import PROPOSE, Approval, Effects
from .provider import Provider
from .tools import MANIFEST, Connector, errors

BUNDLE_FILE, SIG_FILE = "grants-bundle.json", "grants-bundle.json.sig"  # GR-11a
CONNECTOR_ID = "demo"  # `aud` of identity assertions; id in the platform registry
log = logging.getLogger("demo_connector")


class ConfigError(Exception):
    """The service must not start (AC-M3-11)."""


@dataclass(frozen=True)
class Settings:
    profile: str
    bundle_dir: Path
    bundle_keys: str
    state_file: Path
    test_issuer_key: str | None
    test_controls: bool
    identity_keys: str = ""
    approval_url: str | None = None
    approval_audience: str = "approval-api"
    svc_key_file: Path | None = None
    provider_db: Path = Path("/var/lib/demo/provider.sqlite3")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
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


class NoVerifier:
    """No identity adapter keys configured: refuses every call (fail closed)."""

    def verify(self, headers: Mapping[str, str], **_: Any) -> VerifiedIdentity:
        raise TokenError("no identity verifier configured")


def parse_keys(value: str, name: str = "DEMO_BUNDLE_KEYS") -> dict[str, Ed25519PublicKey]:
    keys = {}
    for item in filter(None, (p.strip() for p in value.split(","))):
        kid, sep, key = item.partition("=")
        if not sep or not kid:
            raise ConfigError(f"{name}: expected kid=ed25519:<base64url>")
        try:
            keys[kid] = parse_public_key(key)
        except ValueError as exc:
            raise ConfigError(f"{name}: {exc}") from None
    return keys


class BundleFiles:
    """Loads the bundle at start and again whenever its files change (GR-12). A bad file keeps the previous bundle."""

    def __init__(self, store: BundleStore, directory: Path) -> None:
        self.store = store
        self.directory = directory
        self._seen: tuple[int, int] | None = None

    def refresh(self) -> None:
        try:
            stamp = ((self.directory / BUNDLE_FILE).stat().st_mtime_ns, (self.directory / SIG_FILE).stat().st_mtime_ns)
        except OSError:
            return
        if stamp == self._seen:
            return
        self._seen = stamp
        try:
            bundle = self.store.load(
                (self.directory / BUNDLE_FILE).read_bytes(), (self.directory / SIG_FILE).read_bytes()
            )
            log.info(json.dumps({"event": "bundle_loaded", "bundle_version": bundle.version}))
        except (BundleError, OSError) as exc:
            log.warning(json.dumps({"event": "bundle_rejected", "reason": str(exc)}))


def create_app(
    connector: Connector, verifier: IdentityVerifier, refresh: Callable[[], None], store: BundleStore
) -> Starlette:
    effects = connector.effects
    mcp = FastMCP("demo")
    for tool in connector.tools(verifier, refresh):
        mcp.add_tool(tool)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        # MCC-09: backend (in-process fixtures) + valid bundle; with effect tools also the Approval Service's
        # executor JWKS (fetched from `api`: both reachable). A read-only connector does not depend on it.
        refresh()
        ready = all_ready(store.readiness(), *([effects.jwks.readiness()] if effects else []))
        if not ready.ready:
            log.warning(json.dumps({"event": "not_ready", "reasons": list(ready.reasons)}))
        # The reasons stay in the log: /readyz does not disclose configuration (MCC-09).
        return JSONResponse(
            {"status": "ready" if ready.ready else "not_ready"}, status_code=200 if ready.ready else 503
        )

    return mcp.http_app(stateless_http=True, json_response=True)


@dataclass(frozen=True)
class Wiring:
    """How stage C reaches the Approval Service: default TCP/TLS; tests pass Unix-socket transports and a clock."""

    approval: httpx2.AsyncBaseTransport | None = None  # intents, dispatch
    jwks: httpx2.BaseTransport | None = None  # executor JWKS, fetched in a worker thread
    clock: Callable[[], float] = time.time  # JWKS cache age (AS-09)


def build(settings: Settings, backend: Backend | None = None, wiring: Wiring = Wiring()) -> tuple[Starlette, Connector]:
    backend = backend or Backend()
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    store = BundleStore(parse_keys(settings.bundle_keys), state_file=settings.state_file)
    files = BundleFiles(store, settings.bundle_dir)
    files.refresh()
    verifier: IdentityVerifier = NoVerifier()
    if settings.test_issuer_key is not None:
        verifier = StubVerifier(CONNECTOR_ID, parse_public_key(settings.test_issuer_key))
    elif settings.identity_keys:
        verifier = ContextForgeVerifier(CONNECTOR_ID, parse_keys(settings.identity_keys, "DEMO_IDENTITY_KEYS"))
    connector = Connector(backend, store)
    if settings.approval_url is not None and settings.svc_key_file is not None:
        connector.effects = effects(settings, backend, store, files.refresh, wiring)
    return create_app(connector, verifier, files.refresh, store), connector


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


def configs(settings: Settings, app: Starlette, connector: Connector) -> list[uvicorn.Config]:
    out = [uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning")]  # noqa: S104
    if connector.effects is not None:  # SEC-14: the network lets only the executor reach this port
        out.append(uvicorn.Config(connector.effects.internal_app(), host="0.0.0.0", port=8081, log_level="warning"))  # noqa: S104
    if settings.test_controls:
        from .testctl import create_app as test_app  # noqa: PLC0415 - absent in the production image

        provider = connector.effects.provider if connector.effects else None
        out.append(uvicorn.Config(test_app(connector.backend, provider), host="127.0.0.1", port=8090))
    return out


def serve(configs: list[uvicorn.Config]) -> None:
    """Blocks until shut down."""

    async def run() -> None:
        await asyncio.gather(*(uvicorn.Server(c).serve() for c in configs))

    asyncio.run(run())


def main(env: Mapping[str, str] = os.environ) -> int:
    # JSON log lines (audit, events) to stdout; stack frames of internal errors to stderr only.
    out, err = logging.StreamHandler(sys.stdout), logging.StreamHandler()  # stderr
    out.addFilter(lambda r: r.name != errors.name)
    err.addFilter(lambda r: r.name == errors.name)
    for h in (out, err):
        h.setFormatter(logging.Formatter())  # the message only
    logging.basicConfig(level=logging.INFO, handlers=[out, err])
    try:
        settings = Settings.from_env(env)
        app, connector = build(settings)
    except (ConfigError, RuntimeError, ValueError, OSError) as exc:
        log.error(json.dumps({"event": "startup_refused", "reason": str(exc)}))
        return 2
    serve(configs(settings, app, connector))
    return 0
