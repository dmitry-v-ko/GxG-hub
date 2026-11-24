#!/usr/bin/env python3
"""Idempotent Railway deployment of the GxG Hub pilot (M13): project `gxg-hub-pilot`, services and variables from
`deploy/railway/services.yaml`, over the Railway GraphQL API. The session has no Railway token: the owner runs it.

    RAILWAY_TOKEN=... python scripts/deploy/railway_deploy.py            # create what is missing, keep secrets
    python scripts/deploy/railway_deploy.py --dry-run                    # the plan, no API calls, no values
    RAILWAY_TOKEN=... python scripts/deploy/railway_deploy.py --verify   # AC-M14-11 (exit 1 on any failure)
    RAILWAY_TOKEN=... python scripts/deploy/railway_deploy.py --finalize # delete the transient db-init job

Secrets (EdDSA keys, webhook HMAC, database role passwords, ...) are generated here, in memory, and go straight into
the variables of the one service that owns them (AS-28). Values are never printed or written to disk: every line of
output passes through a redactor that knows each secret this run has seen, and only variable NAMES are logged.
The token and the request headers are never printed either.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets as rnd
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from gxg_approval.crypto import generate as x25519_pair
from gxg_connector_kit import _jwt, exec_token

API = "https://backboard.railway.com/graphql/v2"
ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "deploy/railway/services.yaml"
PRIVATE = re.compile(r"^[a-z0-9.-]+\.railway\.internal$")
PRIVATE_REF = re.compile(r"^\$\{\{[A-Za-z0-9_-]+\.RAILWAY_PRIVATE_DOMAIN\}\}$")

# AS-28 (approval-service-v1.md): which process may hold which secret. `db_dsn` is checked against the role table below.
AS28: dict[str, frozenset[str]] = {
    "db_dsn": frozenset({"migrate", "api", "sweeper", "executor", "ui", "notifier", "decider", "admin"}),
    "svc_key": frozenset({"mcp", "executor"}),
    "exec_signing": frozenset({"executor"}),
    "payload_args": frozenset({"executor"}),
    "payload_preview": frozenset({"ui"}),
    "ui_secrets": frozenset({"ui"}),
    "ui_svc_key": frozenset(
        {"ui"}
    ),  # M13: service JWT key `approval-ui` for the Telegram admin API; the public half goes to telegram
    "telegram_bot_token": frozenset({"notifier"}),
    "webhook_secrets": frozenset({"notifier"}),
    "tailscale": frozenset({"ui", "admin"}),  # tag:approval-ui and tag:approval-admin entries (ADR-0003)
    "bootstrap": frozenset({"db-init", "telegram-db-init"}),  # transient job, removed by --finalize
    "admin_oidc": frozenset(
        {"admin"}
    ),  # the operator's browser sign-in of the invite page: client secret (a reference), session key
    "agent_token": frozenset({"pilot-agent"}),  # vault: the pilot agent's ContextForge API token (SEC-02)
    "bundle_signing": frozenset({"bundle-signer"}),  # vault service, never deployed: signs the grants bundle (GR-11)
}
ROLE_OF = {  # AS-27: one role per process
    "migrate": "gxg_owner", "admin": "gxg_admin", "api": "gxg_api", "sweeper": "gxg_sweeper", "executor": "gxg_executor",
    "ui": "gxg_ui", "notifier": "gxg_notifier", "decider": "gxg_decider",
}  # fmt: skip
# Shared variables the owner keeps for other tools in the project: named exceptions of --verify, never touched here.
FOREIGN_SHARED = {"OPENROUTER_API_KEY": "not ours"}
TG_ROLE_VAR = {
    "gxg_telegram_service": "TELEGRAM_SERVICE_DSN",
    "gxg_telegram_reader": "TELEGRAM_ARCHIVE_DSN",
}  # gxg-telegram-service
SECRET_PREFIX = "GXG_SECRET__"  # noqa: S105 (a naming prefix)


class DeployError(Exception):
    """A readable failure; never carries a secret."""


# ---------------------------------------------------------------------------------------------------------- output
class Out:
    """stdout/stderr writer with redaction: any secret value this run has seen is replaced, whatever the caller did."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def add(self, value: str) -> None:
        if len(value) >= 6:
            self._values.add(value)

    def clean(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            text = text.replace(v, "[redacted]")
        return text

    def say(self, text: str = "", *, err: bool = False) -> None:
        print(self.clean(text), file=sys.stderr if err else sys.stdout, flush=True)


# ---------------------------------------------------------------------------------------------------------- API
Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


def http_transport(token: str, url: str = API) -> Transport:
    """POST one GraphQL document. Errors name the HTTP status only: headers and bodies are not echoed."""

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(  # noqa: S310 (constant https URL)
            url,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": os.environ.get("RAILWAY_USER_AGENT", "gxg-deploy"),  # the API edge rejects some agents
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                doc: dict[str, Any] = json.load(resp)
                return doc
        except urllib.error.HTTPError as exc:
            raise DeployError(f"Railway API: HTTP {exc.code}") from None
        except (OSError, ValueError):
            raise DeployError("Railway API: no valid response") from None

    return post


class Railway:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def call(self, query: str, **variables: Any) -> dict[str, Any]:
        found = re.search(r"(?:query|mutation)\s+(\w+)", query)
        name = found.group(1) if found else "request"
        doc = self._t(query, variables)
        errors = doc.get("errors")
        if errors:
            first = errors[0].get("message", "error") if isinstance(errors[0], dict) else "error"
            raise DeployError(f"Railway API {name}: {str(first)[:200]}")
        data: dict[str, Any] = doc.get("data") or {}
        return data


Q_PROJECTS = """query Projects { projects { edges { node { id name
  environments { edges { node { id name } } } services { edges { node { id name } } } } } } }"""
Q_PROJECT_CREATE = """mutation ProjectCreate($input: ProjectCreateInput!) { projectCreate(input: $input) { id name
  environments { edges { node { id name } } } services { edges { node { id name } } } } }"""
Q_SERVICE_CREATE = "mutation ServiceCreate($input: ServiceCreateInput!) { serviceCreate(input: $input) { id name } }"
Q_SERVICE_DELETE = "mutation ServiceDelete($id: String!, $environmentId: String) { serviceDelete(id: $id, environmentId: $environmentId) }"
Q_INSTANCE = """query ServiceInstance($serviceId: String!, $environmentId: String!) {
  serviceInstance(serviceId: $serviceId, environmentId: $environmentId) { startCommand healthcheckPath restartPolicyType restartPolicyMaxRetries overlapSeconds } }"""
Q_INSTANCE_UPDATE = """mutation ServiceInstanceUpdate($serviceId: String!, $environmentId: String, $input: ServiceInstanceUpdateInput!) {
  serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input) }"""
Q_TRIGGERS = """query DeploymentTriggers($projectId: String!, $environmentId: String!, $serviceId: String!) {
  deploymentTriggers(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId) {
  edges { node { repository branch } } } }"""
Q_CONNECT = """mutation ServiceConnect($id: String!, $input: ServiceConnectInput!) { serviceConnect(id: $id, input: $input) { id } }"""
Q_REDEPLOY = """mutation ServiceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
  serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId) }"""
Q_VARIABLES = """query Variables($projectId: String!, $environmentId: String!, $serviceId: String) {
  variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId, unrendered: true) }"""
Q_UPSERT = """mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }"""
Q_VOLUMES = """query Volumes($projectId: String!) { project(id: $projectId) { volumes { edges { node { id
  volumeInstances { edges { node { serviceId mountPath } } } } } } } }"""
Q_VOLUME_CREATE = "mutation VolumeCreate($input: VolumeCreateInput!) { volumeCreate(input: $input) { id } }"
Q_DOMAINS = """query Domains($projectId: String!, $environmentId: String!, $serviceId: String!) {
  domains(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId) {
  serviceDomains { id domain } customDomains { id domain } } }"""
Q_TCP = """query TcpProxies($environmentId: String!, $serviceId: String!) {
  tcpProxies(environmentId: $environmentId, serviceId: $serviceId) { id proxyPort } }"""
Q_DEL_SERVICE_DOMAIN = "mutation ServiceDomainDelete($id: String!) { serviceDomainDelete(id: $id) }"
Q_DEL_CUSTOM_DOMAIN = "mutation CustomDomainDelete($id: String!) { customDomainDelete(id: $id) }"
Q_DEL_TCP = "mutation TcpProxyDelete($id: String!) { tcpProxyDelete(id: $id) }"


def is_private(url: str) -> bool:
    """The host of `url` is `<service>.railway.internal` or a reference to a service's RAILWAY_PRIVATE_DOMAIN."""
    hostport = urlsplit(url).netloc.rsplit("@", 1)[-1]
    host = hostport.rsplit(":", 1)[0] if re.search(r":\d+$", hostport) else hostport
    return bool(PRIVATE.match(host) or PRIVATE_REF.match(host))


def edges(node: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    return [e["node"] for e in ((node or {}).get(key) or {}).get("edges", [])]


# ---------------------------------------------------------------------------------------------------------- spec
def load_spec(path: Path = SPEC) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text("utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        raise DeployError(f"{path}: no services")
    return doc


def secrets_of(svc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict(svc.get("secrets") or {})


def declared_names(svc: dict[str, Any]) -> set[str]:
    """Every variable name the service may carry (Railway's own RAILWAY_* aside)."""
    names = set(svc.get("env") or {}) | set(svc.get("owner_env") or []) | set(secrets_of(svc))
    names |= set(svc.get("seed") or {}) | set(svc.get("config") or {}) | set(svc.get("build_args") or {})
    if (svc.get("source") or {}).get("dockerfile"):
        names.add("RAILWAY_DOCKERFILE_PATH")
    return names


def validate_spec(spec: dict[str, Any]) -> list[str]:
    """Static checks, no API: AS-28 per process, one role per process (AS-27), no public exposure keys, private URLs."""
    problems: list[str] = []
    services: dict[str, Any] = spec["services"]
    for name, svc in services.items():
        if svc.get("domain") or svc.get("domains") or svc.get("tcp_proxy"):
            problems.append(f"{name}: public domain or TCP proxy in the spec (SEC-28)")
        for var, sec in secrets_of(svc).items():
            key = sec.get("as28")
            if key is not None and key in AS28 and name not in AS28[key]:
                problems.append(f"{name}: secret {var} ({key}) is not allowed for this process (AS-28)")
            gen = str(sec.get("gen", ""))
            if gen.startswith("db_dsn:") and gen != f"db_dsn:{ROLE_OF.get(name)}":
                problems.append(f"{name}: {var} uses another process's database role (AS-27)")
        for url in svc.get("internal_urls") or []:
            if url not in declared_names(svc):
                problems.append(f"{name}: internal_urls names an undeclared variable {url}")
    for key, procs in AS28.items():  # every deployed process that AS-28 gives a secret must declare it
        for proc in procs:
            if proc in services and not any(s.get("as28") == key for s in secrets_of(services[proc]).values()):
                problems.append(f"{proc}: no secret of kind {key}, AS-28 expects one")
    return problems


# ---------------------------------------------------------------------------------------------------------- values
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Values:
    """Resolves what to write. `existing[service][name]` is what Railway has; `new` collects what this run adds."""

    def __init__(self, spec: dict[str, Any], existing: dict[str, dict[str, str]], out: Out, root: Path = ROOT) -> None:
        self.spec, self.existing, self.out, self.root = spec, existing, out, root
        self.new: dict[str, dict[str, str]] = {s: {} for s in spec["services"]}
        self._role_pw: dict[str, str] = {}
        self._keysets: dict[str, dict[str, str]] = {}
        self._pki: dict[str, str] | None = None

    def get(self, service: str, name: str) -> str | None:
        return self.new[service].get(name) or self.existing.get(service, {}).get(name)

    def put(self, service: str, name: str, value: str, *, secret: bool) -> None:
        self.new[service][name] = value
        if secret:
            self.out.add(value)

    # -- generators
    def role_password(self, role: str) -> str:
        if role not in self._role_pw:
            found: str | None = None
            for svc_name, r in ROLE_OF.items():  # the DSN of the process that owns the role, else the db-init map
                dsn = self.get(svc_name, "GXG_SECRET__GXG_DB_DSN") if r == role else None
                if dsn:
                    found = urlsplit(dsn).password
            if not found and role in TG_ROLE_VAR and "telegram" in self.spec["services"]:
                dsn = self.get("telegram", TG_ROLE_VAR[role])
                if dsn and urlsplit(dsn).username == role:  # not the archive owner's DSN an earlier version wrote there
                    found = urlsplit(dsn).password
            if not found:
                raw = self.get("db-init", "GXG_SECRET__GXG_ROLE_PASSWORDS")
                found = json.loads(raw).get(role) if raw and role in ROLE_OF.values() else None
            if not found and role in TG_ROLE_VAR:
                raw = self.get("telegram-db-init", "GXG_TGDB_ROLE_PASSWORDS")
                found = json.loads(raw).get(role) if raw else None
            self._role_pw[role] = found or b64url(rnd.token_bytes(24))
            self.out.add(self._role_pw[role])
        return self._role_pw[role]

    def keyset(self, set_name: str) -> dict[str, str]:
        if set_name not in self._keysets:
            tenant = str(self.spec["tenant"])
            if set_name == "exec-signing":
                key = Ed25519PrivateKey.generate()
                not_before = datetime.fromtimestamp(time.time() - 60, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                self._keysets[set_name] = {
                    "private": json.dumps([{"seed": b64url(key.private_bytes_raw()), "not_before": not_before}]),
                    "public": json.dumps({"keys": [exec_token.public_jwk(key.public_key())]}),
                }
            elif set_name == "payload":
                (args_priv, args_pub), (prev_priv, prev_pub) = x25519_pair(), x25519_pair()
                self._keysets[set_name] = {
                    "args_private": json.dumps({tenant: args_priv}),
                    "preview_private": json.dumps({tenant: prev_priv}),
                    "public": json.dumps({tenant: {"args": args_pub, "preview": prev_pub}}),
                }
            else:
                raise DeployError(f"unknown keyset {set_name}")
        return self._keysets[set_name]

    def pki(self) -> dict[str, str]:
        """Internal CA of the pilot (AS-07) and the connector's server certificate. The CA key lives in this call only:
        it signs once and is dropped, a reissue creates a new CA (and so replaces every member of the set)."""
        if self._pki is None:
            host = str(self.spec["pki"]["host"])
            now = datetime.now(UTC)
            ca_key = ec.generate_private_key(ec.SECP256R1())
            ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gxg-pilot-internal-ca")])
            ca = (
                x509.CertificateBuilder()
                .subject_name(ca_name)
                .issuer_name(ca_name)
                .public_key(ca_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(hours=1))
                .not_valid_after(now + timedelta(days=365))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=False, content_commitment=False, key_encipherment=False,
                        data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                        encipher_only=False, decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(  # the CA can vouch for private names only
                    x509.NameConstraints(permitted_subtrees=[x509.DNSName("railway.internal")], excluded_subtrees=None),
                    critical=True,
                )
                .sign(ca_key, hashes.SHA256())
            )  # fmt: skip
            key = ec.generate_private_key(ec.SECP256R1())
            cert = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(ca_name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(hours=1))
                .not_valid_after(now + timedelta(days=365))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(  # Python 3.13 verifies strictly: the leaf names its issuer key
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
                )
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(ca_key, hashes.SHA256())
            )  # fmt: skip
            pem = ser.Encoding.PEM
            self._pki = {
                "ca_cert": ca.public_bytes(pem).decode(),
                "server_cert": cert.public_bytes(pem).decode(),
                "server_key": key.private_bytes(pem, ser.PrivateFormat.PKCS8, ser.NoEncryption()).decode(),
            }
        return self._pki

    def pki_members(self) -> list[tuple[str, str]]:
        found = []
        for svc_name, svc in self.spec["services"].items():
            for var, sec in {**secrets_of(svc), **(svc.get("config") or {})}.items():
                gen = sec.get("gen", "") if isinstance(sec, dict) else str(sec)
                if gen.startswith("pki:"):
                    found.append((svc_name, var))
        return found

    def keyset_members(self, set_name: str) -> list[tuple[str, str, str]]:
        """(service, variable, member) of every variable that takes its value from this keyset."""
        found = []
        for svc_name, svc in self.spec["services"].items():
            for var, sec in {**secrets_of(svc), **(svc.get("config") or {})}.items():
                gen = sec.get("gen", "") if isinstance(sec, dict) else str(sec)
                if gen.startswith(f"keyset:{set_name}."):
                    found.append((svc_name, var, gen.split(".", 1)[1]))
        return found

    def _public(self, service: str, secret_name: str) -> Any:
        value = self.get(service, secret_name)
        if not value:
            raise DeployError(f"{service}: {secret_name} is needed to derive a public key but is not set")
        if value.startswith("-----"):
            key = ser.load_pem_private_key(value.encode(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise DeployError(f"{service}: {secret_name} is not an Ed25519 key")
        else:
            key = Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(value))
        return key.public_key().public_bytes_raw()

    def generate(self, service: str, name: str, gen: str, *, secret: bool) -> None:  # noqa: PLR0912
        kind, _, arg = gen.partition(":")
        if kind == "random":
            value = b64url(rnd.token_bytes(int(arg)))
        elif kind == "hmac":
            value = rnd.token_hex(int(arg))
        elif kind == "tpl":
            value = arg
        elif kind == "ed25519_seed":
            value = b64url(Ed25519PrivateKey.generate().private_bytes_raw())
        elif kind == "ed25519_pem":
            pem = Ed25519PrivateKey.generate().private_bytes(
                ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()
            )
            value = pem.decode()
        elif kind == "db_dsn":
            host = "${{approval-db.RAILWAY_PRIVATE_DOMAIN}}"
            db = self.spec["services"]["approval-db"]["env"]["POSTGRES_DB"]
            value = f"postgresql://{arg}:{self.role_password(arg)}@{host}:5432/{db}"
        elif kind == "tg_dsn":
            db = self.spec["services"]["telegram-db"]["env"]["POSTGRES_DB"]
            value = (
                f"postgresql://{arg}:{self.role_password(arg)}@${{{{telegram-db.RAILWAY_PRIVATE_DOMAIN}}}}:5432/{db}"
            )
        elif kind == "tg_role_passwords":
            value = json.dumps({r: self.role_password(r) for r in sorted(TG_ROLE_VAR)})
        elif kind == "superuser_dsn":
            env = self.spec["services"][arg]["env"]
            pw = self.get(arg, "POSTGRES_PASSWORD")
            if not pw:
                raise DeployError(f"{arg}: POSTGRES_PASSWORD is not set yet")
            value = f"postgresql://{env['POSTGRES_USER']}:{pw}@${{{{{arg}.RAILWAY_PRIVATE_DOMAIN}}}}:5432/{env['POSTGRES_DB']}"
        elif kind == "role_passwords":
            roles = {ROLE_OF[s] for s in self.spec["services"] if s in ROLE_OF}
            value = json.dumps({r: self.role_password(r) for r in sorted(roles)})
        elif kind == "ed25519_pub":
            value = "ed25519:" + b64url(self._public(service, arg))
        elif kind == "ed25519_identity_keys":
            secret_name, _, kid = arg.partition(":")
            value = f"{kid}=ed25519:{b64url(self._public(service, secret_name))}"
        elif kind == "pki":
            value = self.pki()[arg]
        elif (
            kind == "caller_keys"
        ):  # `<service>.<secret>:<kid>`: the public half of another service's key, `kid=ed25519:…`
            owner, _, rest = arg.partition(".")
            secret_name, _, kid = rest.partition(":")
            value = f"{kid}=ed25519:{b64url(self._public(owner, secret_name))}"
        elif kind == "keyset":
            set_name, _, member = arg.partition(".")
            value = self.keyset(set_name)[member]
        else:
            raise DeployError(f"{service}: unknown generator {kind}")
        self.put(service, name, value, secret=secret)

    # -- the whole plan
    def compute(self) -> None:  # noqa: PLR0912
        for set_name in ("exec-signing", "payload"):  # a keyset is all-or-nothing: its members must stay a pair
            members = self.keyset_members(set_name)
            present = [bool(self.existing.get(s, {}).get(v)) for s, v, _ in members]
            if members and any(present) and not all(present):
                raise DeployError(
                    f"keyset {set_name} is partly set; remove all its variables to rotate, or restore them"
                )
        pki_vars = self.pki_members()  # the CA key is not kept: a partly set PKI cannot be completed, only reissued
        present = [bool(self.existing.get(s, {}).get(v)) for s, v in pki_vars]
        if pki_vars and any(present) and not all(present):
            raise DeployError("the internal PKI is partly set; remove all its variables to reissue it, or restore them")
        for svc_name, svc in self.spec["services"].items():
            have = self.existing.get(svc_name, {})
            for var, value in (svc.get("env") or {}).items():  # enforced literals
                if have.get(var) != str(value):
                    self.put(svc_name, var, str(value), secret=False)
            dockerfile = (svc.get("source") or {}).get("dockerfile")
            if dockerfile and have.get("RAILWAY_DOCKERFILE_PATH") != dockerfile:
                self.put(svc_name, "RAILWAY_DOCKERFILE_PATH", dockerfile, secret=False)
            for var, spec in (svc.get("seed") or {}).items():  # written once, the owner edits them afterwards
                if var not in have:
                    value = (self.root / spec["file"]).read_text("utf-8") if "file" in spec else str(spec)
                    self.put(svc_name, var, value, secret=False)
            for var, sec in secrets_of(svc).items():
                if have.get(var):
                    self.out.add(have[var])
                elif sec.get("gen") != "owner":
                    self.generate(svc_name, var, str(sec["gen"]), secret=True)
            for var, gen in (svc.get("config") or {}).items():
                if not have.get(var):
                    self.generate(svc_name, var, str(gen), secret=False)
        # the role-password map follows the roles of the services (a new process adds a role, nothing is rotated)
        raw = self.get("db-init", "GXG_SECRET__GXG_ROLE_PASSWORDS")
        if raw and "db-init" in self.spec["services"]:
            roles = {ROLE_OF[s] for s in self.spec["services"] if s in ROLE_OF}
            current = json.loads(raw)
            if not roles <= set(current):
                self.put(
                    "db-init",
                    "GXG_SECRET__GXG_ROLE_PASSWORDS",
                    json.dumps({**current, **{r: self.role_password(r) for r in roles}}),
                    secret=True,
                )


# ---------------------------------------------------------------------------------------------------------- apply
class Deployer:
    def __init__(self, api: Railway, spec: dict[str, Any], out: Out, root: Path = ROOT) -> None:
        self.api, self.spec, self.out, self.root = api, spec, out, root
        self.project_id = ""
        self.env_id = ""
        self.service_ids: dict[str, str] = {}

    def discover(self, *, create: bool) -> None:
        name, env_name = self.spec["project"], self.spec["environment"]
        node = next((p for p in edges(self.api.call(Q_PROJECTS), "projects") if p["name"] == name), None)
        if node is None:
            if not create:
                raise DeployError(f"project {name} does not exist")
            node = self.api.call(
                Q_PROJECT_CREATE, input={"name": name, "defaultEnvironmentName": env_name, "isPublic": False}
            )["projectCreate"]
            self.out.say(f"created project {name}")
        env = next((e for e in edges(node, "environments") if e["name"] == env_name), None)
        if env is None:
            raise DeployError(f"project {name} has no environment {env_name}")
        self.project_id, self.env_id = node["id"], env["id"]
        self.service_ids = {s["name"]: s["id"] for s in edges(node, "services")}

    def source_input(self, svc: dict[str, Any]) -> dict[str, Any]:
        if svc["kind"] == "vault":
            return {}  # no source: the service only holds variables, nothing is built or run
        if svc.get("image"):
            return {"source": {"image": svc["image"]}}
        src = svc["source"]
        repo = src.get("repo") or self.spec["repo"]["name"]
        branch = src.get("branch") or self.spec["repo"]["branch"]
        return {"source": {"repo": repo}, "branch": branch}

    def repo_source(self, name: str) -> tuple[str, str] | None:
        """(repository, branch) a repository-built service must follow; None for image services."""
        svc = self.spec["services"][name]
        if svc.get("image") or svc["kind"] == "vault":
            return None
        src = svc["source"]
        return src.get("repo") or self.spec["repo"]["name"], src.get("branch") or self.spec["repo"]["branch"]

    def source_ok(self, name: str) -> bool:
        want = self.repo_source(name)
        if want is None:
            return True
        data = self.api.call(
            Q_TRIGGERS, projectId=self.project_id, environmentId=self.env_id, serviceId=self.service_ids[name]
        )
        triggers = [e["node"] for e in (data.get("deploymentTriggers") or {}).get("edges", [])]
        return any((t.get("repository"), t.get("branch")) == want for t in triggers)

    def ensure_source(self, name: str) -> None:
        """serviceCreate does not reliably pin the branch (it fell back to `main`): connect the service explicitly."""
        want = self.repo_source(name)
        if want is not None and not self.source_ok(name):
            self.api.call(Q_CONNECT, id=self.service_ids[name], input={"repo": want[0], "branch": want[1]})
            self.out.say(f"connected {name} to {want[0]}@{want[1]}")

    def apply(self) -> None:
        self.discover(create=True)
        for name, svc in self.spec["services"].items():
            if name not in self.service_ids:
                made = self.api.call(
                    Q_SERVICE_CREATE,
                    input={
                        "projectId": self.project_id,
                        "environmentId": self.env_id,
                        "name": name,
                        **self.source_input(svc),
                    },
                )
                self.service_ids[name] = made["serviceCreate"]["id"]
                self.out.say(f"created service {name}")
        existing = {n: self.read_variables(n) for n in self.spec["services"]}
        values = Values(self.spec, existing, self.out, self.root)
        values.compute()
        changed: list[str] = []
        for name in self.spec["services"]:
            writes = values.new[name]
            if writes:
                self.api.call(
                    Q_UPSERT,
                    input={
                        "projectId": self.project_id,
                        "environmentId": self.env_id,
                        "serviceId": self.service_ids[name],
                        "variables": writes,
                        "skipDeploys": True,
                    },
                )
                self.out.say(f"set variables on {name}: {', '.join(sorted(writes))}")
                changed.append(name)
            self.ensure_source(name)
            self.ensure_instance(name)
            self.ensure_volume(name)
            self.close_exposure(name)
        for name in (n for n in changed if self.spec["services"][n]["kind"] != "vault"):
            self.api.call(Q_REDEPLOY, serviceId=self.service_ids[name], environmentId=self.env_id)
            self.out.say(f"redeploy {name}")
        missing = self.owner_todo(values)
        if missing:
            self.out.say("owner still has to set (Railway UI): " + ", ".join(missing))
        self.out.say(f"done: {len(changed)} services changed" if changed else "done: nothing to change")

    def read_variables(self, name: str) -> dict[str, str]:
        if name not in self.service_ids:
            return {}
        data = self.api.call(
            Q_VARIABLES, projectId=self.project_id, environmentId=self.env_id, serviceId=self.service_ids[name]
        )
        return {k: str(v) for k, v in (data.get("variables") or {}).items()}

    def ensure_instance(self, name: str) -> None:
        svc = self.spec["services"][name]
        if svc["kind"] == "vault":
            return
        want: dict[str, Any] = {}
        if svc.get("start"):
            want["startCommand"] = svc["start"]
        elif svc["kind"] == "app":
            want["startCommand"] = (
                ""  # a service without `start` in the spec runs the image's own command (a maintenance override is cleared)
            )
        if svc.get("healthcheck"):
            want["healthcheckPath"] = svc["healthcheck"]
        elif svc["kind"] == "app":
            want["healthcheckPath"] = (
                ""  # (null is ignored by the API) an app without a healthcheck in the spec has none in Railway
            )
        if (svc.get("env") or {}).get("TS_HOSTNAME"):
            want["overlapSeconds"] = 0  # the old container leaves the tailnet before the new one joins: no `<name>-2`
        if svc["kind"] == "job":
            want["restartPolicyType"] = "NEVER"
        elif svc["kind"] in ("app", "postgres"):
            want["restartPolicyType"] = "ON_FAILURE"
            want["restartPolicyMaxRetries"] = 5  # a service that cannot start waits for the owner, it does not loop
        have = (
            self.api.call(Q_INSTANCE, serviceId=self.service_ids[name], environmentId=self.env_id).get(
                "serviceInstance"
            )
            or {}
        )
        diff = {k: v for k, v in want.items() if (have.get(k) or "") != (v or "")}
        if diff:
            self.api.call(Q_INSTANCE_UPDATE, serviceId=self.service_ids[name], environmentId=self.env_id, input=diff)
            self.out.say(f"updated settings of {name}: {', '.join(sorted(diff))}")

    def ensure_volume(self, name: str) -> None:
        mount = self.spec["services"][name].get("volume")
        if not mount:
            return
        project = self.api.call(Q_VOLUMES, projectId=self.project_id).get("project")
        for vol in edges(project, "volumes"):
            if any(i["serviceId"] == self.service_ids[name] for i in edges(vol, "volumeInstances")):
                return
        self.api.call(
            Q_VOLUME_CREATE,
            input={
                "projectId": self.project_id,
                "environmentId": self.env_id,
                "serviceId": self.service_ids[name],
                "mountPath": mount,
            },
        )
        self.out.say(f"created volume of {name} at {mount}")

    def exposure(self, name: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        sid = self.service_ids[name]
        doms = (
            self.api.call(Q_DOMAINS, projectId=self.project_id, environmentId=self.env_id, serviceId=sid).get("domains")
            or {}
        )
        tcp = self.api.call(Q_TCP, environmentId=self.env_id, serviceId=sid).get("tcpProxies") or []
        return list(doms.get("serviceDomains") or []), list(doms.get("customDomains") or []), list(tcp)

    def close_exposure(self, name: str) -> None:
        """SEC-28: a service never keeps a public domain or TCP proxy, whoever added it."""
        service_domains, custom_domains, tcp = self.exposure(name)
        for d in service_domains:
            self.api.call(Q_DEL_SERVICE_DOMAIN, id=d["id"])
            self.out.say(f"deleted Railway domain of {name}")
        for d in custom_domains:
            self.api.call(Q_DEL_CUSTOM_DOMAIN, id=d["id"])
            self.out.say(f"deleted custom domain of {name}")
        for p in tcp:
            self.api.call(Q_DEL_TCP, id=p["id"])
            self.out.say(f"deleted TCP proxy of {name}")

    def owner_todo(self, values: Values) -> list[str]:
        todo = []
        for name, svc in self.spec["services"].items():
            have = {**values.existing.get(name, {}), **values.new[name]}
            wanted = (
                [v for v, s in secrets_of(svc).items() if s.get("gen") == "owner"]
                + list(svc.get("owner_env") or [])
                + list(svc.get("build_args") or {})
            )
            todo += [f"{name}.{v}" for v in wanted if not have.get(v)]
        return todo

    def finalize(self) -> None:
        self.discover(create=False)
        for name, svc in self.spec["services"].items():
            if svc.get("transient") and name in self.service_ids:
                self.api.call(Q_SERVICE_DELETE, id=self.service_ids[name], environmentId=self.env_id)
                self.out.say(f"deleted transient service {name} (its variables went with it)")
        self.out.say("finalize: done")

    # -- AC-M14-11
    def verify(self) -> bool:
        self.discover(create=False)
        ok = True

        def check(good: bool, service: str, what: str) -> None:
            nonlocal ok
            ok &= good
            self.out.say(f"{'PASS' if good else 'FAIL'} {service}: {what}")

        shared = {
            k
            for k in (
                self.api.call(Q_VARIABLES, projectId=self.project_id, environmentId=self.env_id).get("variables") or {}
            )
        }
        for name in sorted(shared & set(FOREIGN_SHARED)):
            self.out.say(
                f"NOTE (project): shared variable {name} is not ours, the owner decides ({FOREIGN_SHARED[name]})"
            )
        shared -= set(FOREIGN_SHARED)
        check(not shared, "(project)", "no shared variables of ours (SEC-28: secrets are per service)")
        all_secrets = {v for s in self.spec["services"].values() for v in secrets_of(s)}
        for name, svc in self.spec["services"].items():
            if svc.get("transient"):
                check(name not in self.service_ids, name, "transient job removed (run --finalize)")
                continue
            if name not in self.service_ids:
                check(False, name, "service exists")
                continue
            if self.repo_source(name) is not None:
                want = self.repo_source(name)
                check(self.source_ok(name), name, f"built from {want[0]}@{want[1]}" if want else "source")
            service_domains, custom_domains, tcp = self.exposure(name)
            check(not service_domains, name, "no Railway public domain")
            check(not custom_domains, name, "no custom domain")
            check(not tcp, name, "no TCP proxy")
            actual = {
                k: str(v)
                for k, v in self.api.call(
                    Q_VARIABLES, projectId=self.project_id, environmentId=self.env_id, serviceId=self.service_ids[name]
                )
                .get("variables", {})
                .items()
                if not k.startswith("RAILWAY_") or k == "RAILWAY_DOCKERFILE_PATH"
            }
            check(
                not any(k.startswith("RAILWAY_TCP_") or k == "DATABASE_PUBLIC_URL" for k in actual),
                name,
                "no TCP proxy variables",
            )
            for url in svc.get("internal_urls") or []:
                check(is_private(actual.get(url, "")), name, f"{url} points to *.railway.internal")
            declared_secret = set(secrets_of(svc))
            actual_secret = {
                k for k in actual if k.startswith(SECRET_PREFIX) or k in all_secrets or k in declared_secret
            }
            check(
                actual_secret == declared_secret,
                name,
                f"secret variables equal the declared set (AS-28): extra={sorted(actual_secret - declared_secret)} missing={sorted(declared_secret - actual_secret)}",
            )
            if svc["kind"] != "postgres":
                extra = sorted(set(actual) - declared_names(svc))
                check(not extra, name, f"no undeclared variables: {extra}")
            check(all(actual.get(k) for k in declared_secret & set(actual)), name, "secret values are not empty")
            for var, gen in (svc.get("config") or {}).items():  # a public half taken from another service's key
                if str(gen).startswith("caller_keys:"):
                    check(
                        self.caller_keys_match(str(gen), actual.get(var)),
                        name,
                        f"{var} is the public half of {str(gen).split(':')[1]}",
                    )
        return ok

    def caller_keys_match(self, gen: str, live: str | None) -> bool:
        """`caller_keys:<service>.<secret>:<kid>`: the live value equals the public half of the live secret."""
        owner, _, rest = gen.partition(":")[2].partition(".")
        secret_name, _, kid = rest.partition(":")
        raw = self.api.call(
            Q_VARIABLES, projectId=self.project_id, environmentId=self.env_id, serviceId=self.service_ids[owner]
        ).get("variables", {})
        if not live or not raw.get(secret_name):
            return False
        pub = Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(str(raw[secret_name]))).public_key()
        return live == f"{kid}=ed25519:{b64url(pub.public_bytes_raw())}"


# ---------------------------------------------------------------------------------------------------------- plan
def dry_run(spec: dict[str, Any], out: Out) -> int:
    problems = validate_spec(spec)
    out.say(f"project {spec['project']}, environment {spec['environment']}, tenant {spec['tenant']}")
    out.say("public exposure: none (no Railway domain, no custom domain, no TCP proxy); Funnel off")
    for name, svc in spec["services"].items():
        src = svc.get("image") or (svc.get("source") or {}).get("repo") or spec["repo"]["name"]
        what = svc.get("image") or f"{src} {(svc.get('source') or {}).get('dockerfile', '')}"
        out.say(f"- {name} [{svc['kind']}{', transient' if svc.get('transient') else ''}] {what.strip()}")
        out.say(
            f"    host: {name}.railway.internal"
            + (f":{svc['port']}" if svc.get("port") else "")
            + (f", start: {svc['start']}" if svc.get("start") else "")
        )
        if svc.get("volume"):
            out.say(f"    volume: {svc['volume']}")
        out.say(f"    literals: {', '.join(sorted(svc.get('env') or {})) or '-'}")
        for var, sec in secrets_of(svc).items():
            how = (
                "owner sets it in Railway"
                if sec.get("gen") == "owner"
                else f"generated ({str(sec['gen']).split(':')[0]})"
            )
            out.say(f"    secret {var}: {how}" + (f", AS-28 {sec['as28']}" if sec.get("as28") else ""))
        for var in list(svc.get("owner_env") or []) + list(svc.get("build_args") or {}):
            out.say(f"    owner variable {var}")
        for var in list(svc.get("seed") or {}) + list(svc.get("config") or {}):
            out.say(f"    generated/seeded once {var}")
    for p in problems:
        out.say(f"PROBLEM {p}", err=True)
    out.say("dry-run: no API calls made" + (f", {len(problems)} problem(s)" if problems else ""))
    return 1 if problems else 0


def main(argv: list[str] | None = None, *, transport: Transport | None = None, out: Out | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan, no API calls")
    mode.add_argument(
        "--verify", action="store_true", help="AC-M14-11: no public exposure, private URLs, AS-28 secret sets"
    )
    mode.add_argument("--finalize", action="store_true", help="delete the transient db-init job")
    ap.add_argument("--spec", type=Path, default=SPEC)
    args = ap.parse_args(argv)
    out = out or Out()
    try:
        spec = load_spec(args.spec)
        if args.dry_run:
            return dry_run(spec, out)
        problems = validate_spec(spec)
        if problems:
            for p in problems:
                out.say(f"PROBLEM {p}", err=True)
            return 1
        token = os.environ.get("RAILWAY_TOKEN")
        if token:
            out.add(token)  # a fake value is fine in the session: the proxy substitutes the real one
        if transport is None:
            if not token:
                raise DeployError("RAILWAY_TOKEN is not set")
            transport = http_transport(token)
        deployer = Deployer(Railway(transport), spec, out)
        if args.verify:
            return 0 if deployer.verify() else 1
        if args.finalize:
            deployer.finalize()
        else:
            deployer.apply()
        return 0
    except DeployError as exc:
        out.say(f"error: {exc}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
