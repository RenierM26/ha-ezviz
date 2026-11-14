"""Read-only release preflight and committed-source hash report; never publishes."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = "custom_components/ezviz_cloud"


def git(repo: Path, *args: str) -> bytes:
    """Read Git metadata/blobs from the selected checkout without network access."""
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)



def release_files(repo: Path, commit: str) -> dict:
    """Hash regular tracked component files, never uncommitted working copies."""
    files = {}
    for row in git(repo, "ls-tree", "-rz", "--full-tree", commit, "--", COMPONENT).split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        path = raw_path.decode()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("Release component must contain regular tracked files, not symlinks/submodules")
        content = git(repo, "cat-file", "blob", blob)
        files[path] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "mode": mode}
    if not files:
        raise ValueError("No committed component files found")
    return files


def preflight(repo: Path, commit: str, tag: str) -> dict:
    """Require a clean exact candidate and keep release identity consistent."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("--commit must be the full lowercase 40-character commit SHA")
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", tag):
        raise ValueError("--tag must be a bare version (no v prefix, slash or whitespace)")
    resolved = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved != commit or git(repo, "rev-parse", "HEAD").decode().strip() != commit:
        raise ValueError("Check out the exact candidate commit before qualifying it")
    if git(repo, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Worktree is dirty; commit or set aside changes before qualification")

    def read(path: str) -> bytes:
        return git(repo, "show", f"{commit}:{path}")

    manifest = json.loads(read(f"{COMPONENT}/manifest.json"))
    if manifest["version"] != tag:
        raise ValueError("Release tag must exactly match manifest.version (without a v prefix)")
    found = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
        check=False,
        capture_output=True,
    )
    if found.returncode not in (0, 1):
        raise ValueError("Cannot inspect local release tags")
    if found.returncode == 0:
        target = git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip()
        if target != commit:
            raise ValueError("Version tag already names another commit; choose a new version, never move the tag")
    sdk_lines = [
        line.strip() for line in read("requirements/sdk.txt").decode().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(sdk_lines) != 1 or not re.fullmatch(r"pyezvizapi==[0-9][0-9A-Za-z.+-]*", sdk_lines[0]):
        raise ValueError("SDK qualification profile must contain one exact published-version pin")
    sdk_version = sdk_lines[0].split("==", 1)[1]
    if f"pyezvizapi>={sdk_version}" not in manifest["requirements"]:
        raise ValueError("Manifest SDK minimum and qualified SDK profile differ")
    hacs = json.loads(read("hacs.json"))
    if manifest["domain"] != "ezviz_cloud" or not hacs.get("homeassistant"):
        raise ValueError("Component domain and HACS minimum Home Assistant version are required")
    files = release_files(repo, commit)
    return {
        "schema_version": 1,
        "commit": commit,
        "tag": tag,
        "existing_local_tag_matches": found.returncode == 0,
        "component_tree": git(repo, "rev-parse", f"{commit}:{COMPONENT}").decode().strip(),
        "manifest_version": manifest["version"],
        "minimum_home_assistant": hacs["homeassistant"],
        "qualified_sdk": sdk_version,
        "git_version": git(repo, "--version").decode().strip(),
        "files": files,
        "limits": "Local source/metadata preflight only; not remote-tag, CI, live-device or publication approval",
    }


def main() -> None:
    """Validate without mutating tags, versions, credentials or release records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = preflight(args.repo.resolve(), args.commit, args.tag)
    except (ValueError, KeyError) as error:
        parser.exit(1, f"Release preflight failed: {error}\n")
    except (OSError, subprocess.CalledProcessError):
        parser.exit(1, "Release preflight failed: Git or committed metadata could not be read\n")
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded)


if __name__ == "__main__":
    main()
