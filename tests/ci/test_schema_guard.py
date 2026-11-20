"""AC-M0-02: breaking a JSON Schema makes the contract tests (and so CI) red.

The e2e test runs the mutation proof (scripts/ci/prove_schema_guard.py) for a
representative subset; CI runs the full set in the `schema-guard` job. The
other tests call the script in-process, so mutmut (AC-M0.1-03) can see them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scripts.ci import prove_schema_guard

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "prove_schema_guard.py"
SUBSET = [
    "AC-M0-02 example",
    "additionalProperties false -> true",
    "invalid JSON",
    "schema file deleted",
    "openapi.yaml",
    "drop 'idempotency_key' from required + lock --write",
    "drop 'jti' from required + lock --write",
]


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    return prove_schema_guard


@pytest.mark.e2e
def test_schema_mutations_are_caught() -> None:
    args = [a for s in SUBSET for a in ("--only", s)]
    res = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"{len(SUBSET)}/{len(SUBSET)} schema mutations" in res.stdout


def _schema(root: Path, name: str) -> Any:
    return json.loads((root / "schemas" / name).read_text(encoding="utf-8"))


def _apply(guard: ModuleType, root: Path, name: str) -> None:
    (m,) = [m for m in guard.mutations() if m.name == name]
    guard.prepare(root)
    m.apply(root)


FIXED = {
    "effect-intent: delete top-level required (AC-M0-02 example)",
    "execution-token: additionalProperties false -> true",
    "effect-intent: loosen canonical_args_hash pattern",
    "error: widen code enum",
    "preview: invalid JSON",
    "preview: meta-schema violation (type: objekt)",
    "intent-event: schema file deleted",
    "approval-service.openapi.yaml: drop a response",
}
CRITICAL = {
    "effect-intent.schema.json": ("idempotency_key", "canonical_args_hash", "tenant_id", "expires_at"),
    "execution-token.schema.json": ("jti", "aud", "exp", "ahs"),
}


def test_mutation_names_are_unique_and_complete(guard: ModuleType) -> None:
    names = [m.name for m in guard.mutations()]
    assert len(names) == len(set(names)), "--only selects by name"
    assert FIXED <= set(names)
    for schema, fields in CRITICAL.items():
        for f in fields:
            assert f"{schema}: drop {f!r} from required + lock --write" in names
    for s in SUBSET:
        assert any(s in n for n in names), s


def test_fixed_mutations_have_exactly_their_effect(guard: ModuleType, tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    guard.prepare(clean)

    def run(name: str) -> Path:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        _apply(guard, root, name)
        return root

    r = run("effect-intent: delete top-level required (AC-M0-02 example)")
    want = _schema(clean, "effect-intent.schema.json")
    del want["required"]
    assert _schema(r, "effect-intent.schema.json") == want

    r = run("execution-token: additionalProperties false -> true")
    want = _schema(clean, "execution-token.schema.json")
    assert want["additionalProperties"] is False
    want["additionalProperties"] = True
    assert _schema(r, "execution-token.schema.json") == want

    r = run("effect-intent: loosen canonical_args_hash pattern")
    want = _schema(clean, "effect-intent.schema.json")
    assert want["properties"]["canonical_args_hash"]["pattern"] != ".*"
    want["properties"]["canonical_args_hash"]["pattern"] = ".*"
    assert _schema(r, "effect-intent.schema.json") == want

    r = run("error: widen code enum")
    want = _schema(clean, "error.schema.json")
    want["properties"]["code"]["enum"].append("anything")
    assert _schema(r, "error.schema.json") == want

    r = run("preview: invalid JSON")
    text = (clean / "schemas" / "preview.schema.json").read_text(encoding="utf-8")
    assert (r / "schemas" / "preview.schema.json").read_text(encoding="utf-8") == text[: len(text) // 2]

    r = run("preview: meta-schema violation (type: objekt)")
    want = _schema(clean, "preview.schema.json")
    want["type"] = "objekt"
    assert _schema(r, "preview.schema.json") == want

    r = run("intent-event: schema file deleted")
    assert not (r / "schemas" / "intent-event.schema.json").exists()
    assert (clean / "schemas" / "intent-event.schema.json").exists()

    r = run("approval-service.openapi.yaml: drop a response")
    text = (clean / "schemas" / "approval-service.openapi.yaml").read_text(encoding="utf-8")
    assert text.count("responses:") > 1
    want_text = text.replace("responses:", "responsez:", 1)
    assert (r / "schemas" / "approval-service.openapi.yaml").read_text(encoding="utf-8") == want_text


def test_prepare_copies_the_contract_inputs(guard: ModuleType, tmp_path: Path) -> None:
    guard.prepare(tmp_path)
    for rel in guard.COPY:
        assert (tmp_path / rel).exists(), rel
    assert (tmp_path / "schemas" / "effect-intent.schema.json").read_bytes() == (
        ROOT / "schemas" / "effect-intent.schema.json"
    ).read_bytes()
    assert not list(tmp_path.rglob("__pycache__"))


def test_drop_required_removes_exactly_one_field(guard: ModuleType, tmp_path: Path) -> None:
    guard.prepare(tmp_path / "clean")
    want = _schema(tmp_path / "clean", "execution-token.schema.json")
    want["required"].remove("jti")
    _apply(guard, tmp_path / "m", "execution-token.schema.json: drop 'jti' from required + lock --write")
    assert _schema(tmp_path / "m", "execution-token.schema.json") == want


def test_lock_rewrite_matches_the_mutated_schemas(guard: ModuleType, tmp_path: Path) -> None:
    _apply(guard, tmp_path, "effect-intent.schema.json: drop 'tenant_id' from required + lock --write")
    lock = (tmp_path / "tests" / "contract" / "schema-lock.json").read_text(encoding="utf-8")
    assert lock == guard.schema_lock.render(guard.schema_lock.build_lock(tmp_path / "schemas"))
    assert "tenant_id" not in json.loads(lock)["schemas"]["effect-intent.schema.json"]["required"]["/"]


def test_resolve_follows_json_pointer(guard: ModuleType) -> None:
    doc = {"a/b": {"c~d": [{"x": 1}, {"x": 2}]}}
    assert guard._resolve(doc, "/a~1b/c~0d/1/x") == 2
    assert guard._resolve(doc, "") is doc


def test_mutations_cover_every_required_array(guard: ModuleType) -> None:
    names = [m.name for m in guard.mutations()]
    for path in sorted((ROOT / "schemas").glob("*.schema.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for pointer in guard.schema_lock.required_map(doc):
            assert any(n.startswith(f"{path.name}: drop ") and n.endswith(f" at {pointer}") for n in names), (
                path.name,
                pointer,
            )


def test_run_contract_tests_reports_pytest_result(guard: ModuleType, tmp_path: Path) -> None:
    for body, expected_ok in (("def test_a():\n    assert True\n", True), ("def test_a():\n    assert False\n", False)):
        root = tmp_path / str(expected_ok)
        (root / "tests" / "contract").mkdir(parents=True)
        (root / "tests" / "contract" / "test_x.py").write_text(body, encoding="utf-8")
        assert (guard.run_contract_tests(root) == 0) is expected_ok


class FakeRunner:
    """Stands in for pytest: the clean copy passes, a copy is 'caught' unless its mutation is listed."""

    def __init__(self, baseline_ok: bool = True, survivors: tuple[str, ...] = ()) -> None:
        self.baseline_ok = baseline_ok
        self.survivors = survivors
        self.calls: list[Path] = []
        self.applied: dict[str, str] = {}

    def __call__(self, root: Path) -> int:
        self.calls.append(root)
        if root.name == "baseline":
            return 0 if self.baseline_ok else 1
        return 0 if self.applied.get(root.name) in self.survivors else 1


def _main(guard: ModuleType, monkeypatch: pytest.MonkeyPatch, runner: FakeRunner, *argv: str) -> tuple[int, list[str]]:
    original = list(guard.mutations())

    def recording() -> list[object]:
        out = []
        for i, m in enumerate(original):

            def apply(root: Path, m: object = m, i: int = i) -> None:
                runner.applied[root.name] = m.name  # type: ignore[attr-defined]
                m.apply(root)  # type: ignore[attr-defined]

            out.append(guard.Mutation(m.name, apply))
        return out

    monkeypatch.setattr(guard, "mutations", recording)
    monkeypatch.setattr(guard, "run_contract_tests", runner)
    return guard.main(list(argv)), [m.name for m in original]


def test_main_passes_when_every_mutation_is_caught(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner()
    code, _ = _main(guard, monkeypatch, runner, "--only", "invalid JSON", "--only", "schema file deleted")
    out = capsys.readouterr().out
    assert code == 0
    assert "[caught] preview: invalid JSON" in out
    assert "2/2 schema mutations make the contract tests fail" in out
    assert [p.name for p in runner.calls] == ["baseline", "m0", "m1"]


def test_main_fails_on_a_surviving_mutation(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    both = ("preview: invalid JSON", "intent-event: schema file deleted")
    runner = FakeRunner(survivors=both)
    code, _ = _main(
        guard, monkeypatch, runner, "--only", "invalid JSON", "--only", "schema file deleted", "--only", "objekt"
    )
    cap = capsys.readouterr()
    assert code == 1
    assert "[SURVIVED] preview: invalid JSON" in cap.out
    assert "[caught] preview: meta-schema violation" in cap.out
    assert "1/3 schema mutations" in cap.out
    assert cap.err == "FAIL: mutations not detected:\n  preview: invalid JSON\n  intent-event: schema file deleted\n"


def test_main_fails_when_the_clean_copy_is_red(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _main(guard, monkeypatch, FakeRunner(baseline_ok=False), "--only", "invalid JSON")
    assert code == 1
    assert capsys.readouterr().err == "FAIL: unmutated contract tests do not pass; the proof is meaningless\n"


def test_main_runs_all_mutations_by_default(guard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner()
    code, names = _main(guard, monkeypatch, runner)
    assert code == 0
    assert len(runner.calls) == len(names) + 1


def test_main_list_prints_names_and_runs_nothing(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner()
    code, names = _main(guard, monkeypatch, runner, "--list")
    assert code == 0
    assert capsys.readouterr().out.splitlines() == names
    assert runner.calls == []


def test_unknown_mutation_filter_is_an_error(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner()
    code, _ = _main(guard, monkeypatch, runner, "--only", "no-such-mutation")
    assert code == 2
    assert capsys.readouterr().err == "no mutation matched --only\n"
    assert runner.calls == []
