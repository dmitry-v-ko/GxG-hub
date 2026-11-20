"""Local schema conformance smoke tests. Not an end-to-end security test."""
import json
from pathlib import Path
import unittest

import yaml

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / 'schemas'


def validator(name: str):
    with (SCHEMAS / name).open(encoding='utf-8') as f:
        schema = json.load(f)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


class EnvelopeContractTests(unittest.TestCase):
    def test_search_result(self):
        v = validator('search-result.schema.json')
        valid = {
            'items': [{'id': 'doc-1', 'excerpt': 'A bounded excerpt'}],
            'next_cursor': None, 'complete': True,
            'partial_reasons': [], 'source_refs': ['doc-1'],
        }
        v.validate(valid)
        with self.assertRaises(ValidationError):
            v.validate({**valid, 'complete': 'yes'})
        with self.assertRaises(ValidationError):
            v.validate({**valid, 'secret_token': 'not-allowed'})

    def test_effect_intent_rejects_model_claimed_approval(self):
        v = validator('effect-intent.schema.json')
        valid = {
            'intent_id': '11111111-1111-4111-8111-111111111111', 'tenant_id': 'tenant-1',
            'requester_principal': 'agent:one',
            'tool_key': 'telegram.message.send', 'tool_version': '1',
            'connection_id': 'telegram-account-1', 'resource_id': 'chat-1',
            'canonical_args_hash': 'a' * 64, 'args_ref': 'protected:1',
            'effect_class': 'external_effect', 'idempotency_key': 'req-unique-1234',
            'policy_version': 'policy-1',
            'created_at': '2026-11-20T14:00:00Z',
            'expires_at': '2026-11-20T14:15:00Z',
        }
        v.validate(valid)
        with self.assertRaises(ValidationError):
            v.validate({**valid, 'approved': True})
        with self.assertRaises(ValidationError):
            v.validate({**valid, 'effect_class': 'read'})
        with self.assertRaises(ValidationError):
            v.validate({**valid, 'canonical_args_hash': 'short'})

    def test_unknown_outcome_requires_reconciliation(self):
        v = validator('effect-result.schema.json')
        v.validate({'operation_id': 'op-1', 'status': 'outcome_unknown', 'reconciliation_required': True})
        with self.assertRaises(ValidationError):
            v.validate({'operation_id': 'op-1', 'status': 'outcome_unknown', 'reconciliation_required': False})
        v.validate({'operation_id': 'op-2', 'status': 'succeeded', 'reconciliation_required': False})


    def test_resource_grants_example_and_forbidden_masks(self):
        v = validator('resource-grants.schema.json')
        example = yaml.safe_load((ROOT / 'policies' / 'tenants' / 'example.yaml').read_text(encoding='utf-8'))
        v.validate(example)
        grant = example['grants'][0]
        for bad_rid in ['kb:kb-main/*', 'kb:*/collection/x', 'kb:kb-main/collection/sa*les']:
            with self.assertRaises(ValidationError):
                v.validate({**example, 'grants': [{**grant, 'resources': [bad_rid]}]})
        v.validate({**example, 'grants': [{**grant, 'resources': ['kb:kb-main/collection/*']}]})
        with self.assertRaises(ValidationError):
            v.validate({**example, 'grants': [{**grant, 'actions': ['execute']}]})
        with self.assertRaises(ValidationError):
            v.validate({**example, 'connections': [{**example['connections'][0], 'credential_ref': 'plain-token'}]})


if __name__ == '__main__':
    unittest.main()
