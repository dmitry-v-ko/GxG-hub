"""gxg-policy tests: no state shared between tests through the schema validator cache."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from gxg_policy import load


@pytest.fixture(autouse=True)
def _fresh_validators() -> Iterator[None]:
    # Every test builds its validators itself, so each test that validates also exercises `validator()`.
    load._validators.clear()
    yield
