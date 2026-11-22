"""pytest plugin of the suite: `--connector`, the connector's harness, the test environment, a report by check ID."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tomllib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gxg_connector_kit import svc_jwt
from gxg_connector_kit._jwt import b64url_encode
from gxg_connector_kit.effects import Wiring
from gxg_connector_kit.identity import StubIssuer
from gxg_connector_kit.svc_jwt import parse_registry

from . import bundle
from .fakes import FakeApproval
from .harness import Harness, Running

REPORT: dict[str, dict[str, Any]] = {}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--connector", required=True, help="connector directory (gxg-manifest.yaml, pyproject.toml)")
    parser.addoption("--check-report", default=None, help="write the result of every check as JSON here")


def pytest_configure(config: pytest.Config) -> None:
    os.environ["GXG_PROFILE"] = "test"  # the kit's test issuer refuses to work otherwise
    config.addinivalue_line("markers", "check(id): Conformance Matrix ID of this check")
    config.addinivalue_line("markers", "effects: only for a connector with effect tools (`executes_via`)")


def load_harness(root: Path) -> Harness:
    """The connector's `harness()`; its `src/` goes first on sys.path, so a copy of a connector is the one tested."""
    conf = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    target = conf.get("tool", {}).get("gxg-connector-check", {}).get("harness")
    if not isinstance(target, str) or ":" not in target:
        raise pytest.UsageError(f"{root}/pyproject.toml: set [tool.gxg-connector-check] harness = 'module:function'")
    module, _, func = target.partition(":")
    sys.path.insert(0, str(root / "src"))
    package = module.split(".")[0]
    for name in [n for n in sys.modules if n == package or n.startswith(f"{package}.")]:
        del sys.modules[name]  # an installed copy of the same package must not answer for this one
    harness: Harness = getattr(importlib.import_module(module), func)()
    return harness


@pytest.fixture(scope="session")
def harness(pytestconfig: pytest.Config) -> Harness:
    return load_harness(Path(pytestconfig.getoption("--connector")).resolve())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class Env:
    """The suite's side of one started connector: bundle, keys, identities, fake Approval Service."""

    harness: Harness
    tmp: Path
    manifest: dict[str, Any] = field(init=False)
    issuer: StubIssuer = field(default_factory=StubIssuer)
    bundle_signer: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    executor: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    svc: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    approval_signer: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    secret: str = field(default_factory=lambda: f"provider-secret-{os.urandom(8).hex()}")

    def __post_init__(self) -> None:
        self.manifest = yaml.safe_load((self.harness.root / "gxg-manifest.yaml").read_text(encoding="utf-8"))
        self.connector_id: str = self.manifest["connector"]
        self.effects = any("executes_via" in t for t in self.manifest["tools"])
        self.grants = bundle.grants(self.harness.root)
        self.platform = bundle.registry(
            self.manifest,
            [g["tenant"] for g in self.grants],
            executor=self.executor.public_key(),
            svc=self.svc.public_key(),
            approval=self.approval_signer.public_key(),
        )
        self.approval = FakeApproval(
            lambda: parse_registry(self.platform), bundle.jwks(self.executor.public_key()), self.connector_id
        )
        self.write_bundle(self.grants, datetime.now(UTC) - timedelta(minutes=5))
        (self.tmp / "svc.key").write_text(b64url_encode(self.svc.private_bytes_raw()), encoding="ascii")

    def write_bundle(self, grants: list[dict[str, Any]], built_at: datetime) -> None:
        bundle.write(self.tmp / "bundle", grants, self.platform, self.bundle_signer, built_at=built_at)

    def settings_env(self) -> dict[str, str]:
        p = self.harness.settings.PREFIX
        env = {
            "GXG_PROFILE": "test",
            f"{p}_BUNDLE_DIR": str(self.tmp / "bundle"),
            f"{p}_BUNDLE_KEYS": bundle.keys_setting(self.bundle_signer),
            f"{p}_STATE_FILE": str(self.tmp / "state" / "bundle-state.json"),
            f"{p}_TEST_ISSUER_KEY": bundle.public(self.issuer.public_key),
        }
        if self.effects:
            env |= {f"{p}_APPROVAL_URL": "http://approval", f"{p}_SVC_KEY_FILE": str(self.tmp / "svc.key")}
        return env

    def start(self, **overrides: Any) -> Running:
        os.environ[self.harness.secret_env] = self.secret  # a provider credential the connector may read (SC-09)
        settings = self.harness.settings.from_env(self.settings_env())
        settings = replace(settings, **overrides)
        transport = self.approval.transport()
        return self.harness.start(settings, Wiring(approval=transport, jwks=transport))

    def identity(self, principal: str, **kw: Any) -> dict[str, str]:
        return self.issuer.headers(
            tenant_id=principal.split(":")[1], principal=principal, audience=self.connector_id, **kw
        )

    def executor_jwt(self, *, signer: Ed25519PrivateKey | None = None, iss: str = "executor") -> str:
        return svc_jwt.sign(signer or self.executor, iss=iss, aud=self.connector_id)

    @asynccontextmanager
    async def client(self, running: Running) -> AsyncIterator[Client]:
        async with AsyncExitStack() as stack:
            app = running.app
            await stack.enter_async_context(app.router.lifespan_context(app))
            http = await stack.enter_async_context(
                httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://connector")
            )
            read, write = await stack.enter_async_context(
                streamable_http_client("http://connector/mcp", http_client=http)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            yield Client(self, http, session)


@dataclass
class Client:
    env: Env
    http: httpx2.AsyncClient
    session: ClientSession

    async def call(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        principal: str | None = None,
        identity: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The tool result as JSON (`isError`, `structuredContent`, `content`)."""
        ident = identity if identity is not None else self.env.identity(principal or self.env.harness.reader)
        extra = {**ident, **(headers or {})}
        self.http.headers.update(extra)
        try:
            result = await self.session.call_tool(tool, args, meta=meta)  # type: ignore[arg-type]
        finally:
            for k in extra:
                del self.http.headers[k]
        dumped: dict[str, Any] = result.model_dump(by_alias=True, exclude_none=True, mode="json")
        return dumped


@pytest.fixture
def env(harness: Harness, tmp_path: Path) -> Env:
    return Env(harness, tmp_path)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Effect checks run only for a connector with effect tools; the others are reported `not_applicable`
    (deselected, never skipped: DoD-5)."""
    manifest = yaml.safe_load((Path(config.getoption("--connector")) / "gxg-manifest.yaml").read_text(encoding="utf-8"))
    has_effects = any("executes_via" in t for t in manifest["tools"])
    keep: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        marker = item.get_closest_marker("check")
        if marker is None:
            raise pytest.UsageError(f"{item.nodeid}: every check has @pytest.mark.check(<matrix id>)")
        applies = has_effects or item.get_closest_marker("effects") is None
        (keep if applies else dropped).append(item)
        entry = REPORT.setdefault(marker.args[0], {"status": "not_run" if applies else "not_applicable", "checks": []})
        entry["checks"].append(item.name)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when != "call" and not report.failed:
        return
    name = report.nodeid.rsplit("::", 1)[-1]
    check = next((k for k, v in REPORT.items() if name in v["checks"]), None)
    if check is None:
        return
    entry = REPORT[check]
    if entry["status"] != "failed":
        entry["status"] = "failed" if report.failed else "passed"
    if report.failed:  # the assertion itself, not the ExceptionGroup frame of an async test
        crash = getattr(report.longrepr, "reprcrash", None)
        message = crash.message if crash is not None else report.longreprtext.strip().splitlines()[-1]
        entry.setdefault("errors", []).append(f"{name}: {message[:400]}")


def pytest_sessionfinish(session: pytest.Session) -> None:
    out = session.config.getoption("--check-report")
    if out:
        Path(out).write_text(json.dumps(dict(sorted(REPORT.items())), indent=2) + "\n", encoding="utf-8")
