"""AC-M0-04: the CI secret check rejects commits with secret-like files.

Each test builds a throw-away Git repository and runs the same script CI runs
(scripts/ci/check_secrets.py) as a subprocess, checking the exit code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "check_secrets.py"

# Built from parts so that this test file itself never contains a key header.
PEM_HEADER = "-----BEGIN " + "PRIVATE KEY-----"
RSA_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"


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
    ],
)
def test_private_key_content_fails(repo: Path, content: str) -> None:
    commit(repo, {"config/settings.txt": content}, "add key")
    res = run(repo, "--tree")
    assert res.returncode == 1, res.stderr
    assert "PEM private key block" in res.stderr
    assert KEY_BODY not in res.stderr + res.stdout, "the scanner must not print secret material"


def test_clean_tree_passes(repo: Path) -> None:
    commit(
        repo,
        {
            ".env.example": "JWT_SECRET_KEY=__GENERATE__\n",
            "docs/plan.md": f"Reject files containing the line `{PEM_HEADER}`.\n",
            "src/app.py": "PATTERN = '-----BEGIN ' + 'PRIVATE KEY-----'\n",
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
