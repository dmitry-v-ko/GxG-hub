"""AC-M0-02: a normative change to any JSON Schema fails CI unless the lock is
regenerated deliberately in the same change (scripts/ci/schema_lock.py)."""

import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / 'schemas'
LOCK = Path(__file__).with_name('schema-lock.json')
HINT = (
    'Schema differs from tests/contract/schema-lock.json. Do NOT start by regenerating the lock. '
    '1) Find the requirement in docs/specs/ that this schema change implements; if the spec does not '
    'call for it, revert the schema change. 2) If the spec must change, change the spec first in the '
    'same PR (weakening `required`, `additionalProperties`, a pattern or an enum needs a security '
    'review by CODEOWNERS). 3) Only then refresh the lock with '
    '`uv run python scripts/ci/schema_lock.py --write`. Security-critical fields are also guarded by '
    'tests/contract/test_security_required_fields.py, which the lock does not affect.'
)


def _lock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location('schema_lock', ROOT / 'scripts' / 'ci' / 'schema_lock.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SchemaLock(unittest.TestCase):
    lock: dict[str, Any]
    documents: dict[str, str]
    mod: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        full = json.loads(LOCK.read_text(encoding='utf-8'))
        cls.lock = full['schemas']
        cls.documents = full['documents']
        cls.mod = _lock_module()

    def test_set_of_schemas_matches_lock(self):
        actual = sorted(p.name for p in SCHEMAS.glob('*.schema.json'))
        self.assertEqual(actual, sorted(self.lock), HINT)

    def test_required_fields_match_lock(self):
        for name, entry in sorted(self.lock.items()):
            with self.subTest(schema=name):
                schema = json.loads((SCHEMAS / name).read_text(encoding='utf-8'))
                self.assertEqual(self.mod.required_map(schema), entry['required'], HINT)

    def test_normative_content_matches_lock(self):
        for name, entry in sorted(self.lock.items()):
            with self.subTest(schema=name):
                schema = json.loads((SCHEMAS / name).read_text(encoding='utf-8'))
                self.assertEqual(self.mod.schema_entry(schema)['normative_sha256'], entry['normative_sha256'], HINT)

    def test_openapi_documents_match_lock(self):
        actual = sorted(p.name for p in SCHEMAS.glob('*.openapi.yaml'))
        self.assertEqual(actual, sorted(self.documents), HINT)
        for name, digest in sorted(self.documents.items()):
            with self.subTest(document=name):
                self.assertEqual(self.mod.document_sha256(SCHEMAS / name), digest, HINT)

    def test_every_object_schema_has_top_level_required(self):
        for name in sorted(self.lock):
            with self.subTest(schema=name):
                self.assertTrue(self.lock[name]['required'].get('/'), f'{name}: top-level `required` is missing')

    def test_annotations_do_not_affect_lock_but_property_names_do(self):
        base = {
            'type': 'object',
            'required': ['description'],
            'properties': {'description': {'type': 'string', 'description': 'x'}},
        }
        reworded = {**base, 'title': 'New title', 'properties': {'description': {'type': 'string', 'description': 'y'}}}
        renamed = {**base, 'properties': {'summary': {'type': 'string'}}}
        h = lambda s: self.mod.schema_entry(s)['normative_sha256']  # noqa: E731
        self.assertEqual(h(base), h(reworded))
        self.assertNotEqual(h(base), h(renamed))


if __name__ == '__main__':
    unittest.main()
