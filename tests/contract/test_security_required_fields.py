"""AC-M0-02: semantic guard for security-critical `required` fields.

Independent of tests/contract/schema-lock.json: regenerating the lock with
`schema_lock.py --write` does not make these tests pass. Each field is pinned
to the requirement that needs it. To change this list, change the spec first
(docs/specs/effects-and-approval-v1.md) and get a security review (CODEOWNERS).
"""

import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / 'schemas'

# field -> requirement IDs (docs/specs/effects-and-approval-v1.md)
EFFECT_INTENT_CRITICAL: dict[str, str] = {
    'idempotency_key': 'EFF-08 (uniqueness of propose by (tenant_id, idempotency_key)), EFF-18 p.5',
    'canonical_args_hash': 'EFF-11 (decision bound to intent_id + canonical_args_hash), EFF-18 p.2',
    'tenant_id': 'EFF-08 (idempotency scope), EFF-10 (cross-tenant approval forbidden)',
    'expires_at': 'EFF-16 (an expired intent is never reserved for execution)',
    'intent_id': 'EFF-11 (decision bound to intent_id)',
    'requester_principal': 'EFF-10 (self-approval forbidden)',
    'effect_class': 'EFF-09 (policy is evaluated by effect_class)',
}
EXECUTION_TOKEN_CRITICAL: dict[str, str] = {
    'jti': 'EFF-18 p.1 (jti replay cache), EFF-20',
    'aud': 'EFF-18 p.1 (aud = own connector ID)',
    'exp': 'EFF-18 p.1 (exp checked), Execution token: exp - iat <= 60 s',
    'ahs': 'EFF-18 p.2 (args hash compared with ahs)',
    'iss': 'EFF-18 p.1 (iss = gxg-executor)',
    'ten': 'EFF-18 p.3 (tenant matches connector configuration)',
    'idk': 'EFF-18 p.5 (idempotency_key passed to the provider)',
}

VALID_INTENT: dict[str, Any] = {
    'intent_id': '11111111-1111-4111-8111-111111111111',
    'tenant_id': 'tenant-1',
    'requester_principal': 'agent:one',
    'tool_key': 'telegram.message.send',
    'tool_version': '1',
    'connection_id': 'telegram-account-1',
    'resource_id': 'chat-1',
    'canonical_args_hash': 'a' * 64,
    'args_ref': 'protected:1',
    'effect_class': 'external_effect',
    'idempotency_key': 'req-unique-1234',
    'policy_version': 'policy-1',
    'created_at': '2026-11-20T14:00:00Z',
    'expires_at': '2026-11-20T14:15:00Z',
}
VALID_TOKEN: dict[str, Any] = {
    'iss': 'gxg-executor',
    'aud': 'demo',
    'sub': '11111111-1111-4111-8111-111111111111',
    'jti': '22222222-2222-4222-8222-222222222222',
    'ten': 'tenant-1',
    'req': 'agent:one',
    'tool': 'demo.note.create@1',
    'con': 'demo-conn-1',
    'res': 'note-1',
    'ahs': 'a' * 64,
    'idk': 'req-unique-1234',
    'iat': 1_800_000_000,
    'exp': 1_800_000_060,
}

# Values that must be rejected for a field even when present.
BAD_VALUES: dict[str, dict[str, list[Any]]] = {
    'effect-intent.schema.json': {
        'idempotency_key': ['', None, 1],
        'canonical_args_hash': ['', 'short', 'A' * 64, 'g' * 64, None],
        'tenant_id': ['', None],
        'expires_at': ['', 'tomorrow', None, 1],
        'intent_id': ['', None],
    },
    'execution-token.schema.json': {
        'jti': ['', None],
        'aud': ['', None],
        'exp': ['1800000060', None, 1.5],
        'ahs': ['', 'short', 'A' * 64, None],
        'iss': ['gxg-approval', ''],
        'ten': ['', None],
    },
}


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


class SecurityCriticalRequiredFields(unittest.TestCase):
    cases = (
        ('effect-intent.schema.json', VALID_INTENT, EFFECT_INTENT_CRITICAL),
        ('execution-token.schema.json', VALID_TOKEN, EXECUTION_TOKEN_CRITICAL),
    )

    def test_valid_documents_are_accepted(self):
        for name, valid, _ in self.cases:
            with self.subTest(schema=name):
                _validator(name).validate(valid)

    def test_critical_fields_are_listed_in_required(self):
        for name, _, critical in self.cases:
            required = set(json.loads((SCHEMAS / name).read_text(encoding='utf-8')).get('required', []))
            for field, req in critical.items():
                with self.subTest(schema=name, field=field):
                    self.assertIn(field, required, f'{name}: `{field}` must be required ({req})')

    def test_document_without_critical_field_is_rejected(self):
        for name, valid, critical in self.cases:
            v = _validator(name)
            for field, req in critical.items():
                with self.subTest(schema=name, field=field):
                    doc = {k: val for k, val in valid.items() if k != field}
                    with self.assertRaises(ValidationError, msg=f'{name} without `{field}` accepted ({req})'):
                        v.validate(doc)

    def test_bad_values_of_critical_fields_are_rejected(self):
        for name, valid, _ in self.cases:
            v = _validator(name)
            for field, values in BAD_VALUES[name].items():
                for value in values:
                    with self.subTest(schema=name, field=field, value=value):
                        with self.assertRaises(ValidationError):
                            v.validate({**valid, field: value})

    def test_unknown_fields_are_rejected(self):
        # A smuggled claim such as `approved: true` must not validate (MCC-02: `approved` in arguments grants nothing).
        for name, valid, _ in self.cases:
            with self.subTest(schema=name):
                with self.assertRaises(ValidationError):
                    _validator(name).validate({**valid, 'approved': True})

    def test_expires_at_is_checked_as_date_time(self):
        # FormatChecker must actually enforce date-time; otherwise `expires_at: "tomorrow"` slips through.
        v = _validator('effect-intent.schema.json')
        self.assertFalse(v.is_valid({**VALID_INTENT, 'expires_at': '2026-13-45T99:00:00Z'}))


if __name__ == '__main__':
    unittest.main()
