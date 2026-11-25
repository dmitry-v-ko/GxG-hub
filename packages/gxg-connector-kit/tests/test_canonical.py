"""AC-M1-01, AC-M1-02: canonical JSON and hash (EFF-05)."""

from __future__ import annotations

import hashlib
import json
import random
import unicodedata
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from kit_testing import REPO

from gxg_connector_kit import canonical
from gxg_connector_kit.errors import GxgError

VECTORS = json.loads((REPO / "schemas/test-vectors/canonical-hash.json").read_text(encoding="utf-8"))
PROPERTY = settings(max_examples=1000, derandomize=True, deadline=None)


def test_vector_file_has_the_expected_cases() -> None:
    assert len(VECTORS["vectors"]) == 9
    assert len(VECTORS["invalid_inputs"]) == 3


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["name"])
def test_vectors_bytes_and_sha256(vector: dict[str, Any]) -> None:
    out = canonical.dumps(vector["input"])
    assert out == vector["canonical"].encode("utf-8")
    assert hashlib.sha256(out).hexdigest() == vector["sha256"]
    assert canonical.hash(vector["input"]) == vector["sha256"]


@pytest.mark.parametrize("case", VECTORS["invalid_inputs"], ids=lambda v: v["name"])
def test_invalid_inputs(case: dict[str, Any]) -> None:
    with pytest.raises(GxgError) as exc:
        # `input_json` is raw text: `1.0` must stay a float after parsing, not collapse to 1
        canonical.hash(json.loads(case["input_json"]) if "input_json" in case else case["input"])
    assert exc.value.code == case["expected_error"] == "invalid_input"


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1.0},
        {"a": float("nan")},
        {"n": -(2**53)},
        {"n": 2**53},
        {"s": "\ud800"},  # lone surrogate
        {1: "non-string key"},
        {"é": 1, "e\u0301": 2},  # same key after NFC
        {"x": b"bytes"},
        {"x": {1, 2}},
    ],
    ids=["float", "nan", "int_below", "int_above", "surrogate", "int_key", "nfc_key_collision", "bytes", "set"],
)
def test_rejected_values(value: Any) -> None:
    with pytest.raises(GxgError) as exc:
        canonical.dumps(value)
    assert exc.value.code == "invalid_input"


def test_safe_integer_bounds_are_accepted() -> None:
    assert canonical.dumps([2**53 - 1, -(2**53 - 1)]) == b"[9007199254740991,-9007199254740991]"


def test_keys_sorted_by_utf16_code_units() -> None:
    # U+1F600 is D83D DE00 in UTF-16, which sorts before U+E000; code-point order would put it last.
    assert canonical.dumps({"\ue000": 1, "\U0001f600": 2, "a": 3}) == '{"a":3,"\U0001f600":2,"\ue000":1}'.encode()


def test_scalars_tuples_and_control_characters() -> None:
    assert canonical.dumps((None, True, False, "\x01\x7f\u2028")) == b'[null,true,false,"\\u0001\x7f\xe2\x80\xa8"]'


# --- AC-M1-02: properties on random JSON without floats ------------------------------------

_nfc_text = st.text(max_size=12).map(lambda s: unicodedata.normalize("NFC", s))
_json = st.recursive(
    st.none() | st.booleans() | st.integers(-canonical.MAX_SAFE_INT, canonical.MAX_SAFE_INT) | _nfc_text,
    lambda inner: st.lists(inner, max_size=5) | st.dictionaries(_nfc_text, inner, max_size=5),
    max_leaves=25,
)


def _shuffle_keys(value: Any, rnd: random.Random) -> Any:
    if isinstance(value, dict):
        items = list(value.items())
        rnd.shuffle(items)
        return {k: _shuffle_keys(v, rnd) for k, v in items}
    if isinstance(value, list):
        return [_shuffle_keys(v, rnd) for v in value]
    return value


def _nfd(value: Any) -> Any:
    if isinstance(value, dict):
        return {unicodedata.normalize("NFD", k): _nfd(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfd(v) for v in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFD", value)
    return value


def _leaves(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Paths to scalars and to empty containers (a value can be any of them)."""
    if not value and isinstance(value, dict | list):
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _leaves(v, (*path, k))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _leaves(v, (*path, i))]
    return [path]


def _changed(leaf: Any) -> Any:
    if isinstance(leaf, dict):
        return {"k": None}
    if isinstance(leaf, list):
        return [None]
    if leaf is None:
        return 0
    if isinstance(leaf, bool):
        return not leaf
    if isinstance(leaf, int):
        return leaf - 1 if leaf == canonical.MAX_SAFE_INT else leaf + 1
    return leaf + "x"


def _replace(value: Any, path: tuple[Any, ...], new: Any) -> Any:
    if not path:
        return new
    head, rest = path[0], path[1:]
    if isinstance(value, dict):
        return {k: (_replace(v, rest, new) if k == head else v) for k, v in value.items()}
    return [(_replace(v, rest, new) if i == head else v) for i, v in enumerate(value)]


def _get(value: Any, path: tuple[Any, ...]) -> Any:
    for p in path:
        value = value[p]
    return value


@PROPERTY
@given(value=_json, rnd=st.randoms(use_true_random=False))
def test_property_key_order_does_not_change_hash(value: Any, rnd: random.Random) -> None:
    assert canonical.hash(_shuffle_keys(value, rnd)) == canonical.hash(value)


@PROPERTY
@given(value=_json)
def test_property_nfd_and_nfc_hash_the_same(value: Any) -> None:
    assert canonical.hash(_nfd(value)) == canonical.hash(value)


@PROPERTY
@given(value=_json, data=st.data())
def test_property_changing_any_value_changes_hash(value: Any, data: st.DataObject) -> None:
    path = data.draw(st.sampled_from(_leaves(value)))
    changed = _replace(value, path, _changed(_get(value, path)))
    assert canonical.hash(changed) != canonical.hash(value)
