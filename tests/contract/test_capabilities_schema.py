"""AC-M11.2-01: gxg-capabilities.schema.json (MCC-11 п.2, п.3, п.6)."""

import copy
import importlib.util
import unittest
from pathlib import Path

from jsonschema import ValidationError

_spec = importlib.util.spec_from_file_location('contract_test_specs', Path(__file__).with_name('test_specs.py'))
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
load_yaml, validator = _mod.load_yaml, _mod.validator

EXAMPLE = 'schemas/examples/telegram.gxg-capabilities.yaml'


def _with(**patch):
    doc = load_yaml(EXAMPLE)
    entry = doc['capabilities'][1]
    for k, v in patch.items():
        if v is None:
            entry.pop(k, None)
        else:
            entry[k] = v
    return doc


class CapabilitiesSchema(unittest.TestCase):
    def test_example_passes(self):
        validator('gxg-capabilities.schema.json').validate(load_yaml(EXAMPLE))

    def test_timeouts_below_tool_timeout_pass(self):
        validator('gxg-capabilities.schema.json').validate(_with(limits={'timeout_s': 24.5}))

    def test_negative_examples_rejected(self):
        dup = load_yaml(EXAMPLE)
        dup['capabilities'].append(copy.deepcopy(dup['capabilities'][1]))
        cases = {
            'no effect_class': _with(effect_class=None),
            'no input_schema': _with(input_schema=None),
            'no action': _with(action=None),
            'no resource type': _with(resource={}),
            'non-read enabled_default': _with(
                effect_class='external_effect',
                action='propose',
                propose_tool='telegram.message.propose',
                enabled_default=True,
            ),
            'non-read without propose_tool': _with(effect_class='external_effect', action='propose'),
            'read with action propose': _with(action='propose'),
            'duplicate': dup,
            # owner decision 2026-11-26: tool calls < 25 s (ContextForge's single TOOL_TIMEOUT)
            'timeout 25 s': _with(limits={'max_results': 50, 'timeout_s': 25}),
            'timeout 120 s': _with(limits={'timeout_s': 120}),
            'timeout 0': _with(limits={'timeout_s': 0}),
        }
        for name in (
            'invoke',
            'chats.invoke',
            'auth.2fa.reset',
            'sessions.terminate',
            'account.delete_account',
            'raw.call',
        ):
            cases[f'forbidden name {name}'] = _with(name=name)
        v = validator('gxg-capabilities.schema.json')
        for label, doc in cases.items():
            with self.subTest(label), self.assertRaises(ValidationError):
                v.validate(doc)
