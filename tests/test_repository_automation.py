"""Exercise scope selection and draft-only release boundaries with no network."""

import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest

from tests.test_release_preflight import VERSION, commit, git

pytest_plugins = ("tests.test_release_preflight",)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scope(monkeypatch: pytest.MonkeyPatch):
    """Load the real scope classifier without fake Home Assistant imports."""
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    return importlib.import_module("check_ci_scope")


@pytest.mark.parametrize("paths,full", [
    (["README.md", "docs/setup.md", "tests/runtime/README.md"], False),
    (["README.md", "custom_components/ezviz_cloud/camera.py"], True),
    ([".github/workflows/ci.yaml"], True),
    (["docs/example.py"], True),
    (["unknown-file"], True),
    (["custom_components/ezviz_cloud/camera.py", "docs/camera.md"], True),
])
def test_scope_fails_closed_for_non_markdown(scope, paths: list[str], full: bool) -> None:
    assert scope.needs_full(paths) is full


def test_actual_rename_and_git_failure_cannot_skip_full_checks(scope, candidate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Actual git diff must include the deleted source side of a rename."""
    before = git(candidate, "rev-parse", "HEAD")
    git(candidate, "mv", "custom_components/ezviz_cloud/__init__.py", "code.md")
    after = commit(candidate)
    monkeypatch.chdir(candidate)
    assert scope.needs_full(scope.changed_paths(before, after))
    with pytest.raises(subprocess.CalledProcessError):
        scope.changed_paths("0" * 40, after)
    with pytest.raises(ValueError, match="full Git SHAs"):
        scope.changed_paths("--help", after)


@pytest.fixture
def draft(candidate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real candidate Git metadata plus a fake gh executable recording all calls."""
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    module = importlib.import_module("prepare_draft_release")
    monkeypatch.setattr(module, "ROOT", candidate)
    (candidate / "CHANGELOG.md").write_text(f"# Changes\n\n## {VERSION}\n\nReviewed upgrade notes.\n")
    sha = commit(candidate)
    git(candidate, "update-ref", "refs/remotes/origin/main", sha)
    # Keep fake transport/evidence outside the candidate so it remains clean.
    transport = tmp_path.parent / (tmp_path.name + "-transport")
    transport.mkdir()
    audit = transport / "calls.jsonl"
    gh = transport / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args=sys.argv[1:]
payload=json.load(sys.stdin) if '--input' in args else None
with Path(os.environ['AUDIT']).open('a') as stream:
    stream.write(json.dumps({'args':args,'payload':payload})+'\\n')
if '--paginate' in args:
    if os.environ.get('FAIL_LIST'):
        sys.exit(1)
    print(os.environ.get('EXISTING_RELEASE', ''))
elif args[1].endswith('/generate-notes'):
    print(json.dumps({'body':'Categorized changes'}))
else:
    assert payload['draft'] is True
    print(json.dumps({'id':123, 'draft':True}))
''')
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(transport) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("AUDIT", str(audit))
    monkeypatch.setenv("GITHUB_REPOSITORY", module.REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.chdir(candidate)
    monkeypatch.setattr(sys, "argv", ["prepare_draft_release.py", "--commit", sha,
        "--tag", VERSION, "--prerelease", "true", "--output", str(transport / "report.json")])
    return module, candidate, audit


def test_release_creation_is_draft_only_without_creating_tag(draft) -> None:
    module, repo, audit = draft
    module.main()
    calls = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(calls) == 3
    payload = calls[-1]["payload"]
    assert payload["draft"] is payload["prerelease"] is True
    assert payload["target_commitish"] == git(repo, "rev-parse", "HEAD")
    assert "Reviewed upgrade notes." in payload["body"]
    assert "Categorized changes" in payload["body"]
    assert "unverified" in payload["body"]
    assert git(repo, "tag", "--list") == ""
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("kind", ["tag", "release", "unmerged", "wrong-repo", "transport-error"])
def test_draft_rejects_reused_or_untrusted_identity_before_mutation(draft, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    module, repo, audit = draft
    if kind == "tag":
        git(repo, "tag", VERSION)
    elif kind == "release":
        monkeypatch.setenv("EXISTING_RELEASE", VERSION)
    elif kind == "unmerged":
        git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD^"))
    elif kind == "wrong-repo":
        monkeypatch.setenv("GITHUB_REPOSITORY", "other/fork")
    else:
        monkeypatch.setenv("FAIL_LIST", "1")
    with pytest.raises((ValueError, subprocess.CalledProcessError, FileNotFoundError)):
        module.main()
    if audit.exists():
        assert all(json.loads(line)["payload"] is None for line in audit.read_text().splitlines())


@pytest.mark.parametrize("text", ["## Unreleased\nNot reviewed", "## 9.8.7.6\n\n## Another\nWrong section"])
def test_missing_or_empty_versioned_notes_refuse_release(draft, text: str) -> None:
    module, _, _ = draft
    with pytest.raises(ValueError, match="reviewed"):
        module.version_notes(text, VERSION)


@pytest.mark.parametrize("full,failed,result", [
    (True, None, "success"),
    (False, None, "skipped"),
    (True, "native-ha", "failure"),
    (True, "hassfest", "cancelled"),
    (True, "core-policy", "skipped"),
    (False, "repository", "failure"),
    (False, "scope", "cancelled"),
    (False, "lint-test", "success"),
])
def test_actual_merge_gate_refuses_failures_and_unexpected_skips(full: bool, failed: str | None, result: str) -> None:
    """Execute the workflow's own gate code, not a test-side reimplementation."""
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text()
    code = workflow.split("python3 - <<'PYCODE'\n", 1)[1].split("          PYCODE", 1)[0]
    code = "\n".join(line.removeprefix("          ") for line in code.splitlines())
    results: dict[str, dict[str, Any]] = {name: {"result": "success" if full else "skipped"}
               for name in ("lint-test", "real-ha", "native-ha", "core-policy", "hacs", "hassfest")}
    results.update({"scope": {"result": "success", "outputs": {"full": str(full).lower()}},
                    "repository": {"result": "success"}})
    if failed:
        results[failed]["result"] = result
    process = subprocess.run([sys.executable, "-c", code],
        env={**os.environ, "RESULTS": json.dumps(results)}, capture_output=True, text=True, check=False)
    assert (process.returncode == 0) is (failed is None), process.stderr


def test_reusable_validators_do_not_cancel_each_other_or_the_caller() -> None:
    """Called workflows inherit github.workflow; only explicit prefixes separate them."""
    groups = []
    for name in ("ci.yaml", "HACS.yaml", "hassfest.yaml", "core-policy.yaml"):
        workflow = (ROOT / ".github/workflows" / name).read_text()
        group = re.search(r"^  group: (.+)$", workflow, re.M)
        assert group is not None
        rendered = group[1].replace("${{ github.workflow }}", "CI").replace(
            "${{ github.ref }}", "refs/pull/1/merge"
        ).replace("${{ inputs.checkout_ref }}", "a" * 40)
        groups.append(rendered)
    assert len(groups) == len(set(groups)), groups
