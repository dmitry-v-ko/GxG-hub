"""MCP server of the Telegram connector: FastMCP over Streamable HTTP plus /healthz and /readyz (MCC-09).

Configuration (environment; no secrets, only public keys, paths and ids):
  GXG_PROFILE               `test` allows the fake service, the test identity issuer and test controls
  TELEGRAM_SERVICE          `fake` (test profile only). There is no network adapter to the existing service yet
                            (reports/M11-ESCALATION.md): any other value refuses to start
  TELEGRAM_CONNECTIONS      `tenant=hub_connection:service_connection[,...]`: the Connection used for a verified
                            tenant (MCC-03); a tenant without one reads nothing
  TELEGRAM_BUNDLE_DIR       directory with grants-bundle.json and grants-bundle.json.sig (GR-11a)
  TELEGRAM_BUNDLE_KEYS      `kid=ed25519:<base64url>[,...]`: bundle verification keys (never from the bundle)
  TELEGRAM_STATE_FILE       newest loaded `built_at`, survives restarts (GR-13a)
  TELEGRAM_IDENTITY_KEYS    `kid=ed25519:<base64url>[,...]`: ContextForge identity adapter keys (ADR-0001 C);
                            without them every call is refused
  TELEGRAM_TEST_ISSUER_KEY  test profile only: public key of the test identity issuer
  TELEGRAM_TEST_CONTROLS=1  test profile only: the fake service's controls on 127.0.0.1:8090
shortcut: Settings, parse_keys, BundleFiles and NoVerifier repeat demo's server.py; into gxg-connector-kit with
the connector template.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from gxg_connector_kit import TokenError, parse_public_key
from gxg_connector_kit.cf_identity import ContextForgeVerifier
from gxg_connector_kit.grants import BundleError, BundleStore
from gxg_connector_kit.identity import IdentityVerifier, StubVerifier, VerifiedIdentity

from .fake import FakeTelegramService
from .service import TelegramService
from .tools import Connector, errors

BUNDLE_FILE, SIG_FILE = "grants-bundle.json", "grants-bundle.json.sig"  # GR-11a
CONNECTOR_ID = "telegram"  # `aud` of identity assertions; id in the platform registry
_CONNECTION = re.compile(r"([a-z0-9][a-z0-9_-]*)=([a-z0-9][a-z0-9_-]*):([A-Za-z0-9][A-Za-z0-9._:-]{0,127})")
log = logging.getLogger("telegram_connector")


class ConfigError(Exception):
    """The service must not start."""


@dataclass(frozen=True)
class Settings:
    profile: str
    service: str
    connections: dict[str, tuple[str, str]]
    bundle_dir: Path
    bundle_keys: str
    state_file: Path
    test_issuer_key: str | None
    test_controls: bool
    identity_keys: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        s = cls(
            profile=env.get("GXG_PROFILE", "production"),
            service=env.get("TELEGRAM_SERVICE", ""),
            connections=parse_connections(env.get("TELEGRAM_CONNECTIONS", "")),
            bundle_dir=Path(env.get("TELEGRAM_BUNDLE_DIR", "/bundle")),
            bundle_keys=env.get("TELEGRAM_BUNDLE_KEYS", ""),
            state_file=Path(env.get("TELEGRAM_STATE_FILE", "/var/lib/telegram/bundle-state.json")),
            test_issuer_key=env.get("TELEGRAM_TEST_ISSUER_KEY") or None,
            test_controls=env.get("TELEGRAM_TEST_CONTROLS") == "1",
            identity_keys=env.get("TELEGRAM_IDENTITY_KEYS", ""),
        )
        if s.profile != "test" and (s.test_issuer_key is not None or s.test_controls):
            raise ConfigError("test issuer and test controls are allowed only with GXG_PROFILE=test")
        if s.test_issuer_key is not None and s.identity_keys:
            raise ConfigError("TELEGRAM_TEST_ISSUER_KEY and TELEGRAM_IDENTITY_KEYS are exclusive")
        if s.service != "fake" or s.profile != "test":
            raise ConfigError("TELEGRAM_SERVICE: only `fake` with GXG_PROFILE=test; no adapter to the service yet")
        return s


def parse_connections(value: str) -> dict[str, tuple[str, str]]:
    out = {}
    for item in filter(None, (p.strip() for p in value.split(","))):
        m = _CONNECTION.fullmatch(item)
        if m is None or m[1] in out:
            raise ConfigError(
                "TELEGRAM_CONNECTIONS: expected tenant=hub_connection:service_connection, once per tenant"
            )
        out[m[1]] = (m[2], m[3])
    return out


class NoVerifier:
    """No identity adapter keys configured: refuses every call (fail closed)."""

    def verify(self, headers: Mapping[str, str], **_: Any) -> VerifiedIdentity:
        raise TokenError("no identity verifier configured")


def parse_keys(value: str, name: str) -> dict[str, Ed25519PublicKey]:
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
    mcp = FastMCP("telegram")
    for tool in connector.tools(verifier, refresh):
        mcp.add_tool(tool)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        # Read-only connector: the service port + a valid bundle; no Approval Service (MCC-09).
        refresh()
        ready = store.readiness()
        reasons = list(ready.reasons) + ([] if connector.service.ready() else ["telegram service is not available"])
        if reasons:
            log.warning(json.dumps({"event": "not_ready", "reasons": reasons}))
        # The reasons stay in the log: /readyz does not disclose configuration (MCC-09).
        return JSONResponse({"status": "not_ready" if reasons else "ready"}, status_code=503 if reasons else 200)

    return mcp.http_app(stateless_http=True, json_response=True)


def build(settings: Settings, service: TelegramService | None = None) -> tuple[Starlette, Connector]:
    service = service or FakeTelegramService()
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    store = BundleStore(parse_keys(settings.bundle_keys, "TELEGRAM_BUNDLE_KEYS"), state_file=settings.state_file)
    files = BundleFiles(store, settings.bundle_dir)
    files.refresh()
    verifier: IdentityVerifier = NoVerifier()
    if settings.test_issuer_key is not None:
        verifier = StubVerifier(CONNECTOR_ID, parse_public_key(settings.test_issuer_key))
    elif settings.identity_keys:
        verifier = ContextForgeVerifier(CONNECTOR_ID, parse_keys(settings.identity_keys, "TELEGRAM_IDENTITY_KEYS"))
    connector = Connector(service, store, settings.connections)
    return create_app(connector, verifier, files.refresh, store), connector


def configs(settings: Settings, app: Starlette, connector: Connector) -> list[uvicorn.Config]:
    out = [uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning")]  # noqa: S104
    if settings.test_controls:
        from .testctl import create_app as test_app  # noqa: PLC0415 - absent in the production image

        assert isinstance(connector.service, FakeTelegramService)  # noqa: S101 - from_env allows only the fake
        out.append(uvicorn.Config(test_app(connector.service), host="127.0.0.1", port=8090))
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
    for noisy in (
        "mcp",
        "fastmcp",
        "httpx",
        "httpx2",
    ):  # backlog M3: no SDK text lines next to the audit (uvicorn: configs)
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = Settings.from_env(env)
        app, connector = build(settings)
    except (ConfigError, RuntimeError, ValueError, OSError) as exc:
        log.error(json.dumps({"event": "startup_refused", "reason": str(exc)}))
        return 2
    serve(configs(settings, app, connector))
    return 0
