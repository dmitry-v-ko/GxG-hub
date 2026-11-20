"""scripts/ci/schema_lock.py CLI (AC-M0-02): CI fails while the lock is stale."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scripts.ci import schema_lock

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lock_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    mod = schema_lock
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOCK", tmp_path / "lock.json")
    return mod


def test_write_then_check_passes(lock_mod: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert lock_mod.main(["--write"]) == 0
    assert "wrote lock.json" in capsys.readouterr().out
    assert lock_mod.LOCK.read_text(encoding="utf-8") == (ROOT / "tests" / "contract" / "schema-lock.json").read_text(
        encoding="utf-8"
    )
    assert lock_mod.main([]) == 0
    assert "schema lock is up to date" in capsys.readouterr().out


@pytest.mark.parametrize("content", [None, "{}\n"], ids=["missing", "stale"])
def test_missing_or_stale_lock_fails(
    lock_mod: ModuleType, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    if content is not None:
        lock_mod.LOCK.write_text(content, encoding="utf-8")
    assert lock_mod.main([]) == 1
    assert "schema lock is out of date" in capsys.readouterr().err


STALE = (
    "schema lock is out of date. First make sure docs/specs/ calls for this schema change "
    "(change the spec first, CODEOWNERS review for any weakening); only then run "
    "`uv run python scripts/ci/schema_lock.py --write`.\n"
)


def test_stale_lock_message_is_exact(lock_mod: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert lock_mod.main([]) == 1
    cap = capsys.readouterr()
    assert cap.err == STALE
    assert cap.out == ""
    assert lock_mod.main(["--write"]) == 0
    capsys.readouterr()
    assert lock_mod.main([]) == 0
    assert capsys.readouterr().out == "schema lock is up to date\n"


def test_required_map_escapes_pointer_tokens() -> None:
    doc = {"properties": {"a/b~c": {"required": ["x"]}}, "required": ["a/b~c"]}
    assert schema_lock.required_map(doc) == {"/": ["a/b~c"], "/properties/a~1b~0c": ["x"]}


def test_lists_are_walked_only_under_subschema_keywords() -> None:
    doc = {
        "allOf": [{"title": "t", "required": ["a"]}],
        "enum": [{"title": "kept", "required": ["not-a-schema"]}],
    }
    assert schema_lock.normative(doc) == {"allOf": [{"required": ["a"]}], "enum": doc["enum"]}
    assert schema_lock.required_map(doc) == {"/allOf/0": ["a"]}


# Every subschema keyword of JSON Schema 2020-12 (plus draft-07 leftovers), spelled out here and not
# taken from schema_lock: mutmut does not mutate its module-level keyword tables, this test does.
SUB = {"required": ["r"], "title": "t"}
ONE = ["items", "not", "if", "then", "else", "additionalProperties", "contains", "propertyNames"]
ONE += ["unevaluatedProperties", "unevaluatedItems", "additionalItems"]
NAMED = ["properties", "patternProperties", "$defs", "definitions", "dependentSchemas"]
LIST = ["allOf", "anyOf", "oneOf", "prefixItems"]
EVERY_KEYWORD: list[tuple[str, Any, str]] = [
    *((k, SUB, f"/{k}") for k in ONE),
    *((k, {"n": SUB}, f"/{k}/n") for k in NAMED),
    *((k, [SUB], f"/{k}/0") for k in LIST),
]


@pytest.mark.parametrize(("keyword", "value", "pointer"), EVERY_KEYWORD, ids=[k for k, _, _ in EVERY_KEYWORD])
def test_every_subschema_keyword_is_walked(keyword: str, value: Any, pointer: str) -> None:
    doc = {keyword: value}
    assert schema_lock.required_map(doc) == {pointer: ["r"]}
    assert '"title"' not in json.dumps(schema_lock.normative(doc))


def test_hashes_keep_non_ascii_as_utf8(tmp_path: Path) -> None:
    entry = schema_lock.schema_entry({"enum": ["ё"]})
    want = hashlib.sha256('{"enum":["ё"]}'.encode()).hexdigest()
    assert entry["normative_sha256"] == want
    doc = tmp_path / "x.openapi.yaml"
    doc.write_text("a: ё\nwhen: 2026-11-20\n", encoding="utf-8")  # YAML dates need default=str
    assert schema_lock.document_sha256(doc) == hashlib.sha256('{"a":"ё","when":"2026-11-20"}'.encode()).hexdigest()


def test_render_keeps_non_ascii() -> None:
    assert schema_lock.render({"b": "ё", "a": 1}) == '{\n  "a": 1,\n  "b": "ё"\n}\n'
