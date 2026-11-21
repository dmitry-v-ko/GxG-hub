"""JSON Schema validators for the documents the kit reads at runtime.

`schemas/` in this package holds copies of the repository's `schemas/*.schema.json`
(tests/test_schema.py fails if a copy differs), so the kit works from a wheel too.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

_FILES = (
    "grants-bundle",
    "resource-grants",
    "approval-policy",
    "platform-registry",
    "gxg-manifest",
    "execution-token",
    # Approval Service request/response bodies (M7)
    "intent-create",
    "preview",
    "intent-status",
    "effect-result",
    # connector internal HTTP (M8)
    "execute-request",
    "reconcile-result",
)


@cache
def _registry() -> Registry:
    docs = [json.loads((files(__package__) / "schemas" / f"{n}.schema.json").read_text("utf-8")) for n in _FILES]
    return Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs)


@cache
def validator(name: str) -> Draft202012Validator:
    """Validator for `schemas/<name>.schema.json`; `$ref`s resolve among the copies above."""
    registry = _registry()
    schema = registry.contents(f"https://schemas.gxg.example/hub/v1/{name}.schema.json")
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
