"""`check-connectors --connector <dir> [--report <file>] [pytest args…]`: 0 only if every applicable check passed."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import pytest

CHECKS = Path(__file__).with_name("checks.py")
TIMEOUT_S = 120  # per check (pytest-timeout): a hanging connector is a red check, not a stuck run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-connectors", description=__doc__)
    parser.add_argument("--connector", required=True, type=Path, help="connector directory")
    parser.add_argument("--report", type=Path, help="write {check id: status} as JSON")
    args, rest = parser.parse_known_args(argv)
    root = args.connector.resolve()
    if not (root / "gxg-manifest.yaml").is_file() or not (root / "pyproject.toml").is_file():
        parser.error(f"{root}: no gxg-manifest.yaml or pyproject.toml")
    report = args.report.resolve() if args.report else Path(tempfile.mkdtemp()) / "checks.json"
    code = pytest.main(
        [
            str(CHECKS),
            "-p", "gxg_connector_check.plugin",
            "-p", "no:cacheprovider",
            f"--rootdir={CHECKS.parent}",
            "-c", "/dev/null",  # the connector's own pytest settings do not change the checks
            "-o", "python_files=checks.py",
            f"--timeout={TIMEOUT_S}",
            "--connector", str(root),
            "--check-report", str(report),
            "-q",
            *rest,
        ]
    )  # fmt: skip
    if not report.is_file():
        return int(code) or 1
    results = json.loads(report.read_text(encoding="utf-8"))
    print("\n".join(f"{k:8} {v['status']}" for k, v in results.items()))
    bad = [k for k, v in results.items() if v["status"] not in ("passed", "not_applicable")]
    return 1 if bad or code != 0 else 0


if __name__ == "__main__":
    sys.exit(main())
