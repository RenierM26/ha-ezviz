"""Create only a GitHub draft after qualification; never create a tag or publish."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from check_release import preflight

ROOT = Path(__file__).resolve().parents[1]
REPO = "RenierM26/ha-ezviz"


def version_notes(changelog: str, tag: str) -> str:
    """Require reviewed versioned upgrade notes rather than publishing Unreleased."""
    match = re.search(rf"^## {re.escape(tag)}(?:[ \t]+[^\n]*)?\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if match is None or not match[1].strip():
        raise ValueError("CHANGELOG.md must have nonempty reviewed '## VERSION' notes")
    return match[1].strip()


def api(endpoint: str, *, payload: dict | None = None) -> dict:
    """Use gh's authenticated structured transport; do not put notes in shell text."""
    command = ["gh", "api", endpoint]
    if payload is not None:
        command.extend(["--method", "POST", "--input", "-"])
    result = subprocess.run(
        command, input=json.dumps(payload) if payload is not None else None,
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    """Fail before mutation for reused versions, unknown source or missing notes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prerelease", choices=("true", "false"), required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != REPO:
        raise ValueError("Draft creation is restricted to the owning repository")
    report = preflight(ROOT, args.commit, args.tag)
    # Fetch/check the remote main and all tags in the trusted workflow before this command.
    subprocess.run(["git", "merge-base", "--is-ancestor", args.commit, "origin/main"], check=True)
    if report["existing_local_tag_matches"]:
        raise ValueError("Draft preparation requires a new tag; existing tags are verification-only")
    notes = version_notes((ROOT / "CHANGELOG.md").read_text(), args.tag)
    # Refuse to replace any published or draft release. API errors are not absence.
    releases = subprocess.check_output(
        ["gh", "api", "--paginate", f"repos/{REPO}/releases", "--jq", ".[] | .tag_name"], text=True
    ).splitlines()
    if args.tag in releases:
        raise ValueError("A draft or published release already uses this version")
    generated = api(f"repos/{REPO}/releases/generate-notes", payload={
        "tag_name": args.tag, "target_commitish": args.commit,
        "configuration_file_path": ".github/release.yml",
    })
    run_id = os.environ["GITHUB_RUN_ID"]
    if not run_id.isdecimal():
        raise ValueError("Expected a numeric workflow run ID")
    body = (
        f"{notes}\n\n## Qualification\n\n"
        f"- Candidate: `{args.commit}`\n"
        f"- [Qualification run and source report](https://github.com/{REPO}/actions/runs/{run_id})\n"
        f"- HA minimum: {report['minimum_home_assistant']}; qualified SDK: {report['qualified_sdk']}\n"
        "- Offline checks only; live cloud, device actions and browser/media behavior remain unverified.\n"
        "- Draft only: no tag creation, publication, HACS update or deployment is authorized by this run.\n\n"
        f"## Merged changes\n\n{generated['body']}\n"
    )
    # Save evidence before the single mutation. A failed creation does not publish anything.
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    release = api(f"repos/{REPO}/releases", payload={
        "tag_name": args.tag, "target_commitish": args.commit,
        "name": args.tag, "body": body, "draft": True,
        "prerelease": args.prerelease == "true",
    })
    if not release.get("draft"):
        raise RuntimeError("Unexpected non-draft response; inspect GitHub before any retry")
    print(f"Created draft release ID {release['id']} for {args.commit}")


if __name__ == "__main__":
    main()
