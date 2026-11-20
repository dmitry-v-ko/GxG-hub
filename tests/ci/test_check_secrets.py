"""AC-M0-04: the CI secret check rejects commits with secret-like files.

Each test builds a throw-away Git repository and runs the same script CI runs
(scripts/ci/check_secrets.py) as a subprocess, checking the exit code.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "check_secrets.py"

# Built from parts so that this test file itself never contains a key header.
PEM_HEADER = "-----BEGIN " + "PRIVATE KEY-----"
RSA_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"


def load_scanner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_secrets_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@example.invalid")
    git(tmp_path, "config", "user.name", "CI")
    git(tmp_path, "config", "commit.gpgsign", "false")
    commit(tmp_path, {"README.md": "clean\n"}, "init")
    return tmp_path


def commit(repo: Path, files: dict[str, str], message: str, remove: tuple[str, ...] = ()) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        git(repo, "add", "-f", rel)  # -f: .gitignore must not be the only defence
    for rel in remove:
        git(repo, "rm", "-q", rel)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "path",
    [
        "server.pem",
        "deploy/tls/cert.pem",
        ".env",
        "approval/.env",
        ".env.local",
        ".env.production",
        "keys/service.key",
        "client.p12",
        "id_ed25519",
        # case-insensitive (AC-M0-04, critic M0 cycle 1)
        "KEY.PEM",
        "server.Pem",
        ".ENV",
        "app/.Env.Local",
        "Server.Key",
        "Client.P12",
    ],
)
def test_forbidden_file_names_fail(repo: Path, path: str) -> None:
    commit(repo, {path: "placeholder\n"}, "add file")
    res = run(repo, "--tree")
    assert res.returncode == 1, res.stderr
    assert path in res.stderr


@pytest.mark.parametrize(
    "content",
    [
        f"{PEM_HEADER}\n{KEY_BODY}\n-----END PRIVATE KEY-----\n",
        f"{PEM_HEADER}\n",  # the bare header line from AC-M0-04
        f"   {RSA_HEADER}\n{KEY_BODY}\n",
        '{"key": "' + PEM_HEADER + "\\n" + KEY_BODY + '"}\n',  # key pasted into JSON
        "token: |\n  " + PEM_HEADER + "\n  " + KEY_BODY + "\n",  # YAML block
        # Any occurrence, anywhere in the line (critic M0 cycle 1):
        'KEY="' + PEM_HEADER + KEY_BODY + '"\n',  # one-line key after KEY=
        "PRIVATE_KEY=" + PEM_HEADER + "\n",
        "see the value " + PEM_HEADER + " in prose\n",
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n",
        "-----BEGIN ENCRYPTED " + "PRIVATE KEY-----\n",
        "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\n",
        "-----begin " + "private key-----\n",  # lower case
        "x=-----BEGIN " + "PRIVATE KEY",  # no trailing dashes, no newline at EOF
    ],
)
def test_private_key_content_fails(repo: Path, content: str) -> None:
    commit(repo, {"config/settings.txt": content}, "add key")
    res = run(repo, "--tree")
    assert res.returncode == 1, res.stderr
    assert "PEM private key header" in res.stderr
    assert KEY_BODY not in res.stderr + res.stdout, "the scanner must not print secret material"


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("app/settings.py", "k = '" + PEM_HEADER + "'\n"),
        ("app/settings.py", 'KEY = "' + PEM_HEADER + "\\n" + KEY_BODY + '"\n'),
        ("deploy/values.yaml", "tls:\n  key: '" + PEM_HEADER + KEY_BODY + "'\n"),
        ("README.md", "Paste `" + PEM_HEADER + "` here\n"),
        ("data/blob.bin", "\x00\x01" + PEM_HEADER + "\x00"),
        ("win/key.txt", None),  # UTF-16 encoded file
    ],
    ids=["py-single-quotes", "py-escaped-newline", "yaml-inline", "markdown-prose", "binary", "utf16"],
)
def test_private_key_header_in_any_file_type_fails(repo: Path, path: str, content: str | None) -> None:
    if content is None:
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(("KEY=" + PEM_HEADER + "\n").encode("utf-16-le"))
        git(repo, "add", "-f", path)
        git(repo, "commit", "-q", "-m", "utf16")
    else:
        commit(repo, {path: content}, "add key")
    res = run(repo, "--tree")
    assert res.returncode == 1, res.stderr
    assert f"{path}: PEM private key header" in res.stderr


def test_content_allowlist_is_exact_path(repo: Path) -> None:
    text = f"| AC-M0-04 | ... строка `{PEM_HEADER}` |\n"
    commit(repo, {"docs/implementation-plan-v1.md": text}, "plan")
    assert run(repo, "--tree").returncode == 0  # the allowlisted normative plan
    # The same text at any other path is reported.
    for other in ("docs/implementation-plan-v2.md", "docs/sub/implementation-plan-v1.md", "implementation-plan-v1.md"):
        commit(repo, {other: text}, f"copy to {other}")
        res = run(repo, "--tree")
        assert res.returncode == 1, other
        assert f"{other}: PEM private key header" in res.stderr
        git(repo, "rm", "-q", other)
        git(repo, "commit", "-q", "-m", "rm")


def test_content_allowlist_is_minimal_and_path_exact() -> None:
    mod = load_scanner()
    assert set(mod.CONTENT_ALLOWLIST) == {"docs/implementation-plan-v1.md"}, "allowlist must stay minimal"
    assert {p for p, _ in mod.HISTORICAL_BLOB_ALLOWLIST} == {
        "reports/conformance/M0.json",
        "reports/critic/M0-cycle-1.json",
    }
    assert mod.name_rule("docs/implementation-plan-v1.md") is None
    assert mod.content_rule(PEM_HEADER.encode(), "docs/implementation-plan-v1.md") is None
    assert mod.content_rule(PEM_HEADER.encode(), "docs/other.md") is not None


def test_historical_blob_allowlist_pins_exact_content(repo: Path) -> None:
    """A pinned (path, blob) pair is exempt only for that exact content and path."""
    mod = load_scanner()
    pinned = f"quote of AC-M0-04: `{PEM_HEADER}`\n".encode()
    path = "reports/x.json"
    setattr(mod, "HISTORICAL_BLOB_ALLOWLIST", {(path, mod.git_blob_sha(pinned)): "test"})  # noqa: B010
    assert mod.content_rule(pinned, path) is None
    assert mod.content_rule(pinned, "reports/other.json") is not None  # same content, other path
    assert mod.content_rule(pinned + b" ", path) is not None  # same path, any other content
    assert mod.content_rule(PEM_HEADER.encode(), path) is not None
    # git_blob_sha matches `git hash-object`
    (repo / "blob.txt").write_bytes(pinned)
    assert mod.git_blob_sha(pinned) == git(repo, "hash-object", "blob.txt")


def test_clean_tree_passes(repo: Path) -> None:
    commit(
        repo,
        {
            ".env.example": "JWT_SECRET_KEY=__GENERATE__\n",
            "docs/plan.md": "Reject files containing a PEM private key header.\n",
            "src/app.py": "PATTERN = '-----BEGIN ' + 'PRIVATE KEY-----'\n",
            "docs/keys.md": "-----BEGIN PUBLIC KEY-----\nMIIBIjAN\n-----END PUBLIC KEY-----\n",
            "certs/ca.crt": "-----BEGIN CERTIFICATE-----\n",
            "src/keyboard.py": "x = 1\n",
            "docs/env.md": "environment\n",
        },
        "docs",
    )
    res = run(repo, "--tree")
    assert res.returncode == 0, res.stderr


def test_secret_added_and_removed_within_range_fails(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"secrets.pem": "x\n"}, "oops")
    head = commit(repo, {}, "remove", remove=("secrets.pem",))
    assert run(repo, "--tree").returncode == 0  # final tree is clean ...
    res = run(repo, "--range", f"{base}..{head}")
    assert res.returncode == 1  # ... but the commit history is not
    assert "secrets.pem" in res.stderr


def test_range_from_zero_sha_scans_whole_branch(repo: Path) -> None:
    commit(repo, {"a/.env": "X=1\n"}, "oops")
    head = commit(repo, {}, "remove", remove=("a/.env",))
    assert run(repo, "--range", f"{'0' * 40}..{head}").returncode == 1


def test_range_with_only_clean_commits_passes(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"src/x.py": "x = 1\n"}, "clean")
    assert run(repo, "--range", f"{base}..{head}").returncode == 0


def test_bad_revision_is_an_error_not_a_pass(repo: Path) -> None:
    assert run(repo, "--range", "deadbeef..HEAD").returncode == 2
    assert run(repo, "--tree", "no-such-rev").returncode == 2


def test_this_repository_is_clean() -> None:
    res = run(ROOT, "--tree")
    assert res.returncode == 0, res.stderr
