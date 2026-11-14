"""Qualify release identity and source reports through actual Git/CLI boundaries."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "9.8.7.6"


def git(repo: Path, *args: str) -> str:
    """Use an isolated local repository; never fetch or push during tests."""
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def commit(repo: Path) -> str:
    """Make the exact candidate explicit, including metadata changes."""
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Synthetic release candidate")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal synthetic metadata with actual Git commit/tag semantics."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    git(tmp_path, "init", "--initial-branch=main")
    metadata = {
        "hacs.json": json.dumps({"homeassistant": "2026.10.4"}),
        "requirements/sdk.txt": "pyezvizapi==2.2.0\n",
        "custom_components/ezviz_cloud/manifest.json": json.dumps({
            "domain": "ezviz_cloud", "version": VERSION,
            "requirements": ["pyezvizapi>=2.2.0"],
        }),
    }
    for name, contents in metadata.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    (tmp_path / "custom_components/ezviz_cloud/__init__.py").write_text('"""Synthetic package."""\n')
    commit(tmp_path)
    return tmp_path


def check(repo: Path, tag: str = VERSION, sha: str | None = None, *extra: str) -> subprocess.CompletedProcess[str]:
    """Execute the actual preflight command rather than duplicating its checks."""
    return subprocess.run(
        [sys.executable, str(ROOT / "tools/check_release.py"), "--repo", str(repo),
         "--commit", sha or git(repo, "rev-parse", "HEAD"), "--tag", tag, *extra],
        env=os.environ.copy(), text=True, capture_output=True, timeout=15, check=False,
    )


def test_clean_candidate_report_is_repeatable_and_hashes_exact_source(candidate: Path) -> None:
    """Reported bytes are committed blobs, not a copied or reconstructed package."""
    before = git(candidate, "status", "--porcelain")
    first, second = check(candidate), check(candidate)
    assert first.returncode == second.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert report["commit"] == git(candidate, "rev-parse", "HEAD")
    assert report["tag"] == report["manifest_version"] == VERSION
    assert report["qualified_sdk"] == "2.2.0"
    for name, details in report["files"].items():
        content = (candidate / name).read_bytes()
        assert details["sha256"] == hashlib.sha256(content).hexdigest()
        assert details["bytes"] == len(content)
    assert git(candidate, "status", "--porcelain") == before
    assert git(candidate, "tag", "--list") == ""


@pytest.mark.parametrize("kind", ["tracked", "untracked"])
def test_dirty_worktree_is_not_qualified(candidate: Path, kind: str) -> None:
    """Passing committed code cannot silently qualify different local test sources."""
    path = candidate / ("hacs.json" if kind == "tracked" else "untracked.txt")
    path.write_text("modified")
    result = check(candidate)
    assert result.returncode == 1 and "Worktree is dirty" in result.stderr


@pytest.mark.parametrize("tag", ["v9.8.7.6", "9.8.7.7", "9.8.7.6/other"])
def test_wrong_version_or_tag_convention_is_rejected(candidate: Path, tag: str) -> None:
    """Release identity must not diverge from the HACS manifest version."""
    result = check(candidate, tag)
    assert result.returncode == 1 and "tag" in result.stderr.lower()


def test_existing_tag_on_different_code_is_never_reused(candidate: Path) -> None:
    """The current unreleased-main/version-reuse gap has an actual Git control."""
    old = git(candidate, "rev-parse", "HEAD")
    git(candidate, "tag", VERSION)
    (candidate / "custom_components/ezviz_cloud/__init__.py").write_text('"""New code, old version."""\n')
    commit(candidate)
    result = check(candidate)
    assert result.returncode == 1 and "never move the tag" in result.stderr
    assert git(candidate, "rev-parse", VERSION) == old


@pytest.mark.parametrize("annotated", [False, True])
def test_existing_matching_lightweight_or_annotated_tag_is_read_only(candidate: Path, annotated: bool) -> None:
    """Post-tag verification accepts the same candidate without moving its tag."""
    git(candidate, "tag", *(["-a", VERSION, "-m", "Synthetic release"] if annotated else [VERSION]))
    result = check(candidate)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["existing_local_tag_matches"] is True


@pytest.mark.parametrize("pin", ["pyezvizapi>=2.2.0", "pyezvizapi==2.3.0", "pyezvizapi==2.2.0\nother==1"])
def test_sdk_profile_drift_is_rejected(candidate: Path, pin: str) -> None:
    """A release cannot silently claim the wrong qualified published SDK minimum."""
    (candidate / "requirements/sdk.txt").write_text(pin + "\n")
    commit(candidate)
    result = check(candidate)
    assert result.returncode == 1 and "SDK" in result.stderr


def test_exact_commit_is_required(candidate: Path) -> None:
    """Abbreviations and prior commits cannot inherit evidence from another HEAD."""
    old = git(candidate, "rev-parse", "HEAD")
    assert check(candidate, sha=old[:8]).returncode == 1
    (candidate / "notes.txt").write_text("new candidate")
    commit(candidate)
    result = check(candidate, sha=old)
    assert result.returncode == 1 and "exact candidate" in result.stderr


def test_symlink_is_not_reported_as_a_shipped_regular_file(candidate: Path) -> None:
    """A source report cannot hide a link to content outside the component."""
    (candidate / "custom_components/ezviz_cloud/link.py").symlink_to("../../outside.py")
    commit(candidate)
    result = check(candidate)
    assert result.returncode == 1 and "regular tracked files" in result.stderr


def test_explicit_report_output_does_not_change_candidate(candidate: Path) -> None:
    """The only optional write is the caller's chosen evidence report."""
    destination = candidate.parent / (candidate.name + "-source-report.json")
    result = check(candidate, VERSION, None, "--output", str(destination))
    assert result.returncode == 0 and result.stdout == "", result.stderr
    assert json.loads(destination.read_text())["tag"] == VERSION
    assert git(candidate, "status", "--porcelain") == ""
