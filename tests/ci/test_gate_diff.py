"""grants-widening (owner decision 2026-11-26): which control changes need `widening-approved` (gate_diff.py).

Each case commits `base` files on main and `head` files on a branch of a throw-away repo and checks the
rules of gate_diff.collect(merge-base..head). Bypass cases must give a finding; ordinary changes must not.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

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
    git(repo, "checkout", "-q", "main")  # the findings come from the two commits, never from the work tree
    return sorted(f"{f['rule']} {f['path']} {f['detail']}" for f in gd.collect(repo, b, h))


def oid(text: str | bytes) -> str:
    """The blob id an `unparsed` finding names (12 hex digits)."""
    data = text.encode() if isinstance(text, str) else text
    out = subprocess.run(["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True).stdout
    return out.decode().strip()[:12]


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


# --- exact findings (DoD-4: mutmut kills every rule branch, message and detail) -------------------------------

SCOPES = ["actions", "attestations", "checks", "contents", "deployments", "discussions", "id-token", "issues",
          "models", "packages", "pages", "pull-requests", "repository-projects", "security-events",
          "statuses"]  # fmt: skip
WHO = ["job ci-ok", "job grants-widening", "job tests", "workflow"]
READ, RUNNER = "permissions:\n  contents: read", "ubuntu-24.04"
ON = "on: {pull_request: {}, push: {branches: [main]}}"
STEPS = "    steps:\n      - uses: actions"


def widened(path: str, who: list[str], scopes: list[str], change: str) -> list[str]:
    return sorted(f"permissions.widened {path} {w}: {s} {change}" for w in who for s in scopes)


@pytest.mark.parametrize(
    ("base", "head", "expected"),
    [
        (BASE_WF, wf("${{ secrets.OLD }}", "${{ secrets.NEW }}"), [f"secrets.new {WF} NEW"]),
        (BASE_WF, wf("${{ secrets.OLD }}", "${{ secrets['OLD'] }}"), [f"secrets.new {WF} secrets["]),
        (BASE_WF, wf("${{ secrets.OLD }}", "${{ toJSON(secrets) }}"), [f"secrets.new {WF} toJSON(secrets)"]),
        (BASE_WF, wf("permissions:\n  contents: read", "permissions:\n  contents: write"),
         widened(WF, WHO, ["contents"], "1->2")),
        # read-all: every scope the mapping left at none is now read
        (BASE_WF, wf("permissions:\n  contents: read", "permissions: read-all"),
         widened(WF, WHO, [s for s in SCOPES if s != "contents"], "0->1")),
        (BASE_WF, wf("permissions:\n  contents: read", "permissions: write-all"),
         sorted(widened(WF, WHO, [s for s in SCOPES if s != "contents"], "0->2")
                + widened(WF, WHO, ["contents"], "1->2"))),
        # write-all is the repository default token: dropping it or adding it to a workflow without one is nothing
        (wf(READ, "permissions: write-all"), wf(READ + "\n", ""), []),
        (wf(READ + "\n", ""), wf(READ, "permissions: write-all"), []),
        (BASE_WF, wf(STEPS, "    permissions: {contents: read, id-token: write}\n" + STEPS),
         [f"permissions.widened {WF} job tests: id-token 0->2"]),
        # a scope this file names is judged too, not only the known ones
        (BASE_WF, wf(STEPS, "    permissions: {foo: write}\n" + STEPS),
         [f"permissions.widened {WF} job tests: foo 0->2"]),
        # a scope only the old file named: the job now gets the default token, foo included
        (wf(STEPS, "    permissions: {foo: read}\n" + STEPS).replace("permissions:\n  contents: read\n", ""),
         wf("permissions:\n  contents: read\n", ""),
         sorted(widened(WF, ["job tests"], ["foo"], "1->2") + widened(WF, ["job tests"], SCOPES, "0->2"))),
        # a job keeps its own block: the workflow block does not decide it
        (wf(STEPS, "    permissions: {contents: write, id-token: write}\n" + STEPS),
         wf(STEPS, "    permissions: {contents: write, id-token: write}\n" + STEPS).replace("pytest -q", "pytest"), []),
        (BASE_WF, wf(ON, "on: {pull_request_target: {}}"), [f"trigger.privileged {WF} pull_request_target"]),
        (BASE_WF, wf(ON, '"on": [pull_request, workflow_run]'), [f"trigger.privileged {WF} workflow_run"]),
        (BASE_WF, wf(ON, "on: [push, workflow_run, pull_request_target]"),
         [f"trigger.privileged {WF} pull_request_target", f"trigger.privileged {WF} workflow_run"]),
        (BASE_WF, wf(f"actions/checkout@{PIN}", "actions/checkout@v7"), [f"action.new {WF} actions/checkout@v7"]),
        (BASE_WF, wf(f"actions/checkout@{PIN}", "./.github/actions/x"), [f"action.local {WF} ./.github/actions/x"]),
        (BASE_WF, wf("needs: [tests, lint]", "needs: [tests]"),
         [f"ci-ok.changed {WF} job ci-ok (or its needs) changed "
          f"({gd.digest({'if': 'always()', 'needs': ['tests'], 'runs-on': RUNNER, 'steps': [{'run': 'echo ok'}]})})"]),
        (BASE_WF, wf("bash scripts/ci/grants_widening.sh", "'true'"),
         [f"gate.self {WF} job grants-widening changed "
          f"({gd.digest({'runs-on': RUNNER, 'steps': [{'run': 'true'}]})})"]),
        # a job that is not a mapping is skipped, the jobs after it are still read
        (BASE_WF, BASE_WF.replace("jobs:\n", "jobs:\n  odd: null\n") + "  call:\n    uses: o/r/x.yml@v1\n",
         [f"action.new {WF} o/r/x.yml@v1"]),
    ],
)  # fmt: skip
def test_workflow_findings_exact(tmp_path: Path, base: str, head: str, expected: list[str]) -> None:
    assert rules(tmp_path, {WF: base}, {WF: head}) == expected


def test_new_workflow_file_findings_exact(tmp_path: Path) -> None:
    new = ".github/workflows/new.yml"
    doc = yaml.safe_load(BASE_WF)
    assert rules(tmp_path, {}, {new: BASE_WF}) == [
        f"action.new {new} actions/checkout@{PIN}",
        f"ci-ok.changed {new} job ci-ok (or its needs) changed ({gd.digest(doc['jobs']['ci-ok'])})",
        f"gate.self {new} job grants-widening changed ({gd.digest(doc['jobs']['grants-widening'])})",
        *widened(new, WHO, ["contents"], "0->1"),  # a new file had no token at all
        f"secrets.new {new} OLD",
    ]


def test_workflow_removed_exact(tmp_path: Path) -> None:
    assert rules(tmp_path, {WF: BASE_WF}, {WF: None}) == [f"workflow.removed {WF} workflow file removed"]


@pytest.mark.parametrize(
    ("head", "detail"),
    [
        (wf(ON, "on: 5"), "on: 5"),
        (wf("permissions:\n  contents: read", "permissions: maybe"), "permissions: 'maybe'"),
        (wf("permissions:\n  contents: read", "permissions: {contents: admin}"), "permissions.contents: 'admin'"),
        (wf("permissions:\n  contents: read", "permissions: 5"), "permissions: 5"),
        ("jobs: [a]\n", "jobs is not a mapping"),
        ("- a\n", "not a YAML mapping"),
    ],
)
def test_unparsable_workflow_details(tmp_path: Path, head: str, detail: str) -> None:
    assert rules(tmp_path, {WF: BASE_WF}, {WF: head}) == [f"unparsed {WF} {detail} ({oid(head)})"]


def test_bad_yaml_detail_names_the_parser_error(tmp_path: Path) -> None:
    (found,) = rules(tmp_path, {WF: BASE_WF}, {WF: "jobs: [unclosed\n"})
    assert found.startswith(f"unparsed {WF} YAML: while parsing a flow sequence\n")
    assert found.endswith(f" ({oid('jobs: [unclosed\n')})")


ACTION = ".github/actions/setup/action.yml"
COMPOSITE = "runs:\n  using: composite\n  steps:\n    - run: echo\n      shell: bash\n"


def test_composite_action_is_judged_like_a_workflow(tmp_path: Path) -> None:
    head = COMPOSITE + "    - odd\n    - uses: evil/setup@v1\n"
    assert rules(tmp_path, {ACTION: COMPOSITE}, {ACTION: head}) == [f"action.new {ACTION} evil/setup@v1"]


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        (".github/actions/setup/action.yaml", COMPOSITE + "    - uses: evil/setup@v1\n", ["action.new"]),
        (
            ".github/workflows/ci.yaml",
            "permissions: {}\njobs:\n  a:\n    steps:\n      - run: make || true\n",
            ["control.disabled"],
        ),
        # line rules for scripts, not workflow rules: a shell script of an action, a .github config file
        (".github/actions/setup/run.sh", "make || true\n", ["control.disabled"]),
        (".github/dependabot.yml", "x: make || true\n", ["control.disabled"]),
        # neither: a YAML file elsewhere
        ("stand/compose.yml", "command: sh -c 'make || true'\n", []),
        # scripts: anything under scripts/, *.sh anywhere, Makefile*
        ("scripts/x.py", "run('make || true')\n", ["control.disabled"]),
        ("deploy/run.sh", "make || true\n", ["control.disabled"]),
        ("Makefile", "test:\n\t-make || true\n", ["control.disabled"]),
        ("deploy/Makefile.ci", "test:\n\tmake || true\n", ["control.disabled"]),
        ("app/x.py", "run('make || true')\n", []),
    ],
)
def test_which_files_are_workflows_and_scripts(tmp_path: Path, path: str, text: str, expected: list[str]) -> None:
    found = rules(tmp_path, {path: "# base\n"}, {path: text})
    assert [f.split()[0] for f in found] == expected, found


def test_a_new_github_config_file_is_not_a_workflow(tmp_path: Path) -> None:
    # As a workflow, a new file without `permissions:` would get the default token (write) everywhere.
    assert rules(tmp_path, {}, {".github/dependabot.yml": "version: 2\n"}) == []


# Line rules: the detail is the stripped line.
@pytest.mark.parametrize(
    ("path", "line", "expected"),
    [
        ("tests/test_a.py", "    pytest.skip('no db')  ", "tests.skipped tests/test_a.py pytest.skip('no db')"),
        ("app/a.py", "  x = 1  # noqa: E501", "lint.exception app/a.py x = 1  # noqa: E501"),
        ("scripts/a.sh", "  make || true", "control.disabled scripts/a.sh make || true"),
    ],
)
def test_line_findings_carry_the_stripped_line(tmp_path: Path, path: str, line: str, expected: str) -> None:
    assert rules(tmp_path, {path: "a\n"}, {path: f"a\n{line}\n"}) == [expected]


def test_every_added_line_of_every_hunk_counts(tmp_path: Path) -> None:
    base = "".join(f"x{i} = {i}\n" for i in range(40))
    head = "# noqa: first\n" + base.replace("x20 = 20\n", "x20 = 20  # noqa: one\nx21 = 0  # noqa: two\n")
    assert rules(tmp_path, {"a.py": base}, {"a.py": head}) == [
        "lint.exception a.py # noqa: first", "lint.exception a.py x20 = 20  # noqa: one",
        "lint.exception a.py x21 = 0  # noqa: two",
    ]  # fmt: skip


def test_a_diff_driver_cannot_hide_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # .gitattributes is in the PR; an external diff or textconv that prints nothing must not blind the gate.
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "true")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.hide.textconv")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    found = rules(tmp_path, {".gitattributes": "*.py diff=hide\n", "a.py": "x = 0\n"}, {"a.py": "x = 1  # noqa\n"})
    assert found == ["lint.exception a.py x = 1  # noqa"]


def test_a_path_that_also_reads_as_a_revision_is_still_a_path(tmp_path: Path) -> None:
    # Without "--", git reads ":(literal)a.py" as the index entry "(literal)a.py" and gives up.
    found = rules(tmp_path, {"(literal)a.py": "z\n", "a.py": "x = 0\n"}, {"a.py": "x = 1  # noqa\n"})
    assert found == ["lint.exception a.py x = 1  # noqa"]


def test_exception_list_lines_are_each_a_finding(tmp_path: Path) -> None:
    head = "# list\na.x__mutmut_1  # why\n  b.y__mutmut_2  # why  \n\n# c\nc.z__mutmut_3  # why\n"
    assert rules(tmp_path, {EQ: "# list\n"}, {EQ: head}) == [
        f"exceptions.added {EQ} a.x__mutmut_1  # why", f"exceptions.added {EQ} b.y__mutmut_2  # why",
        f"exceptions.added {EQ} c.z__mutmut_3  # why",
    ]  # fmt: skip


def test_docs_may_mention_any_rule(tmp_path: Path) -> None:
    assert rules(tmp_path, {}, {"docs/a.md": "`# noqa`\n`|| true`\n`@pytest.mark.skip`\n"}) == []


@pytest.mark.parametrize(
    ("base", "head"),
    [("[mypy]\nstrict = True\n", "[mypy]\nstrict = False\n"),
     ("[mypy]\nstrict = True\nx = 1\n", "[mypy]\nstrict = True\n"),
     ("[mypy]\n", None), (None, "[mypy]\n")],
)  # fmt: skip
def test_lint_config_detail_is_the_new_content_hash(tmp_path: Path, base: str | None, head: str | None) -> None:
    files = {} if base is None else {"mypy.ini": base}
    assert rules(tmp_path, files, {"mypy.ini": head}) == [
        f"lint.config mypy.ini config changed ({gd.digest(head or '')})"
    ]


def test_gate_and_policy_tool_details_are_the_new_content_hash(tmp_path: Path) -> None:
    tool = "tools/gxg-policy/src/gxg_policy/x.py"
    gone = "scripts/ci/widening_gate.py"
    assert rules(tmp_path, {GATE[0]: "a\n", gone: "g\n", tool: "b\n"}, {GATE[0]: "c\n", gone: None, tool: None}) == [
        f"gate.self {GATE[0]} gate file changed ({gd.digest('c\n')})",
        f"gate.self {gone} gate file changed ({gd.digest('')})",
        f"policy.tool {tool} gxg-policy changed ({gd.digest('')})",
    ]


GATE = ["scripts/ci/gate_diff.py"]


# --- collect: what cannot be parsed --------------------------------------------------------------------------


def test_unparsed_details_name_the_blob(tmp_path: Path) -> None:
    files = {"scripts/a.bin": b"\0\1", "scripts/b.bin": b"\0\2", "scripts/gone.bin": b"\0\3"}
    found = rules(tmp_path, {"scripts/gone.bin": b"\0\3"}, {**files, "scripts/gone.bin": None})
    assert found == [
        f"unparsed scripts/a.bin binary or unusual path ({oid(b'\0\1')})",
        f"unparsed scripts/b.bin binary or unusual path ({oid(b'\0\2')})",
        "unparsed scripts/gone.bin binary or unusual path (deleted)",
    ]


@pytest.mark.parametrize(
    ("path", "control"),
    [("app/x.py", True), ("app/LICENSE", True), ("app/x.PY", True), (".semgrepignore", True), ("app/x.png", False),
     (" lead.py", True)],
)  # fmt: skip
def test_binary_files_that_could_hold_a_control(tmp_path: Path, path: str, control: bool) -> None:
    found = rules(tmp_path, {}, {path: b"\0\1"})
    assert found == ([f"unparsed {path} binary or unusual path ({oid(b'\0\1')})"] if control else [])


@pytest.mark.parametrize("path", ["app/a\tb.py", "app/new\nline.py", '"quoted.py', os.fsdecode(b"app/bad\xff.py")])
def test_unusual_paths_need_the_label(tmp_path: Path, path: str) -> None:
    assert rules(tmp_path, {}, {path: "x = 1\n"}) == [f"unparsed {path} binary or unusual path ({oid('x = 1\n')})"]


def test_text_that_is_not_utf8_is_unparsed(tmp_path: Path) -> None:
    detail = "not UTF-8: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
    assert rules(tmp_path, {}, {"app/a.py": b"\xff x\n"}) == [f"unparsed app/a.py {detail} ({oid(b'\xff x\n')})"]


def test_a_rename_is_a_delete_and_an_add(tmp_path: Path) -> None:
    text = "".join(f"x{i} = {i}\n" for i in range(20)) + "y = 1  # noqa\n"
    found = rules(tmp_path, {"app/old.py": text, "scripts/old.bin": b"\0\1"},
                  {"app/old.py": None, "app/new.py": text, "scripts/old.bin": None,
                   "scripts/new.bin": b"\0\1"})  # fmt: skip
    assert found == [
        "lint.exception app/new.py y = 1  # noqa",
        f"unparsed scripts/new.bin binary or unusual path ({oid(b'\0\1')})",
        "unparsed scripts/old.bin binary or unusual path (deleted)",
    ]


# --- schemas -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ({"type": "string"}, [], ["/ changed"]),
        ({"maxLength": 5}, {}, ["/maxLength removed"]),
        ({"maxLength": 5, "properties": {"x": {}}}, {"maxLength": 9, "properties": {"x": {"not": {}}}},
         ["/maxLength changed", "/properties/x/not added"]),
        ({}, {"not": {}}, ["/not added"]),
        ({1: {}, "a": 1}, {1: {}, "a": 2}, ["/a changed"]),  # YAML keys need not be strings
        # added keywords that only tighten, and those that do not
        ({}, {"additionalProperties": False, "unevaluatedProperties": False, "uniqueItems": True, "minLength": 1}, []),
        ({}, {"additionalProperties": True}, ["/additionalProperties added"]),
        ({}, {"unevaluatedProperties": {}}, ["/unevaluatedProperties added"]),
        ({}, {"uniqueItems": False}, ["/uniqueItems added"]),
        # values
        ({"maxLength": 5}, {"maxLength": 10}, ["/maxLength changed"]),
        ({"maxLength": 5}, {"maxLength": 3}, []),
        ({"maximum": "10"}, {"maximum": 5}, ["/maximum changed"]),  # not numbers: not judged by value
        ({"minLength": 2}, {"minLength": 1}, ["/minLength changed"]),
        ({"const": 1}, {"const": 2}, ["/const changed"]),
        ({"const": 1}, {"const": False}, ["/const changed"]),
        ({"const": ["a", "b"]}, {"const": ["a"]}, ["/const changed"]),
        ({"const": {"a": 1}}, {"const": {"a": 2}}, ["/const changed"]),
        ({"required": ["a", "b"]}, {"required": ["b", "a"]}, []),
        ({"required": ["a", "b"]}, {"required": ["a"]}, ["/required changed"]),
        ({"enum": ["a", "b"]}, {"enum": "ab"}, ["/enum changed"]),
        ({"type": "string"}, {"type": ["string"]}, []),
        ({"type": ["string", "null"]}, {"type": "string"}, []),
        ({"type": "string"}, {"type": ["string", "null"]}, ["/type changed"]),
        ({"uniqueItems": False}, {"uniqueItems": True}, []),
        ({"uniqueItems": True}, {"uniqueItems": False}, ["/uniqueItems changed"]),
        ({"additionalProperties": {}}, {"additionalProperties": False}, []),
        ({"unevaluatedProperties": {"type": "string"}}, {"unevaluatedProperties": False}, []),
        ({"additionalProperties": False}, {"additionalProperties": {}}, ["/additionalProperties changed"]),
        # named subschemas
        ({"properties": {"x": {}, "y": {}}}, {"properties": {"y": {}}}, ["/properties/x removed"]),
        ({"properties": {}}, {"properties": {"x": {}}}, []),
        ({"properties": {}, "additionalProperties": False}, {"properties": {"x": {}}, "additionalProperties": False},
         ["/properties/x added to a closed object"]),
        ({"properties": {}, "unevaluatedProperties": False}, {"properties": {"x": {}}, "unevaluatedProperties": False},
         ["/properties/x added to a closed object"]),
        ({"patternProperties": {}, "additionalProperties": False},
         {"patternProperties": {"^x": {}}, "additionalProperties": False},
         ["/patternProperties/^x added to a closed object"]),
        ({"$defs": {}, "additionalProperties": False}, {"$defs": {"x": {}}, "additionalProperties": False}, []),
        ({"properties": {"x": {"maxLength": 1}}}, {"properties": {"x": {"maxLength": 2}}},
         ["/properties/x/maxLength changed"]),
        ({"properties": {1: {}, "a": {}}}, {"properties": {1: {}, "a": {"not": {}}}}, ["/properties/a/not added"]),
        ({"properties": []}, {"properties": {}}, ["/properties changed"]),
        ({"items": {"type": "string"}}, {"items": {"type": "string", "not": {}}}, ["/items/not added"]),
        ({"items": {}}, {"items": [{}]}, ["/items changed"]),
        # combinators
        ({"allOf": [{"type": "string"}]}, {"allOf": [{"type": "string"}, {"maxLength": 3}]}, []),
        ({"allOf": [{"maxLength": 5}, {}]}, {"allOf": [{"maxLength": 10}, {}]}, ["/allOf/0/maxLength changed"]),
        ({"allOf": [{}, {"maxLength": 5}]}, {"allOf": [{}, {"maxLength": 10}, {}]}, ["/allOf/1/maxLength changed"]),
        ({"allOf": [{}, {"maxLength": 5}]}, {"allOf": [{}]}, ["/allOf changed"]),
        ({"allOf": [{"type": "string"}]}, {"allOf": {"a": 1, "b": 2}}, ["/allOf changed"]),
        ({"anyOf": [{}, {"maxLength": 5}]}, {"anyOf": [{}, {"maxLength": 10}]}, ["/anyOf/1/maxLength changed"]),
        ({"oneOf": [{"maxLength": 5}]}, {"oneOf": [{"maxLength": 10}]}, ["/oneOf/0/maxLength changed"]),
        ({"prefixItems": [{"maxLength": 5}]}, {"prefixItems": [{"maxLength": 10}]},
         ["/prefixItems/0/maxLength changed"]),
        ({"anyOf": [{}]}, {"anyOf": [{}, {"type": "string"}]}, ["/anyOf changed"]),
    ],
)  # fmt: skip
def test_schema_weakening_exact(a: Any, b: Any, expected: list[str]) -> None:
    assert gd.schema_weakening(a, b) == expected


SX = "schemas/x.schema.json"


@pytest.mark.parametrize(
    ("path", "base", "head", "expected"),
    [
        (SX, schema(), schema(properties__id__maxLength=128), ["/properties/id/maxLength changed"]),
        ("schemas/x.yaml", "maxLength: 5\n", "maxLength: 9\n", ["/maxLength changed"]),
        ("schemas/x.yml", "maxLength: 5\n", "maxLength: 9\n", ["/maxLength changed"]),
        ("schemas/README.txt", "a\n", "b\n", ["changed (not a JSON/YAML schema)"]),
        # a .json schema is read as JSON only: YAML would read this as a string
        (SX, schema(), "not json", "unparsed"),
    ],
)  # fmt: skip
def test_schema_findings_exact(tmp_path: Path, path: str, base: str, head: str, expected: Any) -> None:
    if expected == "unparsed":
        expected_found = [f"unparsed {path} Expecting value: line 1 column 1 (char 0) ({oid(head)})"]
    else:
        expected_found = [f"schema.weakened {path} {w} ({gd.digest(head)})" for w in expected]
    assert rules(tmp_path, {path: base}, {path: head}) == expected_found


def test_removed_schema_exact(tmp_path: Path) -> None:
    assert rules(tmp_path, {SX: schema()}, {SX: None}) == [f"schema.weakened {SX} schema file removed"]


def test_digest() -> None:
    assert gd.digest("abc") == hashlib.sha256(b"abc").hexdigest()[:12] == "ba7816bf8f01"
    assert gd.digest({"b": 1, "a": [2]}) == hashlib.sha256(b'{"a": [2], "b": 1}').hexdigest()[:12]
    assert gd.digest({"d": datetime.date(2026, 1, 2)}) == hashlib.sha256(b'{"d": "2026-01-02"}').hexdigest()[:12]
    assert gd.digest(None) == hashlib.sha256(b"null").hexdigest()[:12]


# --- packages ------------------------------------------------------------------------------------------------

OPT = PY + '[project.optional-dependencies]\npg = ["psycopg>=3"]\n[tool.uv]\ndev-dependencies = ["ruff"]\n'


@pytest.mark.parametrize(
    ("base", "head", "expected"),
    [
        (OPT, OPT.replace('"psycopg>=3"', '"psycopg>=3", "evil"'), ["deps.new pyproject.toml evil"]),
        (OPT, OPT.replace('"ruff"', '"ruff", "evil"'), ["deps.new pyproject.toml evil"]),
        (OPT, OPT.replace('"PyYAML>=6"', '"PyYAML>=6", "evil"'), ["deps.new pyproject.toml evil"]),
        ('[tool.uv]\ndev-dependencies = ["ruff"]\n', '[tool.uv]\ndev-dependencies = ["ruff", "evil"]\n',
         ["deps.new pyproject.toml evil"]),
        (PY.replace('"PyYAML>=6"', '"zope.interface"'), PY.replace('"PyYAML>=6"', '"Zope_Interface>=6"'), []),
        (PY, PY.replace(', "PyYAML>=6"', ""), []),  # a removed package
        # the source of a URL package is all of it: a URL may hold ";"
        (PY.replace('"PyYAML>=6"', '"x @ https://h.invalid/x;v=1"'),
         PY.replace('"PyYAML>=6"', '"x @ https://h.invalid/x;v=2"'), ["deps.source pyproject.toml x @ https://h.invalid/x;v=2"]),
        # unchanged uv sources and lint sections are nothing, a changed one names its section and content hash
        (PY + '[tool.uv.sources]\nhttpx = { git = "https://h.invalid/x" }\n[tool.ruff]\nline-length = 1\n',
         PY.replace(">=0.27", ">=0.28") + '[tool.uv.sources]\nhttpx = { git = "https://h.invalid/x" }\n'
         '[tool.ruff]\nline-length = 1\n', []),
        (PY + '[tool.uv.sources]\nhttpx = { git = "https://h.invalid/x" }\n',
         PY + '[tool.uv.sources]\nhttpx = { git = "https://h.invalid/y" }\n',
         [f"deps.source pyproject.toml tool.uv.sources ({gd.digest({'httpx': {'git': 'https://h.invalid/y'}})})"]),
        (PY + "[tool.ruff]\nline-length = 1\n", PY + "[tool.ruff]\nline-length = 2\n",
         [f"lint.config pyproject.toml tool.ruff ({gd.digest({'line-length': 2})})"]),
    ],
)  # fmt: skip
def test_pyproject_exact(tmp_path: Path, base: str, head: str, expected: list[str]) -> None:
    assert rules(tmp_path, {"pyproject.toml": base}, {"pyproject.toml": head}) == expected


def test_policy_tool_pyproject_is_both_policy_tool_and_packages(tmp_path: Path) -> None:
    path = "tools/gxg-policy/pyproject.toml"
    head = PY.replace('"PyYAML>=6"', '"PyYAML>=6", "evil"')
    assert rules(tmp_path, {path: PY}, {path: head}) == [
        f"deps.new {path} evil",
        f"policy.tool {path} gxg-policy changed ({gd.digest(head)})",
    ]


@pytest.mark.parametrize(
    ("base", "head", "expected"),
    [
        (lock(("httpx", "0.27.0", PYPI)), lock(("httpx", "0.27.0", PYPI), ("evil", "1.0", PYPI)),
         ["deps.new uv.lock evil"]),
        (lock(("httpx", "0.27.0", PYPI)), lock(("httpx", "0.27.0", 'git = "https://h.invalid/x"')),
         ['deps.source uv.lock httpx: {"git": "https://h.invalid/x"}']),
        ("version = 1\n", lock(("httpx", "0.27.0", PYPI)), ["deps.new uv.lock httpx"]),  # no package before
        (lock(("httpx", "0.27.0", PYPI)), lock(("httpx", "0.27.0", PYPI)) + "\n[[package]]\nversion = '1'\n",
         ["deps.new uv.lock "]),  # a package without a name
        # the same source with its keys in another order
        (lock(("x", "1", 'registry = "r", extra = 1')), lock(("x", "2", 'extra = 1, registry = "r"')), []),
    ],
)  # fmt: skip
def test_uv_lock_exact(tmp_path: Path, base: str, head: str, expected: list[str]) -> None:
    assert rules(tmp_path, {"uv.lock": base}, {"uv.lock": head}) == expected


@pytest.mark.parametrize(("path", "text"), [("pyproject.toml", "[project\n"), ("uv.lock", "= x\n")])
def test_toml_detail_names_the_parser_error(tmp_path: Path, path: str, text: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        error = str(exc)
    assert rules(tmp_path, {path: "a = 1\n"}, {path: text}) == [f"unparsed {path} TOML: {error} ({oid(text)})"]


# --- the CLI and the carry-over entry point, on this module (mutmut sees calls through `scripts.ci.gate_diff`) --


def test_main_writes_every_finding_as_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    b = commit(repo, {"a.py": "x = 1\n"}, "b")
    h = commit(repo, {"a.py": "x = 1  # noqa\n"}, "h")
    out = tmp_path / "f.json"
    assert gd.main(["--repo", str(repo), "--base", b, "--head", h, "--json", str(out)]) == 0
    expected = [{"tenant": None, "kind": "gate", "rule": "lint.exception", "path": "a.py", "detail": "x = 1  # noqa"}]
    assert out.read_text(encoding="utf-8") == json.dumps(expected, indent=1) + "\n"
    seen: list[Any] = []

    def collect(*args: Any) -> list[Any]:
        seen.append(args)
        return []

    monkeypatch.setattr(gd, "collect", collect)
    gd.main(["--repo", "r", "--base", "B", "--head", "H", "--json", str(out)])
    assert seen == [(Path("r"), "B", "H")] and out.read_text(encoding="utf-8") == "[]\n"


def test_main_usage(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        gd.main(["--help"])
    out = capsys.readouterr().out
    # the module docstring is the description, kept verbatim (RawDescriptionHelpFormatter)
    assert "1. grants CI more: a new `secrets.*` reference, wider `permissions:`, a `pull_request_target` or\n" in out
    args = ["--repo", "r", "--base", "b", "--head", "h", "--json", "j"]
    for i in range(0, len(args), 2):
        with pytest.raises(SystemExit) as exc:
            gd.main(args[:i] + args[i + 2 :])
        assert exc.value.code == 2
        assert f"the following arguments are required: {args[i]}" in capsys.readouterr().err


POLICY_CASE = ROOT / "tools/gxg-policy/tests/diff/10-manifest-tools"  # policies and connector manifests


def test_widening_at_is_the_policy_diff_and_the_gate_findings_against_the_merge_base(
    tmp_path: Path, capfd: Any
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    shutil.copytree(POLICY_CASE / "base", repo / "policies")
    shutil.copytree(POLICY_CASE / "base-connectors", repo / "connectors")
    commit(repo, {"lib.py": "y = 1  # noqa: E741\n"}, "base")
    git(repo, "checkout", "-qb", "claude/x")
    shutil.rmtree(repo / "policies")
    shutil.rmtree(repo / "connectors")
    shutil.copytree(POLICY_CASE / "head", repo / "policies")
    shutil.copytree(POLICY_CASE / "head-connectors", repo / "connectors")
    head = commit(repo, {"a.py": "x = 1  # noqa: E501\n"}, "widen")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"lib.py": None}, "main moves on")  # against main's tip lib.py would come back: a finding
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    capfd.readouterr()
    found = gd.widening_at(repo, "refs/remotes/origin/main", head, tmp_path / "t")
    policy = json.loads((POLICY_CASE / "expected.json").read_text(encoding="utf-8"))["widening"]
    gate = {"tenant": None, "kind": "gate", "rule": "lint.exception", "path": "a.py", "detail": "x = 1  # noqa: E501"}
    assert found == [*policy, gate]
    work = tmp_path / "t" / f"carry-{head}"
    assert (work / "base/lib.py").exists() and (work / "head/a.py").exists()
    assert (work / "diff.md").read_text(encoding="utf-8").startswith("<!-- gxg-policy-diff -->")
    assert capfd.readouterr() == ("", "")  # the policy diff prints nothing into the job log
    # A branch name works too, even the branch checked out in `repo`: the worktrees are detached.
    assert gd.widening_at(repo, "refs/remotes/origin/main", "main", tmp_path / "t") == []
    with pytest.raises(subprocess.CalledProcessError):
        gd.widening_at(repo, "refs/remotes/origin/main", "f" * 40, tmp_path / "t")
    (repo / "policies/tenants/broken.yaml").parent.mkdir(parents=True, exist_ok=True)
    (repo / "policies/tenants/broken.yaml").write_text("{unclosed\n")
    bad = commit(repo, {}, "broken policy")
    with pytest.raises(subprocess.CalledProcessError):  # gxg-policy diff failed: no report is read
        gd.widening_at(repo, "refs/remotes/origin/main", bad, tmp_path / "t")
    assert capfd.readouterr() == ("", "")  # its error is in the exception, not in the log
