"""Repository pytest policy from conftest.py: no network (AC-M0-01) and no
skipped or xfailed tests (DoD-5)."""

from __future__ import annotations

import errno
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = (ROOT / "conftest.py").read_text(encoding="utf-8")


def test_internet_socket_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocked = r"^network access is forbidden in tests: \('192\.0\.2\.1', 443\)$"
        with pytest.raises(RuntimeError, match=blocked):
            s.connect(("192.0.2.1", 443))  # TEST-NET-1, never routed
        with pytest.raises(RuntimeError, match=blocked):
            s.connect_ex(("192.0.2.1", 443))
    finally:
        s.close()


def _run_inner(pytester: pytest.Pytester, body: str) -> pytest.RunResult:
    # In-process run with the repository conftest, so mutmut sees the code under test.
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_inner=body)
    return pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")


def _out(res: pytest.RunResult) -> str:
    return res.stdout.str()


@pytest.mark.parametrize(
    "body",
    [
        "import pytest\n\n@pytest.mark.skip(reason='x')\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n",
        "import pytest\n\ndef test_a():\n    pytest.skip('x')\n",
        "import pytest\n\n@pytest.mark.xfail(reason='x')\ndef test_a():\n    assert False\n",
        "import pytest\n\n@pytest.mark.xfail(reason='x', strict=False)\ndef test_a():\n    assert True\n",
        "import pytest\n\n@pytest.mark.xfail(reason='x', strict=False)\ndef test_a():\n    assert False\n",
        "import pytest\n\ndef test_a():\n    pytest.xfail('x')\n",
        "import pytest\n\npytest.skip('x', allow_module_level=True)\n\ndef test_a():\n    pass\n",
        "import pytest\n\nnope = pytest.importorskip('no_such_module_gxg')\n\ndef test_a():\n    pass\n",
        "import pytest\n\npytest.importorskip('no_such_module_gxg')\n",  # nothing else collected
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    @unittest.skip('x')\n    def test_a(self):\n        pass\n",
        "import unittest\n\n@unittest.skip('x')\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n",
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.skipTest('x')\n",
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n    def test_a(self):\n        assert False\n",
        "import pytest\n\n@pytest.mark.skipif(True, reason='x')\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n",
        "import pytest\n\n@pytest.fixture\ndef f():\n    pytest.skip('x')\n\ndef test_a(f):\n    pass\n",
    ],
    ids=[
        "skip-marker",
        "skip-call",
        "xfail",
        "xpass-non-strict",
        "xfail-non-strict",
        "xfail-call",
        "module-level-skip",
        "importorskip",
        "importorskip-only",
        "unittest-skip",
        "unittest-skip-class",
        "unittest-skipTest",
        "unittest-expectedFailure",
        "skipif-true",
        "skip-in-fixture",
    ],
)
def test_skip_or_xfail_makes_run_red(pytester: pytest.Pytester, body: str) -> None:
    res = _run_inner(pytester, body)
    assert res.ret == pytest.ExitCode.TESTS_FAILED, _out(res)
    assert any(line.startswith("FAILED: skipped/xfail tests are forbidden (DoD-5): ") for line in res.outlines)


def test_xpass_strict_false_overrides_ini(pytester: pytest.Pytester) -> None:
    """Even with xfail_strict=false in the inner run's config, an XPASS turns it red."""
    pytester.makeini("[pytest]\nxfail_strict = false\n")
    res = _run_inner(pytester, "import pytest\n\n@pytest.mark.xfail(reason='x')\ndef test_a():\n    pass\n")
    assert res.ret == pytest.ExitCode.TESTS_FAILED, _out(res)
    assert "test_inner.py::test_a (xpassed)" in _out(res)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("import pytest\n\ndef test_a():\n    pytest.skip('x')\n", "test_inner.py::test_a (skipped)"),
        ("import pytest\n\ndef test_a():\n    pytest.xfail('x')\n", "test_inner.py::test_a (xfailed)"),
        (
            "import pytest\n\npytest.skip('x', allow_module_level=True)\n",
            "test_inner.py (skipped at collection)",
        ),
    ],
    ids=["skipped", "xfailed", "collection"],
)
def test_violation_is_named_in_summary(pytester: pytest.Pytester, body: str, expected: str) -> None:
    assert expected in _out(_run_inner(pytester, body))


def test_two_violations_are_listed_with_separator(pytester: pytest.Pytester) -> None:
    body = "import pytest\n\ndef test_a():\n    pytest.skip('x')\n\ndef test_b():\n    pytest.skip('y')\n"
    assert "test_inner.py::test_a (skipped), test_inner.py::test_b (skipped)" in _out(_run_inner(pytester, body))


def test_failing_run_keeps_its_exit_code(pytester: pytest.Pytester) -> None:
    """A usage error with a skip stays a usage error: the policy only turns OK/no-tests red."""
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_inner="import pytest\n\ndef test_a():\n    pytest.skip('x')\n")
    res = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider", "--no-such-option")
    assert res.ret == pytest.ExitCode.USAGE_ERROR


def test_clean_run_stays_green(pytester: pytest.Pytester) -> None:
    res = _run_inner(pytester, "def test_a():\n    assert True\n")
    assert res.ret == pytest.ExitCode.OK, _out(res)
    assert "forbidden" not in _out(res)


def test_unix_socket_is_allowed() -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(FileNotFoundError):  # the guard let the call through to the OS
            s.connect("/nonexistent/gxg.sock")
        assert s.connect_ex("/nonexistent/gxg.sock") == errno.ENOENT
    finally:
        s.close()


def test_violation_without_node_id_is_named_session(monkeypatch: pytest.MonkeyPatch) -> None:
    import conftest as policy

    monkeypatch.setattr(policy, "_violations", [])
    policy._record("", "skipped at collection")
    assert policy._violations == ["<session> (skipped at collection)"]
