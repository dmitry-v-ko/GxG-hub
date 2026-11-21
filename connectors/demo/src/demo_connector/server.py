"""MCP server of `demo`: FastMCP over Streamable HTTP plus /healthz and /readyz (MCC-09).

Configuration (environment; no secrets, only public keys and paths):
  GXG_PROFILE             `test` enables the test identity issuer and test controls; anything else is production
  DEMO_BUNDLE_DIR         directory with grants-bundle.json and grants-bundle.json.sig (GR-11a)
  DEMO_BUNDLE_KEYS        `kid=ed25519:<base64url>[,kid=...]`: bundle verification keys (never from the bundle)
  DEMO_STATE_FILE         newest loaded `built_at`, survives restarts (GR-13a)
  DEMO_TEST_ISSUER_KEY    test profile only: public key of the test identity issuer
  DEMO_TEST_CONTROLS=1    test profile only: POST /_test/index/fail on 127.0.0.1
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from gxg_connector_kit import TokenError, parse_public_key
from gxg_connector_kit.grants import BundleError, BundleStore
from gxg_connector_kit.identity import IdentityVerifier, StubVerifier, VerifiedIdentity

from .backend import Backend
from .tools import Connector, errors

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

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        s = cls(
            profile=env.get("GXG_PROFILE", "production"),
            bundle_dir=Path(env.get("DEMO_BUNDLE_DIR", "/bundle")),
            bundle_keys=env.get("DEMO_BUNDLE_KEYS", ""),
            state_file=Path(env.get("DEMO_STATE_FILE", "/var/lib/demo/bundle-state.json")),
            test_issuer_key=env.get("DEMO_TEST_ISSUER_KEY") or None,
            test_controls=env.get("DEMO_TEST_CONTROLS") == "1",
        )
        if s.profile != "test" and (s.test_issuer_key is not None or s.test_controls):
            raise ConfigError("test issuer and test controls are allowed only with GXG_PROFILE=test")
        return s


class NoVerifier:
    """Production until the ContextForge identity verifier (ADR-0001, variant C) is in the kit: refuses every call."""

    def verify(self, headers: Mapping[str, str]) -> VerifiedIdentity:
        raise TokenError("no identity verifier configured")


def parse_keys(value: str) -> dict[str, Ed25519PublicKey]:
    keys = {}
    for item in filter(None, (p.strip() for p in value.split(","))):
        kid, sep, key = item.partition("=")
        if not sep or not kid:
            raise ConfigError("DEMO_BUNDLE_KEYS: expected kid=ed25519:<base64url>")
        try:
            keys[kid] = parse_public_key(key)
        except ValueError as exc:
            raise ConfigError(f"DEMO_BUNDLE_KEYS: {exc}") from None
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
    mcp = FastMCP("demo")
    for tool in connector.tools(verifier, refresh):
        mcp.add_tool(tool)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        # Read-only connector: backend (in-process fixtures) + valid bundle; no Approval Service (MCC-09).
        refresh()
        ready = store.readiness()
        if not ready.ready:
            log.warning(json.dumps({"event": "not_ready", "reasons": list(ready.reasons)}))
        # The reasons stay in the log: /readyz does not disclose configuration (MCC-09).
        return JSONResponse(
            {"status": "ready" if ready.ready else "not_ready"}, status_code=200 if ready.ready else 503
        )

    return mcp.http_app(stateless_http=True, json_response=True)


def build(settings: Settings, backend: Backend | None = None) -> tuple[Starlette, Connector]:
    backend = backend or Backend()
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    store = BundleStore(parse_keys(settings.bundle_keys), state_file=settings.state_file)
    files = BundleFiles(store, settings.bundle_dir)
    files.refresh()
    verifier: IdentityVerifier = (
        StubVerifier(CONNECTOR_ID, parse_public_key(settings.test_issuer_key))
        if settings.test_issuer_key is not None
        else NoVerifier()
    )
    connector = Connector(backend, store)
    return create_app(connector, verifier, files.refresh, store), connector


def configs(settings: Settings, app: Starlette, connector: Connector) -> list[uvicorn.Config]:
    out = [uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning")]  # noqa: S104
    if settings.test_controls:
        from .testctl import create_app as test_app  # noqa: PLC0415 - absent in the production image

        out.append(uvicorn.Config(test_app(connector.backend), host="127.0.0.1", port=8090))
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
