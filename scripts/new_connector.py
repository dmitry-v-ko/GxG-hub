"""Generator behind scripts/new-connector (docs/specs/mcp-toolkit-v1.md «Что генерирует шаблон», plan M1.1).

Renders templates/python-fastmcp with copier and locks the result. Default destination: connectors/<name> with the
kit by path; a destination outside this repository is a service in its own repository: the kit and the conformance
suite come by git from one pinned commit (default: this checkout's HEAD; it must be pushed before others can use it).
It creates no credentials, grants nothing and deploys nothing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import copier

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "templates" / "python-fastmcp"
DEFAULT_URL = "https://github.com/dmitry-v-ko/GxG-hub.git"
NAME = re.compile(r"[a-z][a-z0-9_]{1,30}")
SHA = re.compile(r"[0-9a-f]{40}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="new-connector", description=__doc__.splitlines()[0])
    p.add_argument("--name", required=True, help="connector domain: tools are <name>.<object>.<verb>")
    p.add_argument("--lang", default="python", choices=["python", "ts"])
    p.add_argument("--with-effects", action="store_true", help="also <name>.message.propose + internal execute")
    p.add_argument("--dest", type=Path, help="default: connectors/<name>")
    p.add_argument("--kit-source", choices=["path", "git"], help="default: path inside this repository, else git")
    p.add_argument("--kit-url", default=DEFAULT_URL)
    p.add_argument("--kit-rev", help="pinned GxG-hub commit (default: HEAD of this checkout)")
    a = p.parse_args(argv)
    if a.lang != "python":
        p.error("--lang ts: not in the toolkit yet (mcp-toolkit-v1: a TS kit comes with the first TS connector)")
    if not NAME.fullmatch(a.name):
        p.error("--name: ^[a-z][a-z0-9_]{1,30}$")
    dest = (a.dest or REPO / "connectors" / a.name).resolve()
    if dest.exists() and any(dest.iterdir()):
        p.error(f"{dest} exists and is not empty")
    inside = dest.is_relative_to(REPO)
    source = a.kit_source or ("path" if inside else "git")
    data: dict[str, object] = {"name": a.name, "with_effects": a.with_effects, "kit_source": source}
    if source == "path":
        if not inside:
            p.error("--kit-source path needs a destination inside this repository")
        data["repo"] = Path(*[".."] * len(dest.relative_to(REPO).parts)).as_posix()
    else:
        rev = a.kit_rev or subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
        if not SHA.fullmatch(rev):
            p.error("--kit-rev: a full 40-hex commit (a branch or tag would not be pinned)")
        data |= {"kit_url": a.kit_url, "kit_rev": rev}
    copier.run_copy(str(TEMPLATE), dest, data=data, defaults=True, quiet=True)
    for d in sorted(dest.rglob("*"), reverse=True):  # directories of files the variant does not have
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    subprocess.run(["uv", "lock", "--quiet"], cwd=dest, check=True)
    print(f"{dest}: connector {a.name} ({'with effects' if a.with_effects else 'read-only'}, kit by {source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
