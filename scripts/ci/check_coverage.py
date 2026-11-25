"""AC-M1-09: branch coverage of each security module of gxg-connector-kit is at least 90 %.

Usage: check_coverage.py <coverage.json>  (from `pytest --cov-branch --cov-report=json:<file>`).
Each module is checked on its own, on branches only (covered / total branches).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MODULES = ("exec_token", "svc_jwt", "grants", "canonical", "identity", "_jwt")  # _jwt: shared core of the JWT modules
THRESHOLD = 0.90


def main(path: str) -> int:
    files = json.loads(Path(path).read_text(encoding="utf-8"))["files"]
    failed = False
    for module in MODULES:
        matches = [s for name, s in files.items() if name.endswith(f"gxg_connector_kit/{module}.py")]
        if len(matches) != 1:
            print(f"{module}: not in the coverage report")
            failed = True
            continue
        summary = matches[0]["summary"]
        total, covered = summary["num_branches"], summary["covered_branches"]
        ratio = covered / total if total else 1.0
        ok = ratio >= THRESHOLD
        failed |= not ok
        print(f"{module}: branches {covered}/{total} = {ratio:.1%} {'ok' if ok else 'FAIL'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
