"""FastMCP plumbing every GxG connector shares (docs/specs/mcp-toolkit-v1.md). Needs the `mcp` extra.

Generalised from the reference connector `demo` (M3). A connector supplies a `Domain`: its connection per tenant,
how a named resource resolves to a RID, and one handler per manifest tool. This module supplies the rest, in one
order for every call:

  identity from the trusted channel (SEC-03, MCC-02) -> input schema -> valid bundle (GR-12)
  -> resource hook (MCC-03): allowed() first, so a caller without any grant of the type never reaches the backend;
     `scope: single` resolves the RID (from the argument or by the backend) and check()s it; a named resource without
     a grant is `not_found` (GR-17), `resource_forbidden` only when the caller may read it
  -> handler(Access, args) within limits.timeout_s -> output schema and size (MCC-04)
  -> GxgError envelope as a tool error (MCC-05); one redacted JSON audit line (MCC-08, SEC-18).

A handler cannot run without an `Access`, and a manifest tool without `resource` does not load (manifest schema).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import sys
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, ClassVar, Protocol, Self

import jsonschema
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.base import Tool, ToolResult
from pydantic import Field
from pydantic.json_schema import SkipJsonSchema
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import _schema
from ._jwt import TokenError, parse_public_key
from .cf_identity import ContextForgeVerifier
from .errors import GxgError
from .grants import Action, AllowedSet, BundleError, BundleStore, Decision, GrantResolver
from .identity import IdentityVerifier, StubVerifier, VerifiedIdentity
from .readiness import Readiness, all_ready
from .redact import redact

BUNDLE_FILE, SIG_FILE = "grants-bundle.json", "grants-bundle.json.sig"  # GR-11a
NOT_FOUND = "not found"  # one message for "no grant" and "does not exist" (GR-17, SC-05)
NOTHING = AllowedSet(False, frozenset())
MAX_RESULT_BYTES = 256 * 1024  # MCC-04 default bound of one structuredContent; limits.max_bytes overrides it
log = logging.getLogger("gxg_connector_kit.mcp")

Result = tuple[dict[str, Any], dict[str, Any]]  # (structuredContent, audit fields)


class ConfigError(Exception):
    """The service must not start (fail closed)."""


@dataclass(frozen=True)
class Settings:
    """Environment of a connector process; no secrets, only public keys and paths. `PREFIX` names the variables:

    GXG_PROFILE                  `test` enables the test identity issuer and test controls; anything else: production
    <P>_BUNDLE_DIR               directory with grants-bundle.json and grants-bundle.json.sig (GR-11a)
    <P>_BUNDLE_KEYS              `kid=ed25519:<base64url>[,kid=...]`: bundle verification keys (never from the bundle)
    <P>_STATE_FILE               newest loaded `built_at`, survives restarts (GR-13a)
    <P>_IDENTITY_KEYS            ContextForge identity adapter keys (ADR-0001 C); without them every call is refused
    <P>_TEST_ISSUER_KEY          test profile only: public key of the test identity issuer
    <P>_TEST_CONTROLS=1          test profile only: test controls on 127.0.0.1:8090
    <P>_APPROVAL_URL             effects: base URL of the Approval Service `api` (intents, dispatch, executor JWKS);
                                 without it the connector is read-only: no `propose` tool, no internal port
    <P>_APPROVAL_AUDIENCE        effects: its service ID in the platform registry (default approval-api)
    <P>_SVC_KEY_FILE             effects: secret file, Ed25519 seed (base64url) of this connector's service JWT key
    """

    PREFIX: ClassVar[str] = "GXG"
    profile: str
    bundle_dir: Path
    bundle_keys: str
    state_file: Path
    test_issuer_key: str | None
    test_controls: bool
    identity_keys: str = ""
    approval_url: str = ""
    approval_audience: str = "approval-api"
    svc_key_file: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        p = cls.PREFIX
        s = cls(
            profile=env.get("GXG_PROFILE", "production"),
            bundle_dir=Path(env.get(f"{p}_BUNDLE_DIR", "/bundle")),
            bundle_keys=env.get(f"{p}_BUNDLE_KEYS", ""),
            state_file=Path(env.get(f"{p}_STATE_FILE", f"/var/lib/{p.lower()}/bundle-state.json")),
            test_issuer_key=env.get(f"{p}_TEST_ISSUER_KEY") or None,
            test_controls=env.get(f"{p}_TEST_CONTROLS") == "1",
            identity_keys=env.get(f"{p}_IDENTITY_KEYS", ""),
            approval_url=env.get(f"{p}_APPROVAL_URL", ""),
            approval_audience=env.get(f"{p}_APPROVAL_AUDIENCE", "approval-api"),
            svc_key_file=env.get(f"{p}_SVC_KEY_FILE", ""),
        )
        if s.approval_url and not s.svc_key_file:
            raise ConfigError(f"{p}_APPROVAL_URL needs {p}_SVC_KEY_FILE (service JWT key, AS-06)")
        if s.profile != "test" and (s.test_issuer_key is not None or s.test_controls):
            raise ConfigError("test issuer and test controls are allowed only with GXG_PROFILE=test")
        if s.test_issuer_key is not None and s.identity_keys:
            raise ConfigError(f"{p}_TEST_ISSUER_KEY and {p}_IDENTITY_KEYS are exclusive")
        return s


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


def verifier(settings: Settings, connector_id: str) -> IdentityVerifier:
    """Test issuer (test profile only), ContextForge adapter keys, or nothing: every call refused."""
    if settings.test_issuer_key is not None:
        return StubVerifier(connector_id, parse_public_key(settings.test_issuer_key))
    if settings.identity_keys:
        return ContextForgeVerifier(
            connector_id, parse_keys(settings.identity_keys, f"{settings.PREFIX}_IDENTITY_KEYS")
        )
    return NoVerifier()


class BundleFiles:
    """Loads the bundle at start and again whenever its files change (GR-12). A bad file keeps the previous bundle."""

    def __init__(self, store: BundleStore, directory: Path, events: logging.Logger = log) -> None:
        self.store = store
        self.directory = directory
        self.events = events
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
            self.events.info(json.dumps({"event": "bundle_loaded", "bundle_version": bundle.version}))
        except (BundleError, OSError) as exc:
            self.events.warning(json.dumps({"event": "bundle_rejected", "reason": str(exc)}))


def bundle_store(settings: Settings, events: logging.Logger = log) -> tuple[BundleStore, BundleFiles]:
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    store = BundleStore(
        parse_keys(settings.bundle_keys, f"{settings.PREFIX}_BUNDLE_KEYS"), state_file=settings.state_file
    )
    files = BundleFiles(store, settings.bundle_dir, events)
    files.refresh()
    return store, files


# --- manifest ---------------------------------------------------------------------------------------------------


def load_schema(root: Path, rel: str) -> dict[str, Any]:
    """A schema file referenced by gxg-manifest.yaml, relative to the connector directory `root`."""
    # shortcut: a `../..` path (a repository schema) is looked up by its tail in the nearest parent (repo checkout,
    # /app in the image, mutmut's copy under mutants/); a connector outside the repository keeps its own copy.
    tail = Path(*(part for part in Path(rel).parts if part != ".."))
    found = next((d / tail for d in (root, *root.parents) if (d / tail).is_file()), None)
    if found is None:
        raise ConfigError(f"schema not found: {rel}")
    doc: dict[str, Any] = json.loads(found.read_bytes())
    return doc


def load_manifest(path: Path, root: Path | None = None) -> list[dict[str, Any]]:
    """The manifest's tools, valid by gxg-manifest.schema.json and bounded (MCC-01, MCC-04), or ConfigError.
    Schema paths are relative to `root` (default: the manifest's directory)."""
    root = root or path.parent
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    error = next(_schema.validator("gxg-manifest").iter_errors(doc), None)
    if error is not None:
        raise ConfigError(f"gxg-manifest.yaml: {error.message}")
    tools: list[dict[str, Any]] = doc["tools"]
    for t in tools:
        if not t["name"].startswith(f"{doc['connector']}.") or t["name"].endswith((".execute", ".reconcile")):
            raise ConfigError(f"{t['name']}: a public tool is <connector>.<object>.<verb>, never execute/reconcile")
        if "output_schema" not in t:
            raise ConfigError(f"{t['name']}: output_schema is required (MCC-04)")
        max_results = t.get("limits", {}).get("max_results")
        if max_results is not None:
            limit = load_schema(root, t["input_schema"]).get("properties", {}).get("limit", {})
            if not isinstance(limit.get("maximum"), int) or limit["maximum"] > max_results:
                raise ConfigError(f"{t['name']}: input `limit` must have a maximum <= limits.max_results")
    return tools


# --- the call pipeline ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Access:
    """What the resource hook allowed. `scope: single`: `rid`, `grant_id` and what `resolve` found (`target`);
    `scope: list`: `allowed` (may be empty: the handler returns an empty result without a backend call, GR-16)."""

    ident: VerifiedIdentity
    action: Action
    connection: str | None
    rid: str | None = None
    grant_id: str | None = None
    target: Any = None
    allowed: AllowedSet = field(default_factory=lambda: NOTHING)
    check: Callable[[str], Decision] = field(default=lambda _rid: Decision(False), repr=False)

    @property
    def nothing(self) -> bool:
        return _nothing(self.allowed)


Handler = Callable[[Access, dict[str, Any]], Awaitable[Result]]


class Domain(Protocol):
    """What a connector adds to the kit."""

    connector_id: str  # `aud` of identity assertions; id in the platform registry
    rtype: str  # resource type of its RIDs (GR-01)

    def connection(self, tenant: str) -> str | None:
        """The tenant's connection; configuration only, never a backend call (MCC-03)."""
        ...

    async def resolve(self, tool: str, access: Access, args: dict[str, Any]) -> tuple[str, Any] | None:
        """`scope: single`: (RID, what was found) for the named resource, None if it does not exist.
        Called only after allowed() found some grant of the type; with `resolve: backend` it may read the backend."""
        ...

    def handlers(self) -> Mapping[str, Handler]: ...


@dataclass(frozen=True)
class Logs:
    startup: logging.Logger  # events: bundle loaded or rejected, startup refused (stdout)
    audit: logging.Logger  # one JSON line per call (stdout)
    errors: logging.Logger  # stack frames of internal errors (stderr)

    @classmethod
    def of(cls, package: str) -> Logs:
        return cls(*(logging.getLogger(n) for n in (package, f"{package}.audit", f"{package}.errors")))


def access_hook(domain: Domain, grants: GrantResolver, spec: Mapping[str, Any]) -> Callable[..., Awaitable[Access]]:
    """MCC-03 for one manifest tool; raises `not_found`/`resource_forbidden` before the handler."""
    action: Action = spec["action"]
    scope = spec["resource"].get("scope", "single")
    rtype: str = spec["resource"]["type"]

    async def hook(ident: VerifiedIdentity, args: dict[str, Any]) -> Access:
        tenant, principal = ident.tenant_id, ident.principal
        connection = domain.connection(tenant)

        def check(rid: str) -> Decision:
            return grants.check(tenant, principal, action, rid)

        def allowed(act: Action) -> AllowedSet:
            return grants.allowed(tenant, principal, act, connection, rtype) if connection is not None else NOTHING

        access = Access(ident, action, connection, allowed=allowed(action), check=check)
        if scope == "list":
            return access
        # No grant of the type at all (for `propose`: not even `read`): `not_found` without a backend call (GR-16).
        if access.nothing and (action == "read" or _nothing(allowed("read"))):
            raise GxgError("not_found", NOT_FOUND)
        found = await domain.resolve(spec["name"], access, args)
        if found is None:
            raise GxgError("not_found", NOT_FOUND)
        rid, target = found
        decision = check(rid)
        if not decision.allowed:
            # GR-17: no right on the named resource is `not_found`; a readable one without this action is forbidden
            if action != "read" and grants.check(tenant, principal, "read", rid).allowed:
                raise GxgError("resource_forbidden", f"no {action} right on this resource")
            raise GxgError("not_found", NOT_FOUND)
        return Access(ident, action, connection, rid, decision.grant_id, target, access.allowed, check)

    return hook


def _nothing(allowed: AllowedSet) -> bool:
    return not allowed.all_of_type and not allowed.rids


class GxgTool(Tool):
    """A FastMCP tool whose schemas are the connector's files and whose every call runs the pipeline above."""

    handler: Annotated[SkipJsonSchema[Handler], Field(exclude=True)]
    hook: Annotated[SkipJsonSchema[Callable[..., Awaitable[Access]]], Field(exclude=True)]
    verify: Annotated[SkipJsonSchema[Callable[..., VerifiedIdentity]], Field(exclude=True)]  # IdentityVerifier.verify
    refresh: Annotated[SkipJsonSchema[Callable[[], None]], Field(exclude=True)]
    logs: Annotated[SkipJsonSchema[Any], Field(exclude=True)]  # Logs (pydantic cannot model a Logger)
    max_bytes: Annotated[SkipJsonSchema[int], Field(exclude=True)] = MAX_RESULT_BYTES

    async def _authorized(self, ident: VerifiedIdentity, arguments: dict[str, Any]) -> tuple[Access, Result]:
        access = await self.hook(ident, arguments)
        return access, await self.handler(access, arguments)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        entry: dict[str, Any] = {"request_id": str(uuid.uuid4()), "tool": self.name}
        try:
            # Identity from headers only; tool and arguments are checked against the signed binding
            # (SEC-03 `tool`, `ahs`), _meta never reaches the verifier (MCC-02).
            ident = self.verify(get_http_request().headers, tool=self.name, arguments=arguments)
            entry |= {"request_id": ident.request_id, "tenant": ident.tenant_id, "principal": ident.principal}
            error = next(jsonschema.Draft202012Validator(self.parameters).iter_errors(arguments), None)
            if error is not None:
                where = "/" + "/".join(map(str, error.absolute_path))
                raise GxgError("invalid_input", "arguments do not match the input schema", details={"path": where})
            self.refresh()  # also refuses with temporarily_unavailable without a valid bundle (GR-12)
            try:  # gxg-manifest.yaml limits.timeout_s; FastMCP does not enforce Tool.timeout for this run()
                access, (content, fields) = await asyncio.wait_for(self._authorized(ident, arguments), self.timeout)
            except TimeoutError:
                # sent to the backend, no answer: a read is safe to repeat; never timeout_before_start (MCC-05)
                raise GxgError("temporarily_unavailable", "backend did not answer in time") from None
            self._bounded(content)
            entry |= {"grant_id": [access.grant_id]} if access.grant_id else {}
            entry |= fields | {"outcome": "ok"}
            return ToolResult(structured_content=content)
        except GxgError as exc:
            entry["outcome"] = exc.code
            if isinstance(exc, TokenError):  # why the identity was refused: unknown kid alerts (SEC-03); no token text
                entry["reason"] = exc.reason
            return ToolResult(structured_content=exc.to_dict(), is_error=True)
        except Exception as exc:  # MCC-05 envelope; the text may hold provider secrets (SEC-18): class and frames only
            entry |= {"outcome": "internal_error", "error": type(exc).__name__}
            frames = "\n".join(f"{f.filename}:{f.lineno} in {f.name}" for f in traceback.extract_tb(exc.__traceback__))
            self.logs.errors.error("internal_error %s %s\n%s", entry["request_id"], entry["error"], frames)
            return ToolResult(structured_content=GxgError("internal_error", "internal error").to_dict(), is_error=True)
        finally:
            entry["duration_ms"] = round((time.monotonic() - started) * 1000)
            self.logs.audit.info(json.dumps(redact(entry)))

    def _bounded(self, content: dict[str, Any]) -> None:
        """MCC-04: the result matches the output schema and fits `max_bytes`; otherwise it is not sent."""
        if self.output_schema is not None and not jsonschema.Draft202012Validator(self.output_schema).is_valid(content):
            raise RuntimeError("result does not match the output schema")
        if len(json.dumps(content, ensure_ascii=False).encode()) > self.max_bytes:
            raise RuntimeError("result exceeds limits.max_bytes")


def tools(  # noqa: PLR0917 - the parts a connector wires together, named at the call site
    domain: Domain,
    manifest: Path,
    grants: GrantResolver,
    identity: IdentityVerifier,
    refresh: Callable[[], None],
    logs: Logs,
    root: Path | None = None,
) -> list[GxgTool]:
    """One `GxgTool` per manifest tool; a manifest tool without a handler (or the reverse) does not start.
    `root`: the connector directory that schema paths are relative to (default: the manifest's directory)."""
    handlers = domain.handlers()
    root = root or manifest.parent
    specs = load_manifest(manifest, root)
    if {t["name"] for t in specs} != set(handlers):
        raise ConfigError("manifest tools and handlers differ")

    def ready() -> None:
        refresh()
        _ = grants.version  # GR-12: no valid bundle -> temporarily_unavailable before any handler

    out = []
    for t in specs:
        schema = load_schema(root, t["input_schema"])
        out.append(
            GxgTool(
                name=t["name"],
                description=schema["description"],
                parameters=schema,
                output_schema=load_schema(root, t["output_schema"]),
                timeout=float(t["limits"]["timeout_s"]),
                handler=handlers[t["name"]],
                hook=access_hook(domain, grants, t),
                verify=identity.verify,
                refresh=ready,
                logs=logs,
                max_bytes=int(t["limits"].get("max_bytes", MAX_RESULT_BYTES)),
            )
        )
    return out


def create_app(
    name: str,
    tools: list[GxgTool],
    refresh: Callable[[], None],
    readiness: Callable[[], Readiness],
    events: logging.Logger = log,
) -> Starlette:
    """The public MCP app (Streamable HTTP, `POST /mcp`) with `/healthz` and `/readyz` (MCC-09)."""
    mcp = FastMCP(name)
    for tool in tools:
        mcp.add_tool(tool)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        refresh()
        ready = readiness()
        if not ready.ready:
            events.warning(json.dumps({"event": "not_ready", "reasons": list(ready.reasons)}))
        # The reasons stay in the log: /readyz does not disclose configuration (MCC-09).
        return JSONResponse(
            {"status": "ready" if ready.ready else "not_ready"}, status_code=200 if ready.ready else 503
        )

    return mcp.http_app(stateless_http=True, json_response=True)


def readiness_of(*parts: Callable[[], Readiness]) -> Callable[[], Readiness]:
    return lambda: all_ready(*(p() for p in parts))


# --- cursor -----------------------------------------------------------------------------------------------------

_CURSOR = re.compile(r"([0-9]{1,9})\.[0-9a-f]{64}")


class Cursor:
    """Opaque page cursor: offset + HMAC-SHA256 over the caller and the query, with a key of this process."""

    def __init__(self) -> None:
        self._key = secrets.token_bytes()

    def make(self, binding: bytes, offset: int) -> str:
        return f"{offset}.{hmac.new(self._key, binding + str(offset).encode(), hashlib.sha256).hexdigest()}"

    def offset(self, binding: bytes, cursor: str | None) -> int:
        if cursor is None:
            return 0
        m = _CURSOR.fullmatch(cursor)
        if m is None or not hmac.compare_digest(cursor, self.make(binding, int(m[1]))):
            raise GxgError("invalid_input", "cursor is not valid for this caller and query")
        return int(m[1])


# --- process ----------------------------------------------------------------------------------------------------


def serve(configs: list[uvicorn.Config]) -> None:
    """Blocks until shut down."""

    async def run() -> None:
        await asyncio.gather(*(uvicorn.Server(c).serve() for c in configs))

    asyncio.run(run())


def setup_logging(logs: Logs) -> None:
    """JSON log lines (audit, events) to stdout; stack frames of internal errors to stderr only."""
    out, err = logging.StreamHandler(sys.stdout), logging.StreamHandler()  # stderr
    out.addFilter(lambda r: r.name != logs.errors.name)
    err.addFilter(lambda r: r.name == logs.errors.name)
    for h in (out, err):
        h.setFormatter(logging.Formatter())  # the message only
    logging.basicConfig(level=logging.INFO, handlers=[out, err])


def run_main(
    start: Callable[[], list[uvicorn.Config]], serve: Callable[[list[uvicorn.Config]], None], logs: Logs
) -> int:
    """`start()` builds the apps; a refused configuration exits 2 with one JSON line, never a traceback."""
    setup_logging(logs)
    try:
        configs = start()
    except (ConfigError, RuntimeError, ValueError, OSError) as exc:
        logs.startup.error(json.dumps({"event": "startup_refused", "reason": str(exc)}))
        return 2
    serve(configs)
    return 0
