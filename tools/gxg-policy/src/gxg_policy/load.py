"""Read `policies/` (tenants, approval, platform) and validate files against the repo JSON Schemas."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import yaml

# shortcut: schemas are read from the repository checkout (tools/gxg-policy/src/gxg_policy -> repo root),
# upgrade to package data if gxg-policy is ever installed outside this repo.
REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMAS = REPO_ROOT / "schemas"

KINDS = {
    "tenants": "resource-grants.schema.json",
    "approval": "approval-policy.schema.json",
    "platform": "platform-registry.schema.json",
}


@dataclass(frozen=True)
class Finding:
    file: str
    path: str
    code: str
    message: str
    level: str = "error"

    def __str__(self) -> str:
        return f"{self.file}: {self.path or '<root>'}: [{self.code}] {self.message}"


@dataclass
class Policies:
    root: Path
    tenants: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)  # tenant -> (file, doc)
    approval: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    platform: tuple[str, dict[str, Any]] | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]


class _Loader(yaml.SafeLoader):
    """SafeLoader without timestamp conversion and with duplicate keys rejected."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _mapping(loader: _Loader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return dict(loader.construct_pairs(node))


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader subclass


def parse_ts(value: str) -> datetime:
    """RFC 3339 timestamp with an explicit offset, as UTC."""
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError(f"{value!r} has no UTC offset")
    return ts.astimezone(UTC)


def json_path(parts: Any) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


_validators: dict[str, jsonschema.Draft202012Validator] = {}


def validator(kind: str) -> jsonschema.Draft202012Validator:
    if kind not in _validators:
        schema = json.loads((SCHEMAS / KINDS[kind]).read_text(encoding="utf-8"))
        _validators[kind] = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
        )
    return _validators[kind]


def _schema_findings(rel: str, kind: str, doc: Any) -> list[Finding]:
    out = []
    for err in sorted(validator(kind).iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))):
        # RIDs with `*` anywhere but the last segment fail the RID pattern: name it (GR-04).
        mask = err.validator == "pattern" and isinstance(err.instance, str) and "*" in err.instance
        msg = (
            f"{err.instance!r}: `*` is allowed only as `<domain>:<connection>/<type>/*` (GR-04)"
            if mask
            else err.message
        )
        out.append(Finding(rel, json_path(err.absolute_path), "forbidden-mask" if mask else "schema", msg))
    return out


def load(root: Path) -> Policies:
    """Load and schema-validate every `*.yaml` under `root/{tenants,approval,platform}`."""
    pol = Policies(root)
    if not root.is_dir():
        pol.findings.append(Finding(str(root), "", "io", "not a directory"))
        return pol
    for path in sorted(root.rglob("*.y*ml")):
        rel = path.relative_to(root).as_posix()
        kind = rel.split("/", 1)[0]
        if kind not in KINDS or "/" not in rel:
            pol.findings.append(Finding(rel, "", "unknown-file", "expected tenants/, approval/ or platform/"))
            continue
        try:
            doc = parse_yaml(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            pol.findings.append(Finding(rel, "", "yaml", str(exc).replace("\n", " ")))
            continue
        bad = _schema_findings(rel, kind, doc)
        for i, g in enumerate(doc.get("grants", []) if kind == "tenants" and not bad else []):
            try:
                parse_ts(g.get("expires_at", "1970-01-01T00:00:00Z"))
            except ValueError as exc:
                bad.append(Finding(rel, f"grants[{i}].expires_at", "schema", str(exc)))
        pol.findings += bad
        if bad:
            continue
        if kind == "platform":
            if pol.platform is not None:
                pol.findings.append(Finding(rel, "", "duplicate-id", f"second platform registry ({pol.platform[0]})"))
            else:
                pol.platform = (rel, doc)
            continue
        table = pol.tenants if kind == "tenants" else pol.approval
        if doc["tenant"] in table:
            pol.findings.append(
                Finding(rel, "tenant", "duplicate-id", f"tenant {doc['tenant']!r} also in {table[doc['tenant']][0]}")
            )
        else:
            table[doc["tenant"]] = (rel, doc)
    return pol
