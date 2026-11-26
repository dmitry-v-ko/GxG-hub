"""grants-widening (owner decision 2026-11-26): which control changes need `widening-approved` (gate_diff.py).

Each case commits `base` files on main and `head` files on a branch of a throw-away repo and checks the
rules of gate_diff.collect(merge-base..head). Bypass cases must give a finding; ordinary changes must not.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"scripts.ci.{name}", ROOT / "scripts" / "ci" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gd = load("gate_diff")


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: Mapping[str, str | bytes | None], msg: str) -> str:
    for name, text in files.items():
        f = repo / name
        if text is None:
            f.unlink()
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            f.write_bytes(text)
        else:
            f.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg, "--allow-empty")
    return git(repo, "rev-parse", "HEAD")


def rules(tmp_path: Path, base: Mapping[str, str | bytes | None], head: Mapping[str, str | bytes | None]) -> list[str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    b = commit(repo, {"README.md": "r\n", **base}, "base")
    git(repo, "checkout", "-qb", "claude/x")
    h = commit(repo, head, "head")
    return sorted(f"{f['rule']} {f['path']} {f['detail']}" for f in gd.collect(repo, b, h))


def heads(found: list[str]) -> list[str]:
    """Rule and path only (details carry content hashes)."""
    return [" ".join(f.split()[:2]) for f in found]


WF = ".github/workflows/ci.yml"
PIN = "3d3c42e5aac5ba805825da76410c181273ba90b1"
BASE_WF = """
name: ci
on: {pull_request: {}, push: {branches: [main]}}
permissions:
  contents: read
jobs:
  tests:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - run: pytest -q
        env:
          TOKEN: ${{ secrets.OLD }}
  ci-ok:
    if: always()
    needs: [tests, lint]
    runs-on: ubuntu-24.04
    steps:
      - run: echo ok
  grants-widening:
    runs-on: ubuntu-24.04
    steps:
      - run: bash scripts/ci/grants_widening.sh
"""


def wf(old: str, new: str) -> str:
    assert old in BASE_WF, old
    return BASE_WF.replace(old, new)


# --- 1. CI grants ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "rule"),
    [
        ("${{ secrets.OLD }}", "${{ secrets.NEW }}", "secrets.new"),
        ("${{ secrets.OLD }}", "${{ secrets['OLD'] }}", "secrets.new"),
        ("${{ secrets.OLD }}", "${{ toJSON(secrets) }}", "secrets.new"),
        ("permissions:\n  contents: read", "permissions:\n  contents: write", "permissions.widened"),
        ("permissions:\n  contents: read", "permissions: write-all", "permissions.widened"),
        ("permissions:\n  contents: read\n", "", "permissions.widened"),  # default token: write
        ("    steps:\n      - uses: actions",
         "    permissions: {contents: read, id-token: write}\n    steps:\n      - uses: actions",
         "permissions.widened"),
        ("on: {pull_request: {}, push: {branches: [main]}}", "on: [pull_request_target]", "trigger.privileged"),
        ("on: {pull_request: {}, push: {branches: [main]}}", "on: workflow_run", "trigger.privileged"),
        (f"actions/checkout@{PIN}", "actions/checkout@v7", "action.new"),
        (f"actions/checkout@{PIN}", f"evil/checkout@{PIN}", "action.new"),
        (f"actions/checkout@{PIN}", "./.github/actions/x", "action.local"),
    ],
)  # fmt: skip
def test_ci_grants_need_the_label(tmp_path: Path, old: str, new: str, rule: str) -> None:
    found = rules(tmp_path, {WF: BASE_WF}, {WF: wf(old, new)})
    assert any(f.startswith(f"{rule} {WF}") for f in found), found


def test_reusable_workflow_with_inherited_secrets(tmp_path: Path) -> None:
    head = BASE_WF + "  call:\n    uses: o/r/.github/workflows/x.yml@0123456789abcdef0123456789abcdef01234567\n"
    head += "    secrets: inherit\n"
    assert heads(rules(tmp_path, {WF: BASE_WF}, {WF: head})) == [f"action.new {WF}", f"secrets.new {WF}"]


def test_new_job_inherits_the_workflow_grant_without_a_finding(tmp_path: Path) -> None:
    head = BASE_WF + "  docs:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: make docs\n"
    assert rules(tmp_path, {WF: BASE_WF}, {WF: head}) == []


def test_new_job_in_a_workflow_without_permissions_gets_the_default_token(tmp_path: Path) -> None:
    base = wf("permissions:\n  contents: read\n", "")
    head = base + "  docs:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: make docs\n"
    assert f"permissions.widened {WF}" in heads(rules(tmp_path, {WF: base}, {WF: head}))


def test_narrowing_permissions_and_dropping_a_secret_need_nothing(tmp_path: Path) -> None:
    head = wf("permissions:\n  contents: read", "permissions: {}").replace("${{ secrets.OLD }}", "x")
    assert rules(tmp_path, {WF: BASE_WF}, {WF: head}) == []


def test_new_workflow_file_is_judged_against_nothing(tmp_path: Path) -> None:
    found = heads(rules(tmp_path, {}, {".github/workflows/new.yml": BASE_WF}))
    assert "secrets.new .github/workflows/new.yml" in found
    assert "permissions.widened .github/workflows/new.yml" in found  # contents: read is new
    assert "action.new .github/workflows/new.yml" in found


# --- 2. switched-off controls ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "rule"),
    [
        ("needs: [tests, lint]", "needs: [tests]", "ci-ok.changed"),
        ("    if: always()\n", "", "ci-ok.changed"),
        ("      - run: pytest -q", "      - run: pytest -q\n        continue-on-error: true", "control.disabled"),
        ("      - run: pytest -q", "      - run: pytest -q\n        if: false", "control.disabled"),
        ("      - run: pytest -q", "      - run: pytest -q\n        if: ${{ false }}", "control.disabled"),
        ("      - run: pytest -q", "      - run: pytest -q || true", "control.disabled"),
        ("      - run: pytest -q", "      - run: 'pytest -q || :'", "control.disabled"),
        ("      - run: pytest -q", "      - run: pytest -q --deselect tests/test_a.py::test_x", "tests.skipped"),
        ("      - run: pytest -q", "      - run: pytest -q -k 'not slow'", "tests.skipped"),
        ("      - run: pytest -q", "      - run: pytest -q --ignore=tests/ci", "tests.skipped"),
        ("      - run: bash scripts/ci/grants_widening.sh", "      - run: 'true'", "gate.self"),
    ],
)
def test_switching_a_control_off_needs_the_label(tmp_path: Path, old: str, new: str, rule: str) -> None:
    found = rules(tmp_path, {WF: BASE_WF}, {WF: wf(old, new)})
    assert any(f.startswith(f"{rule} {WF}") for f in found), found


@pytest.mark.parametrize(
    "line",
    [
        "@pytest.mark.skip(reason='later')",
        "@pytest.mark.skipif(True, reason='x')",
        "@pytest.mark.xfail",
        "    pytest.skip('no db')",
        "pg = pytest.importorskip('psycopg')",
        "@unittest.skip('x')",
        "collect_ignore = ['test_x.py']",
        "def pytest_collection_modifyitems(items): items.clear()",
    ],
)
def test_skips_in_tests_need_the_label(tmp_path: Path, line: str) -> None:
    found = rules(tmp_path, {"tests/test_a.py": "def test_a():\n    assert True\n"},
                  {"tests/test_a.py": f"import pytest\n{line}\ndef test_a():\n    assert True\n"})  # fmt: skip
    assert heads(found) == ["tests.skipped tests/test_a.py"], found


@pytest.mark.parametrize(
    "line",
    ["x = 1  # noqa: E501", "x = f()  # type: ignore[no-untyped-call]", "# ruff: noqa", "# mypy: ignore-errors",
     "x = 1  # pragma: no cover", "KEY = 'abc'  # gitleaks:allow", "x = 1  # pyright: ignore"],
)  # fmt: skip
def test_inline_lint_exceptions_need_the_label(tmp_path: Path, line: str) -> None:
    found = rules(tmp_path, {"app/a.py": "x = 0\n"}, {"app/a.py": f"x = 0\n{line}\n"})
    assert heads(found) == ["lint.exception app/a.py"], found


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        ("pyproject.toml", '[project]\nname = "x"\n[tool.ruff]\nline-length = 120\n',
         '[project]\nname = "x"\n[tool.ruff]\nline-length = 120\nextend-exclude = ["app"]\n'),
        ("pyproject.toml", '[project]\nname = "x"\n', '[project]\nname = "x"\n[tool.mypy]\nignore_errors = true\n'),
        ("pyproject.toml", '[project]\nname = "x"\n[tool.pytest.ini_options]\naddopts = "-q"\n',
         '[project]\nname = "x"\n[tool.pytest.ini_options]\naddopts = "-q -p no:randomly"\n'),
        (".gitleaks.toml", "[extend]\nuseDefault = true\n",
         '[extend]\nuseDefault = true\n[allowlist]\npaths = ["app/"]\n'),
        ("mypy.ini", "[mypy]\nstrict = True\n", "[mypy]\nstrict = False\n"),
    ],
)  # fmt: skip
def test_lint_and_scanner_config_changes_need_the_label(tmp_path: Path, path: str, base: str, head: str) -> None:
    assert "lint.config " + path in heads(rules(tmp_path, {path: base}, {path: head}))


# --- 3. exception lists ---------------------------------------------------------------------------------------

EQ = "scripts/ci/mutmut-equivalent.txt"


@pytest.mark.parametrize(
    ("path", "base", "head", "expected"),
    [
        (EQ, "# list\na.x__mutmut_1  # why\n", "# list\na.x__mutmut_1  # why\nb.y__mutmut_2  # why\n",
         ["exceptions.added " + EQ]),
        (EQ, "# list\na.x__mutmut_1  # why\nb.y__mutmut_2\n", "# list\na.x__mutmut_1  # why\n", []),  # removal
        (EQ, "# list\n", "# list\n# a comment only\n\n", []),
        (".gitleaksignore", "abc:app/a.py:generic-api-key:3\n",
         "abc:app/a.py:generic-api-key:3\ndef:app/b.py:generic-api-key:9\n", ["exceptions.added .gitleaksignore"]),
        (".semgrepignore", "", "app/\n", ["exceptions.added .semgrepignore"]),
        (".gitignore", "*.pyc\n", "*.pyc\n.env\n", []),
        ("deploy/x.Dockerfile.dockerignore", "*\n", "*\n!app\n", []),
    ],
)  # fmt: skip
def test_exception_lists(tmp_path: Path, path: str, base: str, head: str, expected: list[str]) -> None:
    assert heads(rules(tmp_path, {path: base}, {path: head})) == expected


# --- 4. policy tooling and schemas ----------------------------------------------------------------------------


def test_policy_tool_code_needs_the_label_its_plain_tests_do_not(tmp_path: Path) -> None:
    src, test = "tools/gxg-policy/src/gxg_policy/diff.py", "tools/gxg-policy/tests/test_diff.py"
    found = rules(tmp_path, {src: "A = 1\n", test: "def test_a():\n    pass\n"},
                  {src: "A = 2\n", test: "def test_a():\n    assert 1\n"})  # fmt: skip
    assert heads(found) == ["policy.tool " + src]


SCHEMA = {
    "type": "object",
    "required": ["id", "kind"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "maxLength": 64, "pattern": "^[a-z]+$", "description": "x"},
        "kind": {"enum": ["a", "b"]},
        "n": {"type": "integer", "minimum": 1},
    },
}


def schema(**changes: object) -> str:
    doc = json.loads(json.dumps(SCHEMA))
    for path, value in changes.items():
        node = doc
        *parents, last = path.split("__")
        for p in parents:
            node = node[p]
        if value is None:
            del node[last]
        else:
            node[last] = value
    return json.dumps(doc, indent=1)


@pytest.mark.parametrize(
    "head",
    [
        schema(required=["id"]),
        schema(additionalProperties=True),
        schema(additionalProperties=None),
        schema(properties__id__maxLength=128),
        schema(properties__id__pattern=None),
        schema(properties__id__pattern="^.*$"),
        schema(properties__kind__enum=["a", "b", "c"]),
        schema(properties__n__minimum=0),
        schema(properties__n__type=["integer", "null"]),
        schema(properties__extra={"type": "string"}),  # a new field of a closed object
        schema(properties__id={"$ref": "#/$defs/id"}),
        "not json",
    ],
)
def test_schema_weakening_needs_the_label(tmp_path: Path, head: str) -> None:
    found = heads(rules(tmp_path, {"schemas/x.schema.json": schema()}, {"schemas/x.schema.json": head}))
    assert found and set(found) <= {"schema.weakened schemas/x.schema.json", "unparsed schemas/x.schema.json"}, found


@pytest.mark.parametrize(
    "head",
    [
        schema(properties__id__description="new words", title="T"),
        schema(required=["id", "kind", "n"]),
        schema(properties__id__maxLength=32),
        schema(properties__n__minimum=5),
        schema(properties__kind__enum=["a"]),
        schema(properties__n__maximum=10),
        schema(properties__id__minLength=1),
    ],
)
def test_schema_tightening_needs_nothing(tmp_path: Path, head: str) -> None:
    assert rules(tmp_path, {"schemas/x.schema.json": schema()}, {"schemas/x.schema.json": head}) == []


def test_new_schema_needs_nothing_removed_schema_does(tmp_path: Path) -> None:
    assert rules(tmp_path, {}, {"schemas/new.schema.json": schema()}) == []
    found = heads(rules(tmp_path / "2", {"schemas/x.schema.json": schema()}, {"schemas/x.schema.json": None}))
    assert found == ["schema.weakened schemas/x.schema.json"]


def test_open_object_may_get_a_new_field(tmp_path: Path) -> None:
    base = json.dumps({"type": "object", "properties": {}})
    head = json.dumps({"type": "object", "properties": {"x": {"type": "string"}}})
    assert rules(tmp_path, {"schemas/o.json": base}, {"schemas/o.json": head}) == []


# --- 5. the gate itself ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["scripts/ci/grants_widening.sh", "scripts/ci/widening_gate.py", "scripts/ci/gate_diff.py", ".github/CODEOWNERS",
     "tests/ci/test_gate_diff.py", "tests/ci/test_widening_gate.py", "tests/ci/test_grants_widening_job.py"],
)  # fmt: skip
def test_gate_files_need_the_label(tmp_path: Path, path: str) -> None:
    assert heads(rules(tmp_path, {path: "a\n"}, {path: "b\n"})) == ["gate.self " + path]


# --- 6. packages ----------------------------------------------------------------------------------------------

PY = '[project]\nname = "x"\ndependencies = ["httpx>=0.27,<1", "PyYAML>=6"]\n[dependency-groups]\ndev = ["pytest>=8"]\n'


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (PY.replace('"PyYAML>=6"', '"PyYAML>=6", "requests>=2"'), ["deps.new pyproject.toml requests"]),
        (PY.replace('"pytest>=8"', '"pytest>=8", "hypothesis"'), ["deps.new pyproject.toml hypothesis"]),
        (PY.replace('"httpx>=0.27,<1"', '"httpx>=0.28,<1"'), []),  # version bump
        (PY.replace('"PyYAML>=6"', '"pyyaml>=6.0.2"'), []),  # same name, normalised
        (PY.replace('"httpx>=0.27,<1"', '"httpx @ git+https://example.invalid/httpx"'),
         ["deps.source pyproject.toml httpx @ git+https://example.invalid/httpx"]),
        (PY + '[tool.uv.sources]\nhttpx = { git = "https://example.invalid/httpx" }\n',
         ["deps.source pyproject.toml tool.uv.sources"]),
        (PY + '[[tool.uv.index]]\nurl = "https://example.invalid/simple"\n',
         ["deps.source pyproject.toml tool.uv.index"]),
        (PY + "[build-system]\nrequires = ['hatchling', 'evil-backend']\n",
         ["deps.new pyproject.toml evil-backend", "deps.new pyproject.toml hatchling"]),
    ],
)  # fmt: skip
def test_pyproject_packages(tmp_path: Path, head: str, expected: list[str]) -> None:
    found = rules(tmp_path, {"pyproject.toml": PY}, {"pyproject.toml": head})
    assert [f.split(" (")[0] for f in found] == expected


def lock(*pkgs: tuple[str, str, str]) -> str:
    return "version = 1\n" + "".join(
        f'\n[[package]]\nname = "{n}"\nversion = "{v}"\nsource = {{ {s} }}\n' for n, v, s in pkgs
    )


PYPI = 'registry = "https://pypi.org/simple"'


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (lock(("httpx", "0.28.1", PYPI)), []),  # version bump
        (lock(("httpx", "0.27.0", PYPI), ("evil", "1.0", PYPI)), ["deps.new uv.lock"]),
        (lock(("httpx", "0.27.0", 'registry = "https://example.invalid/simple"')), ["deps.source uv.lock"]),
        (lock(("httpx", "0.27.0", 'git = "https://example.invalid/httpx?rev=1"')), ["deps.source uv.lock"]),
    ],
)
def test_uv_lock_packages(tmp_path: Path, head: str, expected: list[str]) -> None:
    found = rules(tmp_path, {"uv.lock": lock(("httpx", "0.27.0", PYPI))}, {"uv.lock": head})
    assert heads(found) == expected


# --- fail closed and ordinary changes -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "text"),
    [(WF, "jobs: [unclosed\n"), ("pyproject.toml", "[project\n"), ("uv.lock", "= x\n"), (WF, "- a list\n")],
)
def test_unparsable_control_files_need_the_label(tmp_path: Path, path: str, text: str) -> None:
    base = {WF: BASE_WF, "pyproject.toml": PY, "uv.lock": lock(("httpx", "0.27.0", PYPI))}
    assert heads(rules(tmp_path, base, {path: text})) == ["unparsed " + path]


def test_binary_in_a_control_path_needs_the_label_an_image_does_not(tmp_path: Path) -> None:
    found = rules(tmp_path, {}, {"scripts/ci/tool.bin": b"\0\1\2", "docs/img.png": b"\x89PNG\0\0"})
    assert heads(found) == ["unparsed scripts/ci/tool.bin"]


def test_removed_workflow_needs_the_label(tmp_path: Path) -> None:
    assert heads(rules(tmp_path, {WF: BASE_WF}, {WF: None})) == ["workflow.removed " + WF]


def test_ordinary_changes_need_nothing(tmp_path: Path) -> None:
    head = {
        "app/a.py": "def f() -> int:\n    return 2\n",
        "tests/test_a.py": "def test_f():\n    assert True\n",
        "docs/x.md": "Use `@pytest.mark.skip` with care; `|| true` hides failures.\n",
        "scripts/run.sh": "set -eu\nmake test\n",
        WF: wf("      - run: pytest -q", "      - run: pytest -q --maxfail=1\n        timeout-minutes: 30"),
    }
    assert rules(tmp_path, {WF: BASE_WF, "app/a.py": "def f() -> int:\n    return 1\n"}, head) == []


def test_findings_differ_when_the_content_of_a_coarse_finding_changes(tmp_path: Path) -> None:
    src = "tools/gxg-policy/src/gxg_policy/diff.py"
    one = rules(tmp_path, {src: "A = 1\n"}, {src: "A = 2\n"})
    two = rules(tmp_path / "2", {src: "A = 1\n"}, {src: "A = 3\n"})
    assert heads(one) == heads(two) and one != two


def test_cli_writes_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    b = commit(repo, {"a.py": "x = 1\n"}, "b")
    h = commit(repo, {"a.py": "x = 1  # noqa\n"}, "h")
    out = tmp_path / "f.json"
    cmd = [sys.executable, str(ROOT / "scripts/ci/gate_diff.py"), "--repo", str(repo), "--base", b, "--head", h]
    subprocess.run([*cmd, "--json", str(out)], check=True)
    assert [f["rule"] for f in json.loads(out.read_text())] == ["lint.exception"]
