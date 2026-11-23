"""M1.1: templates/python-fastmcp and scripts/new-connector (AC-M1.1-01, -03, -04; EX part of -02).

AC-M1.1-01/-03 (e2e): `new-connector --name sample [--with-effects]` into a temporary directory outside the repository,
made its own git repository, with the kit and the conformance suite as git dependencies on this repository's HEAD
(file:// URL, so no network to GitHub; PyPI only, AC-M0-01); its scripts/check-all.sh (lock, ruff, mypy, pytest,
check-connectors) must pass without edits. HEAD must hold the template under test: run on a commit (check-all does).
AC-M1.1-04: the files the template owns are byte-identical in connectors/demo and in a render with demo's answers.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
import sys
from pathlib import Path

import copier
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "templates" / "python-fastmcp"
DEMO = REPO / "connectors" / "demo"
CONF = yaml.safe_load((REPO / "copier.yml").read_text(encoding="utf-8"))
DEMO_OWNED = {  # what `copier update` keeps in step in demo (everything else is its domain part)
    ".copier-answers.yml",
    "Dockerfile",
    "Dockerfile.dockerignore",
    "src/demo_connector/__main__.py",
    "src/demo_connector/server.py",
}


def render(dest: Path, **data: object) -> Path:
    copier.run_copy(str(REPO), dest, data=data, defaults=True, quiet=True)
    return dest


def files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def owned(rel: str) -> bool:
    return not any(fnmatch.fnmatch(rel, pat.replace("/**", "/*")) for pat in CONF["_skip_if_exists"])


def test_template_owns_exactly_the_plumbing(tmp_path: Path) -> None:
    """`_skip_if_exists` leaves to the template only the files that are the same in every connector."""
    out = render(tmp_path / "demo", name="demo", with_effects=False, kit_source="path", repo="../..")
    assert {f for f in files(out) if owned(f)} == DEMO_OWNED


# demo keeps its own stage C (M8 effects.py, stage_c.py) instead of gxg_connector_kit.effects (docs/backlog.md, M1.1):
# the only allowed differences from the `with_effects: true` render, each (file, template text, demo text, why).
DEMO_DRIFT = [
    (
        "src/demo_connector/server.py",
        "port 8081 (gxg_connector_kit.effects).",
        "port 8081 (demo's own effects.py, M8).",
        "the docstring names where demo's effects live",
    ),
    (
        "src/demo_connector/server.py",
        "from gxg_connector_kit import effects, mcp\n",
        "from gxg_connector_kit import mcp\n",
        "the kit's effects module is not used by demo",
    ),
    (
        "src/demo_connector/server.py",
        "from .backend import Backend\n",
        "from . import stage_c as effects\nfrom .backend import Backend\n"
        "from .stage_c import Wiring  # noqa: F401 - M8 tests use server.Wiring\n",
        "stage_c supplies connect/Wiring/Settings in place of the kit's effects; M8 tests use server.Wiring",
    ),
    (
        "src/demo_connector/server.py",
        "class Settings(mcp.Settings):",
        "class Settings(effects.Settings):",
        "M8 settings: DEMO_PROVIDER_DB, approval_url/svc_key_file None when unset",
    ),
    (
        "src/demo_connector/server.py",
        "connector.enable_effects(approval, jwks, files.refresh)",
        "connector.enable_effects(approval, jwks, files.refresh, effects.provider(settings))",
        "M8 fake provider, a separate journal from the read backend",
    ),
    (
        "src/demo_connector/server.py",
        "test_app(connector.backend)",
        "test_app(connector.backend, connector.provider)",
        "M8 test controls of the fake provider (stand: provider modes)",
    ),
]


def demo_render(out: Path, rel: str) -> bytes:
    text = (out / rel).read_text(encoding="utf-8")
    for f, template, demo, _why in DEMO_DRIFT:
        if f == rel:
            assert text.count(template) == 1, (rel, template)  # an allowed difference the template no longer has
            text = text.replace(template, demo)
    return text.encode()


def test_demo_is_on_the_template(tmp_path: Path) -> None:
    """AC-M1.1-04: demo's template-owned files are the render of its own .copier-answers.yml, up to DEMO_DRIFT."""
    answers = yaml.safe_load((DEMO / ".copier-answers.yml").read_text(encoding="utf-8"))
    assert answers["_src_path"] == "../.."  # the repository: copier.yml at its root
    assert answers["with_effects"] is True  # demo has demo.message.propose since M8
    data = {k: v for k, v in answers.items() if not k.startswith("_")}
    out = render(tmp_path / "demo", **data)
    assert {f for f, *_ in DEMO_DRIFT} <= DEMO_OWNED
    for rel in sorted(DEMO_OWNED - {".copier-answers.yml"}):
        assert (DEMO / rel).read_bytes() == demo_render(out, rel), f"connectors/demo/{rel} drifted from the template"


@pytest.mark.parametrize("source", ["path", "git"])
def test_dockerignore_keeps_local_secrets_out(source: str, tmp_path: Path) -> None:
    """SEC-17: a provider token in a local .env never reaches the build context."""
    data: dict[str, object] = {"name": "sample", "with_effects": True, "kit_source": source}
    data |= {"repo": "../.."} if source == "path" else {"kit_rev": "0" * 40}
    out = render(tmp_path / "s", **data)
    ignore = (out / ("Dockerfile.dockerignore" if source == "path" else ".dockerignore")).read_text(encoding="utf-8")
    for pattern in ("**/.env*", "**/*.pem", "**/*.key", "**/mutants", "**/.pytest_cache"):
        assert pattern in ignore.splitlines(), pattern


def test_template_search_result_schema_is_the_repository_one() -> None:
    copy = TEMPLATE / "template" / "schemas" / "search-result.schema.json"
    assert json.loads(copy.read_bytes()) == json.loads((REPO / "schemas" / "search-result.schema.json").read_bytes())


def test_generator_refuses_bad_input(tmp_path: Path) -> None:
    gen = [sys.executable, str(REPO / "scripts" / "new_connector.py")]
    for args, why in (
        (["--name", "Bad-Name"], "--name"),
        (["--name", "ok_name", "--lang", "ts"], "--lang ts"),
        (["--name", "ok_name", "--dest", str(tmp_path / "x"), "--kit-rev", "main"], "--kit-rev"),
        (["--name", "ok_name", "--dest", str(tmp_path / "y"), "--kit-source", "path"], "inside this repository"),
    ):
        proc = subprocess.run([*gen, *args], capture_output=True, text=True, timeout=120, check=False)
        assert proc.returncode == 2 and why in proc.stderr, (args, proc.stderr)
    assert not (tmp_path / "x").exists() and not (tmp_path / "y").exists()


@pytest.mark.e2e
@pytest.mark.parametrize("effects", [True, False], ids=["with-effects", "read-only"])
def test_new_service_in_its_own_repository_passes_its_gate(effects: bool, tmp_path: Path) -> None:
    """AC-M1.1-01, AC-M1.1-03."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True)
    rev = head.stdout.strip()
    dest = tmp_path / "sample"
    cmd = [str(REPO / "scripts" / "new-connector"), "--name", "sample", "--lang", "python", "--dest", str(dest),
           "--kit-url", f"file://{REPO}", "--kit-rev", rev, *(["--with-effects"] if effects else [])]  # fmt: skip
    subprocess.run(cmd, check=True, timeout=600)
    pyproject = (dest / "pyproject.toml").read_text(encoding="utf-8")
    for sub in ("packages/gxg-connector-kit", "tools/gxg-connector-check"):
        assert f'git = "file://{REPO}", subdirectory = "{sub}", rev = "{rev}"' in pyproject
    assert "path =" not in pyproject  # nothing from a checkout of GxG-hub
    assert "uv run check-connectors --connector ." in (dest / "scripts" / "check-all.sh").read_text(encoding="utf-8")
    assert "run: scripts/check-all.sh" in (dest / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "enabled: false" in (dest / "contextforge" / "registration.example.yaml").read_text(encoding="utf-8")
    git = ["git", "-c", "user.email=ci@example.invalid", "-c", "user.name=ci"]
    for step in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "generated"]):
        subprocess.run([*git, *step], cwd=dest, check=True, timeout=60)
    proc = subprocess.run(["scripts/check-all.sh"], cwd=dest, capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "check-all: OK" in proc.stdout
    checks = proc.stdout[proc.stdout.rindex("EX-01") :].splitlines()
    assert all(line.split()[1] == ("passed" if effects or not line.startswith("EX") else "not_applicable")
               for line in checks if line[:2] in ("EX", "MC", "SC"))  # fmt: skip
    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True, check=True)
    assert status.stdout == "", "the gate changed files of the service"


SAMPLE_BROKEN = {  # AC-M1.1-02 for the effect checks: broken copies of the generated `sample --with-effects`
    "ex03-any-caller": ("EX-03", {"EX-03"}),  # execute accepts any caller: no service JWT check (AS-06)
    "ex03-unverified-token": ("EX-03", {"EX-03", "EX-04"}),  # «execute без токена»: no signature check at all
    "ex03-serves-everything": ("EX-03", {"EX-03"}),  # serves() is always True: any target (EFF-18 п.3)
    "ex04-no-jti-cache": ("EX-04", {"EX-04"}),  # a replayed token reaches dispatch (EFF-18 п.1)
    "ex11-retry-lost-answer": ("EX-11", {"EX-11"}),  # a lost answer is retried: a second provider call
}


@pytest.mark.e2e
@pytest.mark.parametrize("name", [None, *sorted(SAMPLE_BROKEN)])
def test_broken_sample_fails_on_its_effect_check(name: str | None, tmp_path: Path) -> None:
    root = render(tmp_path / "sample", name="sample", with_effects=True, kit_source="path", repo="../..")
    patches = REPO / "tools" / "gxg-connector-check" / "tests" / "broken" / "sample"
    if name is not None:
        subprocess.run(["git", "apply", str(patches / f"{name}.patch")], cwd=root, check=True, timeout=60)
    report = tmp_path / "report.json"
    cmd = [sys.executable, "-m", "gxg_connector_check.cli", "--connector", str(root), "--report", str(report)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False, cwd=tmp_path)
    failed = {k for k, v in json.loads(report.read_text(encoding="utf-8")).items() if v["status"] == "failed"}
    if name is None:
        assert proc.returncode == 0 and not failed, proc.stdout
    else:
        assert proc.returncode == 1 and failed == SAMPLE_BROKEN[name][1], (failed, proc.stdout[-3000:])
    shutil.rmtree(root)


@pytest.mark.e2e
def test_copier_update_brings_template_changes_and_keeps_the_domain(tmp_path: Path) -> None:
    """The reason for copier (plan «Лестница», DoD-6): a service follows the template by `copier update`."""
    hub = tmp_path / "hub"
    subprocess.run(["git", "clone", "--quiet", str(REPO), str(hub)], check=True, timeout=300)
    git = ["git", "-c", "user.email=ci@example.invalid", "-c", "user.name=ci"]
    old = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=hub, capture_output=True, text=True, check=True
    ).stdout.strip()
    svc = tmp_path / "svc"
    cmd = [str(REPO / "scripts" / "new-connector"), "--name", "sample", "--dest", str(svc), "--kit-url",
           f"file://{hub}", "--kit-rev", old]  # fmt: skip
    subprocess.run(cmd, check=True, timeout=600)
    answers = yaml.safe_load((svc / ".copier-answers.yml").read_text(encoding="utf-8"))
    assert answers["_src_path"] == f"git+file://{hub}" and answers["kit_rev"] == old and answers["_commit"]
    for step in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "generated"]):
        subprocess.run([*git, *step], cwd=svc, check=True, timeout=60)
    src = hub / "templates" / "python-fastmcp" / "template" / "src" / "{{ name }}_connector"
    for name in ("server.py.jinja", "tools.py.jinja"):
        with (src / name).open("a", encoding="utf-8") as f:
            f.write("# template change\n")
    subprocess.run([*git, "commit", "-qam", "template change"], cwd=hub, check=True, timeout=60)
    new = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=hub, capture_output=True, text=True, check=True
    ).stdout.strip()
    copier.run_update(str(svc), vcs_ref=new, defaults=True, overwrite=True, quiet=True)
    pkg = svc / "src" / "sample_connector"
    assert (pkg / "server.py").read_text(encoding="utf-8").endswith("# template change\n")  # owned by the template
    assert "# template change" not in (pkg / "tools.py").read_text(encoding="utf-8")  # owned by the service
    assert yaml.safe_load((svc / ".copier-answers.yml").read_text(encoding="utf-8"))["_commit"] != answers["_commit"]
