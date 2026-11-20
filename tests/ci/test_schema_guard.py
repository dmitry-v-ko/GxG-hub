"""AC-M0-02: breaking a JSON Schema makes the contract tests (and so CI) red.

Runs the mutation proof (scripts/ci/prove_schema_guard.py) for a
representative subset; CI runs the full set in the `schema-guard` job.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "prove_schema_guard.py"
SUBSET = [
    "AC-M0-02 example",
    "additionalProperties false -> true",
    "invalid JSON",
    "schema file deleted",
    "openapi.yaml",
]


def test_schema_mutations_are_caught() -> None:
    args = [a for s in SUBSET for a in ("--only", s)]
    res = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"{len(SUBSET)}/{len(SUBSET)} schema mutations" in res.stdout


def test_unknown_mutation_filter_is_an_error() -> None:
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--only", "no-such-mutation"], capture_output=True, text=True, check=False
    )
    assert res.returncode == 2
