"""AC-M0-04, AC-M0.1-01: the CI secret scan (gitleaks via scripts/ci/gitleaks.sh) rejects secrets.

Every test builds a throw-away Git repository, generates the secret at test time
(keys with `openssl`, nothing secret-like is stored in this repository) and runs
the exact command the `secrets` CI job runs. A non-zero exit is a red job.

Not in the default `pytest` run: the wrapper downloads gitleaks from GitHub, and
AC-M0-01 allows only the package registry. CI runs `uv run pytest tests/secrets`
in the `secrets` job; locally set GITLEAKS to a gitleaks 8.30.1 binary or let the
wrapper download it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCAN = ROOT / "scripts" / "ci" / "gitleaks.sh"
ZERO = "0" * 40
PLAN = "docs/implementation-plan-v1.md"


def header(kind: str = "") -> str:
    """A PEM private key header, built at run time so no file here spells one."""
    return "-----BEGIN " + (kind + " " if kind else "") + "PRIVATE " + "KEY-----"


def openssl_key(*args: str) -> str:
    return subprocess.run(["openssl", *args], check=True, capture_output=True, text=True).stdout


def public_key() -> str:
    private = openssl_key("genpkey", "-algorithm", "ed25519")
    return subprocess.run(
        ["openssl", "pkey", "-pubout"], input=private, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(scope="module")
def keys() -> dict[str, str]:
    """Freshly generated private keys in the PEM flavours CI must reject."""
    return {
        "pkcs8": openssl_key("genpkey", "-algorithm", "ed25519"),
        "rsa": openssl_key("genrsa", "-traditional", "1024"),
        "ec": openssl_key("ecparam", "-genkey", "-name", "prime256v1", "-noout"),
        "encrypted": openssl_key("genpkey", "-algorithm", "ed25519", "-aes256", "-pass", "pass:x"),
    }


def body(pem: str) -> str:
    """The base64 key material of a PEM block, on one line."""
    return "".join(line for line in pem.splitlines() if not line.startswith("-----"))


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


def commit(repo: Path, files: dict[str, str | bytes], message: str, remove: tuple[str, ...] = ()) -> str:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        git(repo, "add", "-f", rel)  # -f: .gitignore must not be the only defence
    for rel in remove:
        git(repo, "rm", "-q", rel)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def scan(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCAN), *args], cwd=repo, capture_output=True, text=True, check=False)


def assert_red(res: subprocess.CompletedProcess[str], path: str, *secrets: str) -> None:
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert f"File:        {path}\n" in out, out
    for s in secrets:
        assert s not in out, "the scan must not print secret material"


def assert_red_everywhere(repo: Path, base: str, path: str, *secrets: str) -> None:
    """Both CI steps fail: the tree scan and the scan of every commit of the PR."""
    assert_red(scan(repo, "tree"), path, *secrets)
    assert_red(scan(repo, "range", f"{base}..HEAD"), path, *secrets)


NAMES = [
    "server.pem",
    "deploy/tls/cert.pem",
    ".env",
    "approval/.env",
    ".env.local",
    ".env.production",
    "keys/service.key",
    "client.p12",
    "client.pfx",
    "id_ed25519",
    "id_rsa",
    # any case (M0 regressions)
    "KEY.PEM",
    "server.Pem",
    ".ENV",
    "app/.Env.Local",
    "Server.Key",
    "Client.P12",
    "Client.PFX",
    # paths the default gitleaks allowlist skips
    "node_modules/pkg/server.pem",
    "venv/lib/.env",
    "docs/key.pem.png",
]


@pytest.mark.parametrize("path", NAMES)
def test_forbidden_file_names_fail(repo: Path, path: str) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {path: "placeholder\n"}, "add file")
    assert_red_everywhere(repo, base, path)


@pytest.mark.parametrize(
    "kind",
    ["pkcs8", "rsa", "ec", "encrypted"],
)
def test_generated_private_keys_fail(repo: Path, keys: dict[str, str], kind: str) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"config/key.txt": keys[kind]}, "add key")
    assert_red_everywhere(repo, base, "config/key.txt", body(keys[kind]))


def content_cases(keys: dict[str, str]) -> dict[str, tuple[str, str | bytes]]:
    k = body(keys["pkcs8"])
    h = header()
    return {
        "bare-header-line": ("notes.txt", h + "\n"),  # the AC-M0-04 string on its own
        "inline-after-KEY=": ("app/.config", 'KEY="' + h + k + '"\n'),
        "after-PRIVATE_KEY=": ("app/settings.ini", "PRIVATE_KEY=" + h + "\n"),
        "json-escaped": ("deploy/creds.json", '{"key": "' + h + "\\n" + k + '"}\n'),
        "yaml-block": ("deploy/values.yaml", "tls:\n  key: |\n    " + h + "\n    " + k + "\n"),
        "yaml-inline": ("deploy/values2.yaml", "tls:\n  key: '" + h + k + "'\n"),
        "python-string": ("app/settings.py", "k = '" + h + "'\n"),
        "markdown-prose": ("README.md", "Paste `" + h + "` here\n"),
        "lower-case": ("a.txt", h.lower() + "\n"),
        "no-trailing-dashes-no-eol": ("b.txt", "x=" + h[: -len("-----")]),
        "openssh": ("c.txt", header("OPENSSH") + "\n" + k + "\n"),
        "pgp": ("d.txt", header("PGP")[: -len("-----")] + " BLOCK-----\n"),
        "binary": ("data/blob.bin", b"\x00\x01" + h.encode() + b"\x00"),
        "binary-dat": ("data/blob.dat", b"\x00\x01" + h.encode() + b"\x00"),
        # paths the default gitleaks allowlist skips
        "pdf": ("docs/scan.pdf", "%PDF-1.4\n" + h + "\n" + k + "\n"),
        "png": ("img/logo.png", b"\x89PNG\r\n\x1a\n" + h.encode() + b"\n"),
        "node_modules": ("node_modules/pkg/key.txt", h + "\n" + k + "\n"),
        "venv-lib": ("venv/lib/site.txt", h + "\n"),
        "named-gitleaks.toml": ("conf/my.gitleaks.toml", 'k = "' + h + '"\n'),
        "utf-16-le": ("win/key.txt", ("KEY=" + h + "\n").encode("utf-16-le")),
        "utf-16-be": ("win/key-be.txt", ("KEY=" + h + "\n").encode("utf-16-be")),
        "utf-16-bom": ("win/key-bom.txt", ("KEY=" + h + "\n").encode("utf-16")),
        "gitleaks-allow-comment": ("e.py", "k = '" + h + k + "'  # gitleaks:allow\n"),
    }


CONTENT_IDS = list(content_cases({"pkcs8": ""}))


@pytest.mark.parametrize("case", CONTENT_IDS)
def test_private_key_header_anywhere_fails(repo: Path, keys: dict[str, str], case: str) -> None:
    path, content = content_cases(keys)[case]
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {path: content}, "add key")
    assert_red_everywhere(repo, base, path, body(keys["pkcs8"]))


def test_key_added_and_removed_within_the_pr_fails(repo: Path, keys: dict[str, str]) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"secrets.pem": "x\n", "cfg/app.txt": keys["rsa"]}, "oops")
    head = commit(repo, {}, "remove", remove=("secrets.pem", "cfg/app.txt"))
    assert scan(repo, "tree").returncode == 0  # the final tree is clean ...
    res = scan(repo, "range", f"{base}..{head}")  # ... the commits of the PR are not
    assert_red(res, "secrets.pem", body(keys["rsa"]))
    assert_red(res, "cfg/app.txt")


def test_new_branch_range_from_zero_sha_scans_everything(repo: Path) -> None:
    commit(repo, {"a/.env": "X=1\n"}, "oops")
    head = commit(repo, {}, "remove", remove=("a/.env",))
    assert_red(scan(repo, "range", f"{ZERO}..{head}"), "a/.env")


def test_key_in_a_merge_commit_fails(repo: Path, keys: dict[str, str]) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"side.txt": "a\n"}, "side")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"main.txt": "b\n"}, "main")
    git(repo, "merge", "-q", "--no-commit", "side")
    (repo / "merged.txt").write_text(keys["ec"], encoding="utf-8")
    git(repo, "add", "merged.txt")
    git(repo, "commit", "-q", "-m", "merge with a key in the merge commit only")
    assert_red(scan(repo, "range", f"{base}..HEAD"), "merged.txt", body(keys["ec"]))


PLAN_ROW = (
    "| AC-M0-04 | CI отклоняет коммит с файлом, совпадающим с шаблонами секретов "
    "(`*.pem`, `.env`, строка `" + header() + "`) |"
)


def test_the_plan_quote_passes(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {PLAN: "# Plan\n\n| ID | Критерий |\n| --- | --- |\n" + PLAN_ROW + "\nmore\n"}, "plan")
    assert scan(repo, "tree").returncode == 0
    assert scan(repo, "range", f"{base}..HEAD").returncode == 0


def test_the_quote_in_this_repository_is_the_plan_row() -> None:
    assert PLAN_ROW in (ROOT / PLAN).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "text",
    [
        PLAN_ROW + "\n" + header() + "\n{k}\n-----END PRIVATE KEY-----\n",  # key right after the quote
        PLAN_ROW + " {k}\n",  # key material on the quote's line
        PLAN_ROW + "`" + header() + "`\n",  # a second header on the same line
        PLAN_ROW + "\n" + PLAN_ROW.replace("AC-M0-04", "AC-M9-99") + "\n",  # the quote in another row
        PLAN_ROW + "\nKEY=" + header("RSA") + "{k}\n",
        header() + "\n{k}\n",  # a key in the plan without the quote
    ],
    ids=["key-after-quote", "key-on-quote-line", "second-header-on-line", "other-row", "inline-key", "no-quote"],
)
def test_key_next_to_the_quote_in_the_plan_fails(repo: Path, keys: dict[str, str], text: str) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {PLAN: text.replace("{k}", body(keys["pkcs8"]))}, "plan with key")
    assert_red_everywhere(repo, base, PLAN, body(keys["pkcs8"]))


@pytest.mark.parametrize(
    "other", ["docs/implementation-plan-v2.md", "docs/sub/implementation-plan-v1.md", "implementation-plan-v1.md"]
)
def test_the_quote_at_any_other_path_fails(repo: Path, other: str) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {other: PLAN_ROW + "\n"}, "copy of the plan")
    assert_red_everywhere(repo, base, other)


def test_clean_files_pass(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(
        repo,
        {
            ".env.example": "JWT_SECRET_KEY=__GENERATE__\n",
            "app/.env.example": "X=\n",
            "docs/plan.md": "Reject files containing a PEM private key header.\n",
            "src/app.py": "PATTERN = '-----BEGIN ' + 'PRIVATE KEY-----'\n",
            "docs/keys.md": public_key(),
            "certs/ca.crt": "-----BEGIN CERTIFICATE-----\n",
            "src/keyboard.py": "x = 1\n",
            "docs/env.md": "environment\n",
            "src/env.py": "x = 2\n",
        },
        "clean",
    )
    res = scan(repo, "tree")
    assert res.returncode == 0, res.stdout + res.stderr
    res = scan(repo, "range", f"{base}..HEAD")
    assert res.returncode == 0, res.stdout + res.stderr


def test_bad_revision_is_an_error_not_a_pass(repo: Path) -> None:
    assert scan(repo, "range", "deadbeef..HEAD").returncode == 2
    assert scan(repo, "range", "HEAD..deadbeef").returncode == 2
    assert scan(repo, "range", "HEAD").returncode == 2
    assert scan(repo, "tree", "no-such-rev").returncode != 0
    assert scan(repo, "nonsense").returncode == 2


def test_this_repository_is_clean() -> None:
    res = scan(ROOT, "tree")
    assert res.returncode == 0, res.stdout + res.stderr
    res = scan(ROOT, "range", f"{ZERO}..HEAD")
    assert res.returncode == 0, res.stdout + res.stderr
