"""Resource grants: `GrantResolver` and the signed grants bundle (GR-01…GR-17, GR-11a).

Allow-only: anything not granted explicitly is denied. A RID is
`<domain>:<connection_id>/<type>/<id>`; a grant resource is an exact RID or the mask
`<domain>:<connection_id>/<type>/*` (GR-04). The bundle is `grants-bundle.json`
(JCS) plus `grants-bundle.json.sig` (`{"alg": "EdDSA", "kid", "sig"}`); verification
keys by `kid` come from service configuration, never from the bundle. The signed document
must match schemas/grants-bundle.schema.json.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import _jwt, _schema
from ._jwt import Clock
from .errors import GxgError
from .readiness import Readiness
from .svc_jwt import Service, parse_registry

Action = Literal["read", "collect", "write", "propose"]
MAX_BUNDLE_AGE_S = 24 * 3600  # GR-13

_RID = re.compile(r"([a-z][a-z0-9_]*):([a-z0-9][a-z0-9_-]*)/([a-z][a-z0-9_]*)/([^/*\s]+)")
_MASK = re.compile(r"([a-z][a-z0-9_]*):([a-z0-9][a-z0-9_-]*)/([a-z][a-z0-9_]*)/\*")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    grant_id: str | None = None  # GR-07: goes to the audit on allow


DENY = Decision(False)


@dataclass(frozen=True)
class AllowedSet:
    """RIDs allowed for one (connection, type): exact RIDs, or all of the type when a mask grants it."""

    all_of_type: bool
    rids: frozenset[str]


class GrantResolver(Protocol):
    def check(self, tenant: str, principal: str, action: Action, rid: str) -> Decision: ...

    def allowed(self, tenant: str, principal: str, action: Action, connection_id: str, rtype: str) -> AllowedSet: ...

    @property
    def version(self) -> str: ...


class BundleError(ValueError):
    """The bundle or its signature is invalid; the bundle is not used."""


@dataclass(frozen=True)
class _Grant:
    id: str
    subjects: frozenset[str]  # principals, groups expanded
    actions: frozenset[str]
    exact: frozenset[str]
    masks: frozenset[tuple[str, str, str]]  # (domain, connection, type)
    expires_at: datetime | None


@dataclass(frozen=True)
class TenantGrants:
    tenant: str
    connections: Mapping[str, str]  # connection_id → domain
    principals: frozenset[str]
    grants: tuple[_Grant, ...]


@dataclass(frozen=True)
class Bundle:
    version: str
    built_at: datetime
    tenants: Mapping[str, TenantGrants]
    approval_policies: Mapping[str, Mapping[str, Any] | None]
    platform: Mapping[str, Service]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must carry a time zone")
    return parsed


def parse_tenant(doc: Mapping[str, Any]) -> TenantGrants:
    """One tenant grants file, already valid by schemas/resource-grants.schema.json. Expands groups (GR-11a or raw)."""
    principals_doc = doc["principals"]
    principals = frozenset(p["id"] for kind in ("agents", "humans") for p in principals_doc.get(kind, []))
    groups: Mapping[str, list[str]] = doc.get("groups") or {}
    connections = {c["id"]: c["domain"] for c in doc["connections"]}
    grants = []
    for g in doc["grants"]:
        subjects: set[str] = set()
        for s in g["subjects"]:
            # An unknown group grants nobody (fail closed); lint (GR-09) reports it.
            subjects.update(groups.get(s[len("group:") :], []) if s.startswith("group:") else [s])
        exact, masks = set(), set()
        for r in g["resources"]:
            if _RID.fullmatch(r):
                exact.add(r)
            elif m := _MASK.fullmatch(r):
                masks.add((m[1], m[2], m[3]))
            else:
                # e.g. a trailing newline: the schema's `$` allows it, fullmatch does not
                raise ValueError(f"grant {g['id']!r}: resource is neither a RID nor a type mask (GR-04)")
        expires = g.get("expires_at")
        grants.append(
            _Grant(
                id=g["id"],
                subjects=frozenset(subjects),
                actions=frozenset(g["actions"]),
                exact=frozenset(exact),
                masks=frozenset(masks),
                expires_at=_parse_time(expires) if expires is not None else None,
            )
        )
    return TenantGrants(doc["tenant"], connections, principals, tuple(grants))


def _verify_signature(data: bytes, sig: bytes, keys: Mapping[str, Ed25519PublicKey]) -> None:
    try:
        envelope = json.loads(sig)
        if not isinstance(envelope, dict) or envelope.get("alg") != "EdDSA":
            raise BundleError("signature: alg must be EdDSA")
        kid = envelope.get("kid")
        key = keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise BundleError("signature: unknown kid")
        signature = _jwt.b64url_decode(envelope["sig"])
        key.verify(signature, data)
    except BundleError:
        raise
    except InvalidSignature:
        raise BundleError("signature: does not verify") from None
    except (ValueError, KeyError, TypeError):
        raise BundleError("signature: malformed") from None


def load_bundle(data: bytes, sig: bytes, keys: Mapping[str, Ed25519PublicKey], *, tenant: str | None = None) -> Bundle:
    """Verify the signature over the exact bytes of `data`, then parse (GR-11a). `tenant` keeps one slice (GR-12)."""
    _verify_signature(data, sig, keys)
    try:
        doc = json.loads(data.decode("utf-8"))
    except ValueError:
        raise BundleError("bundle: not UTF-8 JSON") from None
    error = next(_schema.validator("grants-bundle").iter_errors(doc), None)
    if error is not None:
        raise BundleError(f"bundle: does not match grants-bundle.schema.json at /{'/'.join(map(str, error.path))}")
    try:
        built_at = _parse_time(doc["built_at"])  # schema pins the form: YYYY-MM-DDTHH:MM:SSZ
        tenants: dict[str, TenantGrants] = {}
        policies: dict[str, Mapping[str, Any] | None] = {}
        for tid, entry in doc["tenants"].items():
            if tenant is not None and tid != tenant:
                continue
            parsed = parse_tenant(entry["grants"])
            policy = entry["approval_policy"]
            if parsed.tenant != tid or (policy is not None and policy.get("tenant") != tid):
                raise BundleError(f"bundle: tenant {tid!r} contains another tenant's file")
            tenants[tid] = parsed
            policies[tid] = policy
        if tenant is not None and tenant not in tenants:
            raise BundleError(f"bundle: no slice for tenant {tenant!r}")
        platform = parse_registry(doc["platform"])
    except BundleError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BundleError(f"bundle: malformed ({type(exc).__name__})") from None
    return Bundle(doc["bundle_version"], built_at, tenants, policies, platform)


class BundleStore:
    """Holds the current valid bundle; implements `GrantResolver` and readiness.

    A failed load keeps the previous valid bundle. A bundle older (`built_at`) than the
    loaded one, or than the newest `built_at` recorded in `state_file`, is rejected, so an
    old signed bundle cannot bring back a revoked grant, also across restarts (GR-13a).
    Without a valid bundle younger than 24 h the store is not ready and every call raises
    `temporarily_unavailable` (GR-12, GR-13).
    """

    def __init__(
        self,
        keys: Mapping[str, Ed25519PublicKey],
        *,
        state_file: Path,
        tenant: str | None = None,
        max_age_s: float = MAX_BUNDLE_AGE_S,
        now: Clock = time.time,
    ) -> None:
        self._keys = dict(keys)
        self._tenant = tenant
        self._max_age_s = max_age_s
        self._now = now
        self._bundle: Bundle | None = None
        self._last_error: str | None = None
        self._lock = threading.Lock()
        self._state_file = state_file
        # A missing file is a first start; an unreadable one stops the service (fail closed).
        self._floor = float(json.loads(state_file.read_text())["max_built_at"]) if state_file.exists() else None

    def load(self, data: bytes, sig: bytes) -> Bundle:
        try:
            bundle = load_bundle(data, sig, self._keys, tenant=self._tenant)
        except BundleError as exc:
            self._last_error = str(exc)
            raise
        if bundle.built_at.timestamp() > self._now() + _jwt.SKEW_S:
            # A future built_at would block every later bundle as a rollback and never go stale.
            self._last_error = "bundle: built_at is in the future"
            raise BundleError(self._last_error)
        with self._lock:
            t = bundle.built_at.timestamp()
            if self._floor is not None and t < self._floor:
                self._last_error = "bundle: older than a bundle loaded before (rollback)"
                raise BundleError(self._last_error)
            if self._floor != t:
                tmp = self._state_file.with_name(self._state_file.name + ".tmp")
                tmp.write_text(json.dumps({"max_built_at": t}))
                os.replace(tmp, self._state_file)
                self._floor = t
            self._bundle = bundle
            self._last_error = None
        return bundle

    def _age_s(self, bundle: Bundle) -> float:
        return self._now() - bundle.built_at.timestamp()

    def readiness(self) -> Readiness:
        bundle = self._bundle
        if bundle is None:
            return Readiness(False, (f"no valid grants bundle: {self._last_error or 'not loaded'}",))
        if self._age_s(bundle) > self._max_age_s:
            return Readiness(False, (f"grants bundle {bundle.version} is older than 24 h (GR-13)",))
        return Readiness(True)

    @property
    def current(self) -> Bundle:
        """The valid, fresh bundle, or `temporarily_unavailable`."""
        bundle = self._bundle
        if bundle is None or self._age_s(bundle) > self._max_age_s:
            raise GxgError("temporarily_unavailable", "grants bundle is not ready")
        return bundle

    @property
    def version(self) -> str:
        return self.current.version

    def registry(self) -> Mapping[str, Service]:
        """Platform registry of the current bundle (for `SvcJwtVerifier`)."""
        return self.current.platform

    def _candidates(self, tenant: str, principal: str, action: Action) -> tuple[TenantGrants, list[_Grant]] | None:
        t = self.current.tenants.get(tenant)
        if t is None or principal not in t.principals:
            return None
        now = datetime.fromtimestamp(self._now(), UTC)
        live = [
            g
            for g in t.grants
            if principal in g.subjects and action in g.actions and (g.expires_at is None or now < g.expires_at)
        ]
        return t, live

    def check(self, tenant: str, principal: str, action: Action, rid: str) -> Decision:
        found = self._candidates(tenant, principal, action)
        m = _RID.fullmatch(rid)
        if found is None or m is None:
            return DENY
        t, live = found
        domain, connection, rtype = m[1], m[2], m[3]
        if t.connections.get(connection) != domain:  # GR-02: connection of another tenant or domain
            return DENY
        for g in live:
            if rid in g.exact or (domain, connection, rtype) in g.masks:
                return Decision(True, g.id)
        return DENY

    def allowed(self, tenant: str, principal: str, action: Action, connection_id: str, rtype: str) -> AllowedSet:
        found = self._candidates(tenant, principal, action)
        if found is None or connection_id not in found[0].connections:
            return AllowedSet(False, frozenset())
        t, live = found
        domain = t.connections[connection_id]
        prefix = f"{domain}:{connection_id}/{rtype}/"
        rids = frozenset(r for g in live for r in g.exact if r.startswith(prefix))
        return AllowedSet(any((domain, connection_id, rtype) in g.masks for g in live), rids)
