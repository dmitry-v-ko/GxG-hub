"""Loader of `gxg-capabilities.yaml`, the ext.* registry (MCC-11 п.1-3, п.6; MC-06).

The registry is an explicit allowlist: an entry without effect class, schema, `action`
or resource type, a forbidden name, a duplicate name, or a non-`read` entry enabled by
default makes the whole load fail, so the connector does not start (fail closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._schema import validator


class RegistryError(ValueError):
    """The registry is invalid; the connector must not start or publish entries."""


@dataclass(frozen=True)
class Capability:
    name: str
    version: str
    description: str
    input_schema: str
    effect_class: str
    action: str
    resource: Mapping[str, Any]
    enabled_default: bool
    propose_tool: str | None
    group: str | None
    limits: Mapping[str, Any]


@dataclass(frozen=True)
class Registry:
    connector: str
    entries: Mapping[str, Capability]

    def get(self, name: str) -> Capability | None:
        return self.entries.get(name)


def load_registry(doc: object) -> Registry:
    """Validate a parsed `gxg-capabilities.yaml` and index it by entry name."""
    errors = sorted(validator("gxg-capabilities").iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise RegistryError(f"invalid capabilities registry at {where}: {e.message}")
    if not isinstance(doc, dict):  # unreachable: the schema requires an object
        raise RegistryError("registry must be a mapping")
    entries: dict[str, Capability] = {}
    for raw in doc["capabilities"]:
        name = raw["name"]
        if name in entries:
            raise RegistryError(f"duplicate capability name: {name}")
        entries[name] = Capability(
            name=name,
            version=raw["version"],
            description=raw["description"],
            input_schema=raw["input_schema"],
            effect_class=raw["effect_class"],
            action=raw["action"],
            resource=raw["resource"],
            enabled_default=raw.get("enabled_default", False),
            propose_tool=raw.get("propose_tool"),
            group=raw.get("group"),
            limits=raw.get("limits", {}),
        )
    return Registry(connector=doc["connector"], entries=entries)


def capability_group(cap: Capability) -> str:
    """The grant action an entry needs (MCC-11 п.5, GR-19): `ext:<class>[:<group>]`."""
    base = f"ext:{cap.effect_class}"
    return f"{base}:{cap.group}" if cap.group else base
