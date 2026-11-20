"""Repository pytest policy from conftest.py: no network (AC-M0-01) and no
skipped or xfailed tests (DoD-5)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_internet_socket_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access is forbidden"):
            s.connect(("192.0.2.1", 443))  # TEST-NET-1, never routed
        with pytest.raises(RuntimeError, match="network access is forbidden"):
            s.connect_ex(("192.0.2.1", 443))
    finally:
        s.close()


def _run_inner(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    shutil.copy(ROOT / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_inner.py").write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


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
def test_skip_or_xfail_makes_run_red(tmp_path: Path, body: str) -> None:
    res = _run_inner(tmp_path, body)
    assert res.returncode != 0, res.stdout
    assert "skipped/xfail tests are forbidden" in res.stdout


def test_xpass_strict_false_overrides_ini(tmp_path: Path) -> None:
    """Even with xfail_strict=false in the inner run's config, an XPASS turns it red."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nxfail_strict = false\n", encoding="utf-8")
    res = _run_inner(tmp_path, "import pytest\n\n@pytest.mark.xfail(reason='x')\ndef test_a():\n    pass\n")
    assert res.returncode != 0, res.stdout
    assert "xpassed" in res.stdout


def test_clean_run_stays_green(tmp_path: Path) -> None:
    res = _run_inner(tmp_path, "def test_a():\n    assert True\n")
    assert res.returncode == 0, res.stdout
