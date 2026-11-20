"""Repository-wide pytest policy.

* AC-M0-01: tests must not need the network. Any attempt to open an
  internet socket during a test fails that test.
* DoD-5: no test may be skipped or marked as expected to fail. A skip or
  xfail turns the whole run red.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

_ALLOWED_FAMILIES = {getattr(socket, "AF_UNIX", None)}


class NetworkAccessError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(self: socket.socket, address: Any) -> None:
        if self.family not in _ALLOWED_FAMILIES:
            raise NetworkAccessError(f"network access is forbidden in tests: {address!r}")

    def connect(self: socket.socket, address: Any) -> None:
        guard(self, address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        guard(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield


_skipped: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        _skipped.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _skipped and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if _skipped:
        terminalreporter.write_line(
            "FAILED: skipped/xfail tests are forbidden (DoD-5): " + ", ".join(_skipped),
            red=True,
        )
