#!/usr/bin/env python3
"""Mutation proof for AC-M0-02: breaking any JSON Schema turns the tests red.

Copies the contract test inputs (``schemas/``, ``policies/``,
``tests/contract/``, ``scripts/ci/schema_lock.py``) into a temporary directory,
applies one mutation at a time and runs ``pytest tests/contract`` there.

* The unmutated copy must pass (otherwise the proof proves nothing).
* Every mutation must make pytest fail. A surviving mutation is reported and
  the script exits with 1.

Mutations: the AC example (``required`` deleted from ``effect-intent``), one
field dropped from every ``required`` array of every schema,
``additionalProperties: false`` relaxed, a pattern loosened, an enum widened,
invalid JSON, a meta-schema violation, a deleted schema, a broken OpenAPI file,
and a security-critical field dropped from ``required`` together with a
regenerated lock (``schema_lock.py --write``): the lock-independent tests in
``tests/contract/test_security_required_fields.py`` must still fail.

    uv run python scripts/ci/prove_schema_guard.py [--only NAME ...] [--list]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci import schema_lock  # noqa: E402

COPY = ("schemas", "policies", "tests/contract", "scripts/ci/schema_lock.py")

Json = Any
Mutate = Callable[[Path], None]


@dataclass(frozen=True)
class Mutation:
    name: str
    apply: Mutate


def _edit_json(rel: str, change: Callable[[Json], None]) -> Mutate:
    def apply(root: Path) -> None:
        path = root / "schemas" / rel
        doc = json.loads(schema_lock.read_text(path))
        change(doc)
        schema_lock.write_text(path, json.dumps(doc))

    return apply


def _resolve(doc: Json, pointer: str) -> Json:
    node = doc
    for raw in [p for p in pointer.split("/") if p]:
        token = raw.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def _drop_required(pointer: str, field: str) -> Callable[[Json], None]:
    def change(doc: Json) -> None:
        _resolve(doc, pointer)["required"].remove(field)

    return change


def _raw(rel: str, change: Callable[[str], str]) -> Mutate:
    def apply(root: Path) -> None:
        path = root / "schemas" / rel
        schema_lock.write_text(path, change(schema_lock.read_text(path)))

    return apply


def _delete(rel: str) -> Mutate:
    def apply(root: Path) -> None:
        (root / "schemas" / rel).unlink()

    return apply


def _set(pointer: str, key: str, value: Json) -> Callable[[Json], None]:
    def change(doc: Json) -> None:
        _resolve(doc, pointer)[key] = value

    return change


def _with_lock_rewrite(mutate: Mutate) -> Mutate:
    def apply(root: Path) -> None:
        mutate(root)
        lock = root / "tests" / "contract" / "schema-lock.json"
        schema_lock.write_text(lock, schema_lock.render(schema_lock.build_lock(root / "schemas")))

    return apply


def mutations() -> Iterator[Mutation]:
    yield Mutation(
        "effect-intent: delete top-level required (AC-M0-02 example)",
        _edit_json("effect-intent.schema.json", lambda d: d.pop("required")),
    )
    for path in sorted((ROOT / "schemas").glob("*.schema.json")):
        doc = json.loads(schema_lock.read_text(path))
        for pointer, fields in sorted(schema_lock.required_map(doc).items()):
            # one field per required array keeps the run short and still covers every array
            field = fields[0]
            yield Mutation(
                f"{path.name}: drop {field!r} from required at {pointer}",
                _edit_json(path.name, _drop_required(pointer, field)),
            )
    # The critic's scenario: drop a security-critical field AND regenerate the lock,
    # as an agent reacting to a red CI might. The semantic tests must still fail.
    critical = {
        "effect-intent.schema.json": ("idempotency_key", "canonical_args_hash", "tenant_id", "expires_at"),
        "execution-token.schema.json": ("jti", "aud", "exp", "ahs"),
    }
    for name, crit_fields in critical.items():
        for crit in crit_fields:
            yield Mutation(
                f"{name}: drop {crit!r} from required + lock --write",
                _with_lock_rewrite(_edit_json(name, _drop_required("", crit))),
            )
    yield Mutation(
        "execution-token: additionalProperties false -> true",
        _edit_json("execution-token.schema.json", _set("", "additionalProperties", True)),
    )
    yield Mutation(
        "effect-intent: loosen canonical_args_hash pattern",
        _edit_json("effect-intent.schema.json", _set("/properties/canonical_args_hash", "pattern", ".*")),
    )
    yield Mutation(
        "error: widen code enum",
        _edit_json("error.schema.json", lambda d: d["properties"]["code"]["enum"].append("anything")),
    )
    yield Mutation("preview: invalid JSON", _raw("preview.schema.json", lambda s: s[: len(s) // 2]))
    yield Mutation(
        "preview: meta-schema violation (type: objekt)", _edit_json("preview.schema.json", _set("", "type", "objekt"))
    )
    yield Mutation("intent-event: schema file deleted", _delete("intent-event.schema.json"))
    yield Mutation(
        "approval-service.openapi.yaml: drop a response",
        _raw("approval-service.openapi.yaml", lambda s: s.replace("responses:", "responsez:", 1)),
    )


def run_contract_tests(root: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/contract"],
        cwd=root,
        capture_output=True,  # pragma: no mutate (only keeps the log short)
        check=False,  # pragma: no mutate (the default)
    )
    return proc.returncode


def prepare(dst: Path) -> None:
    for rel in COPY:
        src = ROOT / rel
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,  # pragma: no mutate (help text)
        formatter_class=argparse.RawDescriptionHelpFormatter,  # pragma: no mutate (help text)
    )
    parser.add_argument("--only", action="append")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    selected = [m for m in mutations() if not args.only or any(o in m.name for o in args.only)]
    if args.list:
        print("\n".join(m.name for m in selected))
        return 0
    if not selected:
        print("no mutation matched --only", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        baseline = Path(tmp) / "baseline"
        prepare(baseline)
        if run_contract_tests(baseline) != 0:
            print("FAIL: unmutated contract tests do not pass; the proof is meaningless", file=sys.stderr)
            return 1
        survived: list[str] = []
        for i, m in enumerate(selected):
            work = Path(tmp) / f"m{i}"
            prepare(work)
            m.apply(work)
            code = run_contract_tests(work)
            verdict = "caught" if code != 0 else "SURVIVED"
            print(f"[{verdict}] {m.name}")
            if code == 0:
                survived.append(m.name)
    print(f"{len(selected) - len(survived)}/{len(selected)} schema mutations make the contract tests fail")
    if survived:
        print("FAIL: mutations not detected:\n  " + "\n  ".join(survived), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
