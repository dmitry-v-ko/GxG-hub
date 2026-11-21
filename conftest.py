"""Repository-wide pytest policy.

* AC-M0-01: tests must not need the network. pytest-socket (addopts in
  pyproject.toml) fails any test that creates a non-Unix socket. No per-test
  opt-out: the block is applied again after all fixtures, so the
  enable_socket / allow_hosts markers and the socket_enabled fixture do not lift it.
* DoD-5: no test may be skipped or marked as expected to fail. Any skip
  (marker, call, unittest.skip, skipif, or at collection time: module-level
  skip, importorskip) and any xfail or XPASS (strict or not) turns the whole
  run red.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_socket import disable_socket

_violations: list[str] = []


def _record(nodeid: str, what: str) -> None:
    _violations.append(f"{nodeid or '<session>'} ({what})")


def pytest_runtest_call(item: pytest.Item) -> None:
    disable_socket(allow_unix_socket=item.config.getoption("--allow-unix-socket"))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # xfail and XPASS (strict or not) carry `wasxfail`; a plain skip is `skipped`.
    if hasattr(report, "wasxfail"):
        _record(report.nodeid, "xpassed" if report.passed else "xfailed")
    elif report.skipped:
        _record(report.nodeid, "skipped")


def pytest_collectreport(report: pytest.CollectReport) -> None:
    # Skips at collection time: pytest.skip(allow_module_level=True), module-level
    # pytest.importorskip(...). They never reach pytest_runtest_logreport.
    if report.skipped:
        _record(report.nodeid, "skipped at collection")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _violations and session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if _violations:
        terminalreporter.write_line(
            "FAILED: skipped/xfail tests are forbidden (DoD-5): " + ", ".join(_violations),
            red=True,  # pragma: no mutate (colour only)
        )
