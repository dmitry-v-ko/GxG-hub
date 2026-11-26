#!/usr/bin/env python3
"""Control changes in a diff that need the `widening-approved` label (grants-widening, owner decision 2026-11-26).

Policies and connector manifests are judged by `gxg-policy diff`. This module judges everything else in
merge-base(main, head)..head and returns one finding per change that:

1. grants CI more: a new `secrets.*` reference, wider `permissions:`, a `pull_request_target` or
   `workflow_run` trigger, a new or re-pinned external action (`uses:`);
2. switches a control off: a change to the `ci-ok` job (its `needs` included), `continue-on-error`,
   `if: false`, `|| true`, skip/xfail/deselect, a gitleaks/mypy/ruff exception (inline or in config);
3. adds to an exception list (`scripts/ci/mutmut-equivalent.txt`, `.gitleaksignore`, other ignore files);
4. widens the policy tooling (`tools/gxg-policy/` outside its tests) or weakens `schemas/`;
5. edits the gate itself or CODEOWNERS;
6. adds a package or a package source (`pyproject.toml`, `uv.lock`); a version bump alone is not a finding.

What cannot be parsed reliably (bad YAML/TOML/JSON, a binary or quoted path, an unknown schema change)
is a finding too: fail closed. Findings carry no line numbers or SHAs, so the same change gives the same
finding after a merge of main; widening_gate.py uses that to keep a label across such merges.

Usage: gate_diff.py --repo DIR --base SHA --head SHA --json OUT
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

Finding = dict[str, Any]

GATE_SELF = {
    "scripts/ci/grants_widening.sh",
    "scripts/ci/widening_gate.py",
    "scripts/ci/gate_diff.py",
    "tests/ci/test_widening_gate.py",
    "tests/ci/test_grants_widening_job.py",
    "tests/ci/test_gate_diff.py",
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
}
DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
LINT_CONFIG_FILES = {"ruff.toml", ".ruff.toml", "mypy.ini", ".mypy.ini", "setup.cfg", "pytest.ini", "tox.ini",
                     ".coveragerc", ".pre-commit-config.yaml"}  # fmt: skip
LINT_SECTIONS = ("ruff", "mypy", "pytest", "coverage", "mutmut", "pyright")
UV_SOURCE_KEYS = ("sources", "index", "index-url", "extra-index-url", "find-links", "no-index",
                  "override-dependencies", "constraint-dependencies", "dependency-metadata")  # fmt: skip
HARMLESS_IGNORE = re.compile(r"(^|/)(\.gitignore|\.dockerignore|[^/]*\.dockerignore)$")

SKIP_RE = re.compile(
    r"pytest\.mark\.(skip|skipif|xfail)\b|pytest\.(skip|xfail|importorskip)\s*\(|unittest\.(skip|expectedFailure)"
    r"|@skip\w*|--deselect\b|--ignore(-glob)?\b|(^|\s)-k\s+['\"]?[^'\"\n]*\bnot\b|collect_ignore"
    r"|pytest_collection_modifyitems|pytest_ignore_collect|-p\s+no:"
)
LINT_RE = re.compile(
    r"#\s*noqa\b|#\s*type:\s*ignore|#\s*pyright:\s*ignore|#\s*mypy:|#\s*ruff:\s*noqa|gitleaks:allow"
    r"|pragma:\s*no\s*(cover|branch)|#\s*nosec\b|nosemgrep"
)
CONTROL_RE = re.compile(
    r"continue-on-error\s*:\s*(?!\s*false\s*$)|(^|\s)if\s*:\s*(\$\{\{\s*)?false\b|\|\|\s*(true|:)(?=[\s;)'\"`]|$)"
    r"|\|\|\s*exit\s+0\b|set\s+\+e\b"
)
SECRET_RE = re.compile(
    r"\bsecrets\s*\.\s*([A-Za-z0-9_-]+)|\bsecrets\s*\[|toJSON\(\s*secrets\s*\)|secrets\s*:\s*inherit"
)
REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
LEVEL = {"none": 0, "read": 1, "write": 2}
PRIVILEGED_TRIGGERS = {"pull_request_target", "workflow_run"}


class Unparsed(Exception):
    pass


def finding(rule: str, path: str, detail: str) -> Finding:
    return {"tenant": None, "kind": "gate", "rule": rule, "path": path, "detail": detail}


def digest(value: Any) -> str:
    """Short content hash: a coarse finding ("file changed") must differ when the content changes after a label."""
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()[:12]


# --- git -------------------------------------------------------------------------------------------------------


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=repo, check=True,
                          capture_output=True).stdout  # fmt: skip


def changed(repo: Path, base: str, head: str) -> list[tuple[str, str, bool]]:
    """(status A/M/D/T, path, binary) per changed file; renames are a delete plus an add."""
    status = git(repo, "diff", "--no-renames", "--name-status", "-z", base, head).split(b"\0")
    numstat = git(repo, "diff", "--no-renames", "--numstat", "-z", base, head).split(b"\0")
    binary = {f.split(b"\t", 2)[2].decode() for f in numstat if f.startswith(b"-\t-\t")}
    out = []
    for st, path in zip(status[0::2], status[1::2], strict=False):
        p = path.decode("utf-8", "surrogateescape")
        out.append((st.decode()[:1], p, p in binary))
    return out


def blob(repo: Path, sha: str, path: str) -> str | None:
    try:
        return git(repo, "show", f"{sha}:{path}").decode()
    except subprocess.CalledProcessError:
        return None
    except UnicodeDecodeError as exc:
        raise Unparsed(f"not UTF-8: {exc}") from exc


def line_changes(repo: Path, base: str, head: str, path: str) -> tuple[list[str], list[str]]:
    """Added and removed lines of one file (no context)."""
    raw = git(repo, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "-U0", base, head, "--",
              f":(literal){path}").decode("utf-8", "surrogateescape")  # fmt: skip
    added: list[str] = []
    removed: list[str] = []
    in_hunks = False
    for line in raw.splitlines():
        if line.startswith("@@"):
            in_hunks = True
        elif in_hunks and line.startswith("+"):
            added.append(line[1:])
        elif in_hunks and line.startswith("-"):
            removed.append(line[1:])
    return added, removed


# --- workflows -------------------------------------------------------------------------------------------------


def _load_yaml(text: str | None) -> dict[str, Any]:
    if text is None:
        return {}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise Unparsed(f"YAML: {exc}") from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise Unparsed("not a YAML mapping")
    return doc


def _perms(value: Any, scopes: set[str]) -> dict[str, int]:
    if isinstance(value, str):
        level = {"read-all": 1, "write-all": 2}.get(value)
        if level is None:
            raise Unparsed(f"permissions: {value!r}")
        return dict.fromkeys(scopes, level)
    if isinstance(value, dict):
        out = dict.fromkeys(scopes, 0)
        for k, v in value.items():
            if v not in LEVEL:
                raise Unparsed(f"permissions.{k}: {v!r}")
            out[str(k)] = LEVEL[v]
        return out
    raise Unparsed(f"permissions: {value!r}")


def _jobs(doc: dict[str, Any]) -> dict[str, Any]:
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        raise Unparsed("jobs is not a mapping")
    return jobs


def _effective(doc: dict[str, Any], job: Any, scopes: set[str]) -> dict[str, int]:
    if isinstance(job, dict) and "permissions" in job:
        return _perms(job["permissions"], scopes)
    if "permissions" in doc:
        return _perms(doc["permissions"], scopes)
    return dict.fromkeys(scopes, 2)  # repository default token: treat as write (fail closed)


def _scopes(*docs: dict[str, Any]) -> set[str]:
    names = {
        "actions",
        "attestations",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
    }
    for d in docs:
        for holder in [d, *[j for j in _jobs(d).values() if isinstance(j, dict)]]:
            p = holder.get("permissions")
            if isinstance(p, dict):
                names |= {str(k) for k in p}
    return names  # fmt: skip


def _triggers(doc: dict[str, Any]) -> set[str]:
    keys: dict[Any, Any] = doc
    on = keys["on"] if "on" in keys else keys.get(True)  # YAML 1.1 (PyYAML) reads the key `on` as True
    if on is None:
        return set()
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(x) for x in on}
    if isinstance(on, dict):
        return {str(x) for x in on}
    raise Unparsed(f"on: {on!r}")


def _uses(doc: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for job in _jobs(doc).values():
        if not isinstance(job, dict):
            continue
        if isinstance(job.get("uses"), str):
            out.add(job["uses"].strip())
        for st in job.get("steps") or []:
            if isinstance(st, dict) and isinstance(st.get("uses"), str):
                out.add(st["uses"].strip())
    runs = doc.get("runs")  # composite action
    if isinstance(runs, dict):
        for st in runs.get("steps") or []:
            if isinstance(st, dict) and isinstance(st.get("uses"), str):
                out.add(st["uses"].strip())
    return out


def _secrets(text: str | None) -> set[str]:
    return {m.group(1) or m.group(0) for m in SECRET_RE.finditer(text or "")}


def _permission_findings(path: str, a: dict[str, Any], b: dict[str, Any], new_file: bool) -> list[Finding]:
    scopes = _scopes(a, b)
    none = dict.fromkeys(scopes, 0)
    # The old workflow-level grant; a missing one is the repository default (write), a new file had none.
    top_old = none if new_file else _perms(a["permissions"], scopes) if "permissions" in a else dict.fromkeys(scopes, 2)
    top_new = _perms(b["permissions"], scopes) if "permissions" in b else dict.fromkeys(scopes, 2)
    out = [finding("permissions.widened", path, f"workflow: {s} {top_old[s]}->{top_new[s]}")
           for s in sorted(scopes) if top_new[s] > top_old[s]]  # fmt: skip
    ja, jb = _jobs(a), _jobs(b)
    for name, job in jb.items():
        # A new job is compared with what a job of the old file got without its own block (none in a new file).
        before = (
            _effective(a, ja[name], scopes) if name in ja else none if new_file or "permissions" not in a else top_old
        )
        after = _effective(b, job, scopes)
        out += [finding("permissions.widened", path, f"job {name}: {s} {before[s]}->{after[s]}")
                for s in sorted(scopes) if after[s] > before[s]]  # fmt: skip
    return out


def workflow_findings(path: str, old: str | None, new: str | None) -> list[Finding]:
    if new is None:
        return [finding("workflow.removed", path, "workflow file removed")]
    a, b = _load_yaml(old), _load_yaml(new)
    ja, jb = _jobs(a), _jobs(b)
    out = [finding("secrets.new", path, s) for s in sorted(_secrets(new) - _secrets(old))]
    out += _permission_findings(path, a, b, old is None)
    out += [finding("trigger.privileged", path, t) for t in sorted((_triggers(b) - _triggers(a)) & PRIVILEGED_TRIGGERS)]
    new_uses = sorted(_uses(b) - _uses(a))
    out += [finding("action.local" if u.startswith("./") else "action.new", path, u) for u in new_uses]
    for job, rule, what in (("ci-ok", "ci-ok.changed", "job ci-ok (or its needs) changed"),
                            ("grants-widening", "gate.self", "job grants-widening changed")):  # fmt: skip
        if ja.get(job) != jb.get(job):
            out.append(finding(rule, path, f"{what} ({digest(jb.get(job))})"))
    return out


# --- schemas ---------------------------------------------------------------------------------------------------

ANNOTATIONS = {"title", "description", "$comment", "examples", "example", "deprecated", "readOnly", "writeOnly",
               "default", "x-gxg-note"}  # fmt: skip
NAME_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
UPPER = {"maxLength", "maximum", "exclusiveMaximum", "maxItems", "maxProperties", "maxContains"}
LOWER = {"minLength", "minimum", "exclusiveMinimum", "minItems", "minProperties", "minContains"}
ADDABLE = UPPER | LOWER | {"required", "pattern", "format", "enum", "const", "multipleOf", "uniqueItems",
                           "additionalProperties", "unevaluatedProperties", "propertyNames",
                           "dependentRequired"}  # fmt: skip
SUBSCHEMA = {"items", "additionalItems", "contains", "if", "then", "else", "additionalProperties",
             "unevaluatedProperties", "unevaluatedItems", "propertyNames"}  # fmt: skip


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _closed(node: dict[str, Any]) -> bool:
    return node.get("additionalProperties") is False or node.get("unevaluatedProperties") is False


def _added(key: str, vb: Any) -> bool:
    """Whether adding keyword `key` with value `vb` can only tighten."""
    if key in {"additionalProperties", "unevaluatedProperties"}:
        return vb is False
    if key == "uniqueItems":
        return vb is True
    return key in ADDABLE


def _types(t: Any) -> set[Any]:
    return {t} if isinstance(t, str) else set(t)


def _tightened(key: str, va: Any, vb: Any) -> bool | None:
    """True/False for a keyword whose change is judged by value; None when it holds subschemas."""
    lists = isinstance(va, list) and isinstance(vb, list)
    checks: list[tuple[bool, Any]] = [
        (key in UPPER and _num(va) and _num(vb), lambda: vb < va),
        (key in LOWER and _num(va) and _num(vb), lambda: vb > va),
        (key == "required" and lists, lambda: set(va) <= set(vb)),
        (key == "enum" and lists, lambda: all(x in va for x in vb)),
        (
            key == "type" and isinstance(va, (str, list)) and isinstance(vb, (str, list)),
            lambda: _types(vb) <= _types(va),
        ),
        (key == "uniqueItems" and vb is True, lambda: True),
        (key in {"additionalProperties", "unevaluatedProperties"} and vb is False, lambda: True),
    ]
    for applies, verdict in checks:
        if applies:
            return bool(verdict())
    return None if key in NAME_MAPS | {"allOf", "anyOf", "oneOf", "prefixItems"} | SUBSCHEMA else False


def _sub(key: str, va: Any, vb: Any, b: dict[str, Any], at: str) -> list[str]:
    if key in NAME_MAPS and isinstance(va, dict) and isinstance(vb, dict):
        out: list[str] = []
        for name in sorted(set(va) | set(vb), key=str):
            if name not in vb:
                out.append(f"{at}/{name} removed")
            elif name not in va:
                if key in {"properties", "patternProperties"} and _closed(b):
                    out.append(f"{at}/{name} added to a closed object")
            else:
                out += schema_weakening(va[name], vb[name], f"{at}/{name}")
        return out
    lists = isinstance(va, list) and isinstance(vb, list)
    if key == "allOf" and lists and len(vb) >= len(va):  # a new allOf member only adds constraints
        return [w for i, (x, y) in enumerate(zip(va, vb, strict=False)) for w in schema_weakening(x, y, f"{at}/{i}")]
    if key in {"anyOf", "oneOf", "prefixItems"} and lists and len(va) == len(vb):
        return [w for i, (x, y) in enumerate(zip(va, vb, strict=True)) for w in schema_weakening(x, y, f"{at}/{i}")]
    if key in SUBSCHEMA and isinstance(va, dict) and isinstance(vb, dict):
        return schema_weakening(va, vb, at)
    return [f"{at} changed"]


def schema_weakening(a: Any, b: Any, where: str = "") -> list[str]:
    """Where `b` may accept something `a` rejects. Only changes known to tighten pass; the rest is weakening."""
    if a == b:
        return []
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return [f"{where or '/'} changed"]
    out: list[str] = []
    for key in sorted((set(a) | set(b)) - ANNOTATIONS, key=str):
        at = f"{where}/{key}"
        if key not in b:
            out.append(f"{at} removed")
        elif key not in a:
            if not _added(key, b[key]):
                out.append(f"{at} added")
        elif a[key] != b[key]:
            verdict = _tightened(key, a[key], b[key])
            if verdict is None:
                out += _sub(key, a[key], b[key], b, at)
            elif not verdict:
                out.append(f"{at} changed")
    return out


def _load_schema(path: str, text: str) -> Any:
    try:
        return json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise Unparsed(str(exc)) from exc


def schema_findings(path: str, old: str | None, new: str | None) -> list[Finding]:
    if old is None:
        return []  # a new schema weakens nothing that existed
    if new is None:
        return [finding("schema.weakened", path, "schema file removed")]
    if not path.endswith((".json", ".yaml", ".yml")):
        return [finding("schema.weakened", path, f"changed (not a JSON/YAML schema) ({digest(new)})")]
    weak = schema_weakening(_load_schema(path, old), _load_schema(path, new))
    return [finding("schema.weakened", path, f"{w} ({digest(new)})") for w in weak]


# --- packages --------------------------------------------------------------------------------------------------


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _reqs(items: Iterable[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in items:
        if isinstance(r, str) and (m := REQ_NAME.match(r)):
            out[_norm(m.group(1))] = r.split(";", 1)[0].strip()
    return out


def _deps(doc: dict[str, Any]) -> dict[str, str]:
    project = doc.get("project", {})
    items: list[Any] = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        items += group
    for group in doc.get("dependency-groups", {}).values():
        items += group
    items += doc.get("build-system", {}).get("requires", [])
    items += doc.get("tool", {}).get("uv", {}).get("dev-dependencies", [])
    return _reqs(items)


def _toml(path: str, text: str | None) -> dict[str, Any]:
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Unparsed(f"TOML: {exc}") from exc


def pyproject_findings(path: str, old: str | None, new: str | None) -> list[Finding]:
    a, b = _toml(path, old), _toml(path, new)
    da, db = _deps(a), _deps(b)
    out = [finding("deps.new", path, n) for n in sorted(set(db) - set(da))]
    out += [finding("deps.source", path, db[n]) for n in sorted(set(db) & set(da)) if "@" in db[n] and db[n] != da[n]]
    ua, ub = a.get("tool", {}).get("uv", {}), b.get("tool", {}).get("uv", {})
    out += [
        finding("deps.source", path, f"tool.uv.{k} ({digest(ub.get(k))})")
        for k in UV_SOURCE_KEYS
        if ua.get(k) != ub.get(k)
    ]
    ta, tb = a.get("tool", {}), b.get("tool", {})
    out += [
        finding("lint.config", path, f"tool.{k} ({digest(tb.get(k))})") for k in LINT_SECTIONS if ta.get(k) != tb.get(k)
    ]
    return out


def _locked(doc: dict[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in doc.get("package", []):
        out.setdefault(_norm(str(p.get("name", ""))), set()).add(json.dumps(p.get("source"), sort_keys=True))
    return out


def lock_findings(path: str, old: str | None, new: str | None) -> list[Finding]:
    a, b = _locked(_toml(path, old)), _locked(_toml(path, new))
    out = [finding("deps.new", path, n) for n in sorted(set(b) - set(a))]
    out += [finding("deps.source", path, f"{n}: {s}") for n in sorted(set(a) & set(b)) for s in sorted(b[n] - a[n])]
    return out


# --- line rules ------------------------------------------------------------------------------------------------


def _is_doc(path: str) -> bool:
    return Path(path).suffix.lower() in DOC_SUFFIXES


def _is_workflow(path: str) -> bool:
    return path.startswith(".github/") and path.endswith((".yml", ".yaml"))


def _is_script(path: str) -> bool:
    name = Path(path).name
    return _is_workflow(path) or path.startswith("scripts/") or path.endswith(".sh") or name.startswith("Makefile")


def _is_exception_list(path: str) -> bool:
    name = Path(path).name
    return name == "mutmut-equivalent.txt" or (name.endswith("ignore") and not HARMLESS_IGNORE.search(path))


CONTROL_DIRS = (".github/", "scripts/", "schemas/", "tools/gxg-policy/", "policies/", "tests/")
TEXT_SUFFIXES = {".py", ".sh", ".toml", ".ini", ".cfg", ".yml", ".yaml", ".json", ".txt", ".lock", ""}


def _controls(path: str) -> bool:
    """Whether a binary file here could hide a control change (code, config, lists): then it is unparsed."""
    return path.startswith(CONTROL_DIRS) or Path(path).suffix.lower() in TEXT_SUFFIXES or _is_exception_list(path)


def line_findings(path: str, added: list[str], removed: list[str], new: str | None = None) -> list[Finding]:
    out: list[Finding] = []
    for line in added:
        text = line.strip()
        if _is_exception_list(path):
            if text and not text.startswith("#"):
                out.append(finding("exceptions.added", path, text))
            continue
        if _is_doc(path):
            continue
        if SKIP_RE.search(line):
            out.append(finding("tests.skipped", path, text))
        if LINT_RE.search(line):
            out.append(finding("lint.exception", path, text))
        if _is_script(path) and CONTROL_RE.search(line):
            out.append(finding("control.disabled", path, text))
    name = Path(path).name
    if (name.startswith(".gitleaks") and name.endswith(".toml")) or name in LINT_CONFIG_FILES:
        if added or removed:
            out.append(finding("lint.config", path, f"config changed ({digest(new or '')})"))
    return out


# --- the whole diff --------------------------------------------------------------------------------------------


def file_findings(repo: Path, base: str, head: str, status: str, path: str) -> list[Finding]:
    old = None if status == "A" else blob(repo, base, path)
    new = None if status == "D" else blob(repo, head, path)
    out: list[Finding] = []
    if path in GATE_SELF:
        out.append(finding("gate.self", path, f"gate file changed ({digest(new or '')})"))
    if path.startswith("tools/gxg-policy/") and not path.startswith("tools/gxg-policy/tests/"):
        out.append(finding("policy.tool", path, f"gxg-policy changed ({digest(new or '')})"))
    if path.startswith("schemas/"):
        out += schema_findings(path, old, new)
    if path.startswith(".github/workflows/") or (path.startswith(".github/actions/") and _is_workflow(path)):
        out += workflow_findings(path, old, new)
    if Path(path).name == "pyproject.toml":
        out += pyproject_findings(path, old, new)
    if Path(path).name == "uv.lock":
        out += lock_findings(path, old, new)
    added, removed = line_changes(repo, base, head, path)
    out += line_findings(path, added, removed, new)
    return out


def collect(repo: Path, base: str, head: str) -> list[Finding]:
    out: list[Finding] = []
    for status, path, binary in changed(repo, base, head):
        oid = "deleted" if status == "D" else git(repo, "rev-parse", f"{head}:{path}").decode().strip()[:12]
        if (binary and _controls(path)) or not path.isprintable() or path.startswith('"'):
            out.append(finding("unparsed", path, f"binary or unusual path ({oid})"))
            continue
        if binary:
            continue  # an image or archive outside the control paths holds no rule of this gate
        try:
            out += file_findings(repo, base, head, status, path)
        except Unparsed as exc:
            out.append(finding("unparsed", path, f"{exc} ({oid})"))
    return out


def widening_at(repo: Path, main_ref: str, sha: str, tmp: Path) -> list[Finding]:
    """Every widening of merge-base(main, sha)..sha, as the job computes it for the head: gxg-policy diff + collect."""
    base = git(repo, "merge-base", main_ref, sha).decode().strip()
    out = tmp / f"carry-{sha}"
    for side, rev in (("base", base), ("head", sha)):
        git(repo, "worktree", "add", "--detach", str(out / side), rev)
    cmd = [sys.executable, "-m", "gxg_policy.cli", "diff", str(out / "base/policies"), str(out / "head/policies"),
           "--base-connectors", str(out / "base/connectors"), "--head-connectors", str(out / "head/connectors"),
           "--json", str(out / "diff.json"), "--md", str(out / "diff.md")]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True)
    report = json.loads((out / "diff.json").read_text(encoding="utf-8"))
    return [*report["widening"], *collect(repo, base, sha)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--json", type=Path, required=True)
    a = p.parse_args(argv)
    a.json.write_text(json.dumps(collect(a.repo, a.base, a.head), indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
