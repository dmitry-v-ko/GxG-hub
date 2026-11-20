"""Executable checks for machine-readable spec artifacts (schemas, examples, vectors, OpenAPI).
Not an end-to-end security test."""
import hashlib
import json
import unicodedata
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / 'schemas'


def _registry():
    reg = Registry()
    for p in SCHEMAS.glob('*.schema.json'):
        doc = json.loads(p.read_text(encoding='utf-8'))
        res = Resource.from_contents(doc)
        reg = reg.with_resource(doc['$id'], res).with_resource(p.name, res)
    return reg


REGISTRY = _registry()


def validator(name):
    schema = json.loads((SCHEMAS / name).read_text(encoding='utf-8'))
    return Draft202012Validator(schema, registry=REGISTRY, format_checker=FormatChecker())


def load_yaml(rel):
    return yaml.safe_load((ROOT / rel).read_text(encoding='utf-8'))


H = 'a' * 64
PREVIEW = {
    'schema_version': 1, 'title': 'Отправка демо-сообщения',
    'target': {'label': 'Доска a-public', 'rid': 'demo:demo-main/board/a-public'},
    'fields': [{'label': 'Получатель', 'value': 'alice_01', 'kind': 'identifier'},
               {'label': 'Текст', 'value': 'Привет!', 'kind': 'multiline'}],
}


class SchemaArtifacts(unittest.TestCase):
    def test_all_schemas_are_valid_2020_12(self):
        names = sorted(p.name for p in SCHEMAS.glob('*.schema.json'))
        self.assertGreaterEqual(len(names), 15)
        for n in names:
            with self.subTest(schema=n):
                Draft202012Validator.check_schema(json.loads((SCHEMAS / n).read_text(encoding='utf-8')))

    def test_policy_examples(self):
        validator('approval-policy.schema.json').validate(load_yaml('policies/approval/example.yaml'))
        validator('platform-registry.schema.json').validate(load_yaml('policies/platform/registry.example.yaml'))
        validator('gxg-manifest.schema.json').validate(load_yaml('schemas/examples/demo.gxg-manifest.yaml'))

    def test_auto_approve_requires_limits_and_no_quorum(self):
        v = validator('approval-policy.schema.json')
        base = load_yaml('policies/approval/example.yaml')
        rule = {'id': 'r', 'match': {'tools': ['x.y.propose']}, 'decision': 'auto_approve'}
        with self.assertRaises(ValidationError):
            v.validate({**base, 'rules': [rule]})
        with self.assertRaises(ValidationError):
            v.validate({**base, 'rules': [{**rule, 'limits': {'max_per_hour': 1}, 'approvals': 2}]})
        v.validate({**base, 'rules': [{**rule, 'limits': {'max_per_hour': 1}}]})

    def test_manifest_effect_tools_must_be_propose_with_executor(self):
        v = validator('gxg-manifest.schema.json')
        m = load_yaml('schemas/examples/demo.gxg-manifest.yaml')
        eff = m['tools'][3]
        bad = {k: x for k, x in eff.items() if k != 'executes_via'}
        with self.assertRaises(ValidationError):
            v.validate({**m, 'tools': [bad]})
        with self.assertRaises(ValidationError):
            v.validate({**m, 'tools': [{**eff, 'action': 'read'}]})

    def test_registry_connector_cannot_serve_all_tenants(self):
        v = validator('platform-registry.schema.json')
        r = load_yaml('policies/platform/registry.example.yaml')
        conn = r['services'][2]
        with self.assertRaises(ValidationError):
            v.validate({**r, 'services': [{**conn, 'tenants': ['*']}]})

    def test_intent_create_and_status(self):
        validator('intent-create.schema.json').validate({
            'tool_key': 'demo.message.propose', 'tool_version': '1.0.0', 'connection_id': 'demo-main',
            'resource_id': 'demo:demo-main/board/a-public', 'idempotency_key': 'cand-1:v1',
            'canonical_args': {'recipient': 'alice_01', 'text': 'Привет!'}, 'canonical_args_hash': H,
            'preview': PREVIEW, 'requester': {'principal': 'agent:t-alpha:actor', 'tenant_id': 't-alpha'}})
        v = validator('intent-status.schema.json')
        status = {'intent_id': 'i-1', 'tenant_id': 't-alpha', 'tool_key': 'demo.message.propose',
                  'status': 'succeeded', 'version': 4, 'created_at': '2026-11-20T10:00:00Z',
                  'expires_at': '2026-11-21T10:00:00Z',
                  'result': {'operation_id': 'op-1', 'status': 'succeeded', 'reconciliation_required': False}}
        v.validate(status)
        for leak in ({'args': {}}, {'preview': PREVIEW}, {'execution_token': 'x'}):
            with self.assertRaises(ValidationError):
                v.validate({**status, **leak})
        with self.assertRaises(ValidationError):
            v.validate({**status, 'status': 'reserved'})

    def test_preview_rejects_html_fields_and_unknown_kinds(self):
        v = validator('preview.schema.json')
        v.validate(PREVIEW)
        with self.assertRaises(ValidationError):
            v.validate({**PREVIEW, 'html': '<b>x</b>'})
        with self.assertRaises(ValidationError):
            v.validate({**PREVIEW, 'fields': [{'label': 'x', 'value': 'y', 'kind': 'html'}]})

    def test_error_outcome_unknown_never_retryable(self):
        v = validator('error.schema.json')
        v.validate({'code': 'rate_limited', 'message': 'slow down', 'retryable': True, 'retry_after_s': 30})
        with self.assertRaises(ValidationError):
            v.validate({'code': 'outcome_unknown', 'message': 'x', 'retryable': True})
        with self.assertRaises(ValidationError):
            v.validate({'code': 'approval_required', 'message': 'x', 'retryable': False})

    def test_execution_token_and_decision(self):
        validator('execution-token.schema.json').validate({
            'iss': 'gxg-executor', 'aud': 'demo', 'sub': 'i-1', 'jti': 'e-1', 'ten': 't-alpha',
            'req': 'agent:t-alpha:actor', 'tool': 'demo.message.propose@1.0.0', 'con': 'demo-main',
            'res': 'demo:demo-main/board/a-public', 'ahs': H, 'idk': 'cand-1:v1', 'iat': 1, 'exp': 61})
        v = validator('approval-decision.schema.json')
        d = {'decision_id': 'd-1', 'intent_id': 'i-1', 'tenant_id': 't-alpha',
             'decided_by': 'policy:tg-partner-auto@abc123', 'decision': 'approve',
             'decided_at': '2026-11-20T10:00:00Z', 'intent_version': 1, 'canonical_args_hash': H}
        v.validate(d)
        with self.assertRaises(ValidationError):
            v.validate({**d, 'decided_by': 'agent:t-alpha:actor'})


def _canon(v):
    def norm(x):
        if isinstance(x, bool) or x is None:
            return x
        if isinstance(x, str):
            return unicodedata.normalize('NFC', x)
        if isinstance(x, int):
            if abs(x) > 2 ** 53 - 1:
                raise ValueError('int out of range')
            return x
        if isinstance(x, float):
            raise ValueError('float')
        if isinstance(x, list):
            return [norm(i) for i in x]
        return {unicodedata.normalize('NFC', k): norm(i) for k, i in x.items()}
    return json.dumps(norm(v), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class CanonicalVectors(unittest.TestCase):
    def test_vectors(self):
        doc = json.loads((SCHEMAS / 'test-vectors' / 'canonical-hash.json').read_text(encoding='utf-8'))
        for vec in doc['vectors']:
            with self.subTest(vec['name']):
                c = _canon(vec['input'])
                self.assertEqual(c, vec['canonical'])
                self.assertEqual(hashlib.sha256(c.encode('utf-8')).hexdigest(), vec['sha256'])
        for bad in doc['invalid_inputs']:
            with self.subTest(bad['name']), self.assertRaises(ValueError):
                _canon(bad['input'])


class OpenApi(unittest.TestCase):
    def test_openapi_document_is_valid(self):
        from openapi_spec_validator import validate
        from openapi_spec_validator.readers import read_from_filename
        spec, base_uri = read_from_filename(str(SCHEMAS / 'approval-service.openapi.yaml'))
        validate(spec, base_uri=base_uri)


if __name__ == '__main__':
    unittest.main()
