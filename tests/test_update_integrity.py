"""CANON-3 — the deployed Git commit must equal a checksummed SOURCE_COMMIT.

The updater used to verify a release tarball's checksum and then deploy an
INDEPENDENT `git checkout FETCH_HEAD`, so the checksum never proved what git
actually deployed. `resolve_verified_update_commit` now downloads the
artifact, SOURCE_COMMIT and SHA256SUMS, verifies both checksums, validates
SOURCE_COMMIT's grammar, resolves the fetched tag to its commit, and requires
equality — aborting BEFORE any checkout/build/migration/restart on mismatch.

Deterministic: local temporary Git repositories and file:// "downloads".
No GitHub, no root, no Docker, no networking.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "scripts" / "centralpay"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, marker: str) -> str:
    (repo / "file.txt").write_text(marker)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", marker, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def sandbox(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    install = tmp_path / "install"
    config = tmp_path / "config"
    releases = tmp_path / "releases"
    config.mkdir()
    releases.mkdir()
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    commit_a = _commit(work, "A")
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    # Point origin's default branch at main so the install clone checks it out
    # (git init --bare may default HEAD to a different branch name).
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
    git("clone", "-q", str(origin), str(install), cwd=tmp_path)
    return {
        "origin": origin,
        "work": work,
        "install": install,
        "config": config,
        "releases": releases,
        "commit_a": commit_a,
    }


def tag(sandbox, name: str, commit: str, *, annotated: bool = False, force: bool = False) -> None:
    work = sandbox["work"]
    args = ["tag"]
    if force:
        args.append("-f")
    if annotated:
        args += ["-a", name, "-m", name, commit]
    else:
        args += [name, commit]
    git(*args, cwd=work)
    push = ["push", "-q", "origin", f"refs/tags/{name}"]
    if force:
        push.insert(2, "-f")
    git(*push, cwd=work)


def build_assets(
    sandbox,
    ref: str,
    *,
    source_content: str,
    tamper: str | None = None,
    omit_source: bool = False,
) -> None:
    """Create release assets for `ref` under releases/<ref>/ served via file://."""
    d = sandbox["releases"] / ref
    d.mkdir(parents=True, exist_ok=True)
    artifact_name = f"centralpay-bridge-{ref[1:] if ref.startswith('v') else ref}.tar.gz"
    artifact = d / artifact_name
    artifact.write_bytes(b"dummy source tarball for " + ref.encode())
    sums: list[str] = []

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    artifact_hash = sha(artifact)
    if tamper == "artifact_hash":
        artifact_hash = "0" * 64
    sums.append(f"{artifact_hash}  {artifact_name}")

    if not omit_source:
        source = d / "SOURCE_COMMIT"
        source.write_text(source_content)
        source_hash = sha(source)
        if tamper == "source_hash":
            source_hash = "f" * 64
        sums.append(f"{source_hash}  SOURCE_COMMIT")

    # An SBOM line the updater intentionally does not download (filtered out).
    sums.append(f"{'a' * 64}  sbom-centralpay-bridge.spdx.json")
    (d / "SHA256SUMS").write_text("\n".join(sums) + "\n")


def run_resolve(sandbox, ref: str, *, env_lines: str = "") -> subprocess.CompletedProcess[str]:
    (sandbox["config"] / "centralpay.env").write_text(env_lines)
    env = {
        **os.environ,
        **_GIT_ENV,
        "CENTRALPAY_CLI_SOURCE_ONLY": "1",
        "CENTRALPAY_INSTALL_DIR": str(sandbox["install"]),
        "CENTRALPAY_CONFIG_DIR": str(sandbox["config"]),
        "CENTRALPAY_RELEASE_BASE_URL": f"file://{sandbox['releases']}",
    }
    return subprocess.run(
        ["bash", "-c", 'source "$1"; resolve_verified_update_commit "$2"', "_", str(CLI), ref],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def installed_head(sandbox) -> str:
    return git("rev-parse", "HEAD", cwd=sandbox["install"])


# --- is_release_tag grammar --------------------------------------------------


@pytest.mark.parametrize("ref", ["v1.2.3", "v1.2.3-rc1", "v0.6.0-rc1", "v10.20.30"])
def test_is_release_tag_accepts_supported_grammar(ref):
    r = subprocess.run(
        ["bash", "-c", 'source "$1"; is_release_tag "$2"', "_", str(CLI), ref],
        env={**os.environ, "CENTRALPAY_CLI_SOURCE_ONLY": "1"},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, ref


@pytest.mark.parametrize(
    "ref", ["v1.2.3evil", "v1.2.3/other", "v1.2.3-rc", "main", "1.2.3", "v1.2", "v1.2.3-rcx"]
)
def test_is_release_tag_rejects_everything_else(ref):
    r = subprocess.run(
        ["bash", "-c", 'source "$1"; is_release_tag "$2"', "_", str(CLI), ref],
        env={**os.environ, "CENTRALPAY_CLI_SOURCE_ONLY": "1"},
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0, ref


# --- commit binding ----------------------------------------------------------


def test_matching_tag_and_source_commit_resolves_exact_commit(sandbox):
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a)
    build_assets(sandbox, "v1.2.3", source_content=a + "\n")
    result = run_resolve(sandbox, "v1.2.3")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == a


def test_annotated_tag_resolves_and_matches_underlying_commit(sandbox):
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a, annotated=True)
    build_assets(sandbox, "v1.2.3", source_content=a + "\n")
    result = run_resolve(sandbox, "v1.2.3")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == a


def test_moved_tag_aborts_and_leaves_install_unchanged(sandbox):
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a)
    # Assets were produced for commit A...
    build_assets(sandbox, "v1.2.3", source_content=a + "\n")
    # ...then the tag is moved to a new commit B on origin.
    b = _commit(sandbox["work"], "B")
    tag(sandbox, "v1.2.3", b, force=True)
    assert b != a
    head_before = installed_head(sandbox)
    result = run_resolve(sandbox, "v1.2.3")
    assert result.returncode != 0
    assert result.stdout.strip() == ""  # nothing to deploy
    assert "mismatch" in result.stderr.lower()
    assert installed_head(sandbox) == head_before  # no checkout happened


def test_missing_source_commit_fails_closed(sandbox):
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a)
    build_assets(sandbox, "v1.2.3", source_content=a + "\n", omit_source=True)
    result = run_resolve(sandbox, "v1.2.3")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-commit\n",
        "abc123\n",  # too short
        "a" * 40 + "\n" + "b" * 40 + "\n",  # two lines
        "A" * 40 + "\n",  # uppercase hex not allowed
    ],
)
def test_malformed_source_commit_fails_closed(sandbox, bad):
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a)
    build_assets(sandbox, "v1.2.3", source_content=bad)
    result = run_resolve(sandbox, "v1.2.3")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_source_commit_checksum_mismatch_fails_closed(sandbox):
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a)
    build_assets(sandbox, "v1.2.3", source_content=a + "\n", tamper="source_hash")
    result = run_resolve(sandbox, "v1.2.3")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_artifact_checksum_mismatch_fails_closed(sandbox):
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a)
    build_assets(sandbox, "v1.2.3", source_content=a + "\n", tamper="artifact_hash")
    result = run_resolve(sandbox, "v1.2.3")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_non_release_ref_is_development_mode(sandbox):
    """A branch ref stays explicit development/unverified mode: it resolves
    the fetched commit and warns, with no SOURCE_COMMIT binding."""
    result = run_resolve(sandbox, "main")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == sandbox["commit_a"]
    assert "DEVELOPMENT MODE" in result.stderr


def test_allow_unverified_escape_hatch_when_assets_absent(sandbox):
    """With the explicit root-operator opt-in, a release tag with no
    downloadable assets deploys the fetched commit and warns unmistakably,
    without falsely claiming checksum or SOURCE_COMMIT verification."""
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a)
    # No build_assets → nothing to download.
    result = run_resolve(
        sandbox, "v1.2.3", env_lines="CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == a
    assert "UNVERIFIED" in result.stderr
    assert "NO checksum" in result.stderr


def test_missing_assets_without_optin_fails_closed(sandbox):
    a = sandbox["commit_a"]
    tag(sandbox, "v1.2.3", a)
    result = run_resolve(sandbox, "v1.2.3")  # no assets, no opt-in
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_update_command_aborts_before_side_effects_on_mismatch():
    """Static guarantee: cmd_update resolves and binds the commit BEFORE the
    backup, checkout, build, migration, restart, and version-history steps."""
    text = CLI.read_text()
    body = text[text.index("cmd_update() {"):text.index("cmd_rollback() {")]
    # Anchor on the actual command invocation (the assignment), not the
    # explanatory comment that names the later steps.
    resolve_i = body.index("target_commit=$(resolve_verified_update_commit")
    for later in (
        "scripts/backup.sh",
        'checkout -q "$target_commit"',
        "compose build",
        "compose up",
        "record_version_history",
    ):
        assert resolve_i < body.index(later), later
    # The checkout deploys the verified commit, never a raw FETCH_HEAD.
    assert 'checkout -q "$target_commit"' in body
    assert "checkout -q FETCH_HEAD" not in body


# --- strict SHA256SUMS manifest parsing + exact SOURCE_COMMIT byte grammar ---
#
# These exercise verify_manifest_and_extract_commit directly with byte-exact
# inputs (no git, no downloads): it must accept ONLY a manifest with exactly
# one entry each for the exact artifact filename and the exact filename
# "SOURCE_COMMIT" (each line `[0-9a-f]{64}  <name>`), and a SOURCE_COMMIT file
# whose raw bytes full-match `[0-9a-f]{40}` or `[0-9a-f]{40}\n`.

ARTIFACT_NAME = "centralpay-bridge-1.2.3.tar.gz"
ARTIFACT_BYTES = b"dummy source tarball"
COMMIT40 = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"  # 40 lowercase hex


def _sha_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_verify(
    tmp_path: Path,
    *,
    manifest: bytes,
    source: bytes,
    artifact_name: str = ARTIFACT_NAME,
    artifact: bytes = ARTIFACT_BYTES,
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "SHA256SUMS").write_bytes(manifest)
    (tmp_path / artifact_name).write_bytes(artifact)
    (tmp_path / "SOURCE_COMMIT").write_bytes(source)
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; verify_manifest_and_extract_commit "$2" "$3" "$4" "$5"',
            "_",
            str(CLI),
            str(tmp_path / "SHA256SUMS"),
            artifact_name,
            str(tmp_path / artifact_name),
            str(tmp_path / "SOURCE_COMMIT"),
        ],
        env={**os.environ, "CENTRALPAY_CLI_SOURCE_ONLY": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _line(name: str, data: bytes) -> str:
    return f"{_sha_hex(data)}  {name}"


def _good_manifest(source: bytes, *, artifact: bytes = ARTIFACT_BYTES) -> bytes:
    return (
        _line(ARTIFACT_NAME, artifact)
        + "\n"
        + _line("SOURCE_COMMIT", source)
        + "\n"
        + f"{'a' * 64}  sbom-centralpay-bridge.spdx.json\n"
    ).encode()


# --- acceptance --------------------------------------------------------------


@pytest.mark.parametrize("source", [COMMIT40.encode(), (COMMIT40 + "\n").encode()])
def test_verify_accepts_exact_40_byte_commit_with_optional_newline(tmp_path, source):
    result = run_verify(tmp_path, manifest=_good_manifest(source), source=source)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == COMMIT40


# --- manifest exactness (rejections) -----------------------------------------


def _manifest_reject_cases() -> dict[str, bytes]:
    src = (COMMIT40 + "\n").encode()
    src_line = _line("SOURCE_COMMIT", src)
    art_line = _line(ARTIFACT_NAME, ARTIFACT_BYTES)
    good_hash = _sha_hex(src)
    return {
        "two_source_no_artifact": f"{src_line}\n{src_line}\n".encode(),
        "two_artifact_no_source": f"{art_line}\n{art_line}\n".encode(),
        "duplicate_source_plus_artifact": f"{art_line}\n{src_line}\n{src_line}\n".encode(),
        "duplicate_artifact_plus_source": f"{art_line}\n{art_line}\n{src_line}\n".encode(),
        "dot_slash_source": f"{art_line}\n{good_hash}  ./SOURCE_COMMIT\n".encode(),
        "path_source": f"{art_line}\n{good_hash}  sub/SOURCE_COMMIT\n".encode(),
        "source_dot_old": f"{art_line}\n{good_hash}  SOURCE_COMMIT.old\n".encode(),
        "similarly_named_artifact": (
            f"{good_hash}  centralpay-bridge-1.2.3.tar.gz.bak\n{src_line}\n".encode()
        ),
        "missing_artifact_entry": f"{src_line}\n".encode(),
        "missing_source_entry": f"{art_line}\n".encode(),
        "uppercase_source_hash": f"{art_line}\n{good_hash.upper()}  SOURCE_COMMIT\n".encode(),
        "short_source_hash": f"{art_line}\n{good_hash[:63]}  SOURCE_COMMIT\n".encode(),
        "binary_marker_source": f"{art_line}\n{good_hash} *SOURCE_COMMIT\n".encode(),
        "single_space_source": f"{art_line}\n{good_hash} SOURCE_COMMIT\n".encode(),
        "trailing_space_after_name": f"{art_line}\n{src_line} \n".encode(),
        "leading_junk_source": f"{art_line}\n x{src_line}\n".encode(),
    }


@pytest.mark.parametrize("name,manifest", list(_manifest_reject_cases().items()))
def test_verify_rejects_inexact_manifest(tmp_path, name, manifest):
    source = (COMMIT40 + "\n").encode()
    result = run_verify(tmp_path, manifest=manifest, source=source)
    assert result.returncode != 0, name
    assert result.stdout.strip() == "", name


# --- SOURCE_COMMIT byte grammar (rejections) ---------------------------------


def _byte_reject_cases() -> dict[str, bytes]:
    return {
        "embedded_newline": (COMMIT40[:20] + "\n" + COMMIT40[20:] + "\n").encode(),
        "two_trailing_newlines": (COMMIT40 + "\n\n").encode(),
        "crlf": (COMMIT40 + "\r\n").encode(),
        "leading_space": (" " + COMMIT40).encode(),
        "trailing_space": (COMMIT40 + " ").encode(),
        "trailing_space_before_newline": (COMMIT40 + " \n").encode(),
        "tab": (COMMIT40 + "\t").encode(),
        "nul": (COMMIT40 + "\x00").encode(),
        "uppercase_hex": ("A" * 40).encode(),
        "two_hashes": (COMMIT40 + COMMIT40).encode(),
        "too_short": COMMIT40[:39].encode(),
        "too_long_non_newline": (COMMIT40 + "a").encode(),
        "empty": b"",
    }


@pytest.mark.parametrize("name,source", list(_byte_reject_cases().items()))
def test_verify_rejects_malformed_source_commit_bytes(tmp_path, name, source):
    # A valid manifest (hashes computed over the actual malformed bytes) so the
    # ONLY failing gate is the byte grammar, which is checked before checksums.
    result = run_verify(tmp_path, manifest=_good_manifest(source), source=source)
    assert result.returncode != 0, name
    assert result.stdout.strip() == "", name


# --- checksum enforcement against the individually selected expected hashes ---


def test_verify_rejects_artifact_hash_mismatch(tmp_path):
    source = (COMMIT40 + "\n").encode()
    manifest = _good_manifest(source, artifact=b"the-manifest-hash-covers-this")
    result = run_verify(
        tmp_path, manifest=manifest, source=source, artifact=b"but-the-file-is-different"
    )
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_verify_rejects_source_hash_mismatch(tmp_path):
    source = (COMMIT40 + "\n").encode()
    # Manifest's SOURCE_COMMIT hash covers a DIFFERENT valid commit than the
    # file, so the bytes are well-formed but the checksum must fail.
    other = ("b" * 40 + "\n").encode()
    manifest = (
        _line(ARTIFACT_NAME, ARTIFACT_BYTES)
        + "\n"
        + _line("SOURCE_COMMIT", other)
        + "\n"
    ).encode()
    result = run_verify(tmp_path, manifest=manifest, source=source)
    assert result.returncode != 0
    assert result.stdout.strip() == ""
