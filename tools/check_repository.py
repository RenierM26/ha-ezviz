"""Validate repository YAML, issue forms and label references without HA imports."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def check_forms(github: Path, known: set[str]) -> None:
    """Check required issue fields, IDs and type labels."""
    for path in (github / "ISSUE_TEMPLATE").glob("*.yml"):
        if path.name == "config.yml":
            continue
        form = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        if not all(form.get(key) for key in ("name", "description", "body")):
            raise ValueError(f"Missing issue form fields: {path}")
        if not set(form.get("labels", [])).issubset(known):
            raise ValueError(f"Unknown form label: {path}")
        ids = []
        for field in form["body"]:
            if field["type"] == "markdown":
                continue
            ids.append(field["id"])
            if not field["attributes"].get("label"):
                raise ValueError(f"Missing field label: {path}")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate form field ID: {path}")


def main() -> None:
    """Catch malformed YAML/forms and misspelled labels before merging."""
    github = ROOT / ".github"
    files = sorted([*github.rglob("*.yaml"), *github.rglob("*.yml")])
    for path in files:
        # BaseLoader retains GitHub's YAML-1.2 `on` key instead of treating it as True.
        document = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        if not isinstance(document, dict):
            raise ValueError(f"Expected a mapping: {path}")
    labels = json.loads((github / "labels.json").read_text())
    custom = {item["name"] for item in labels}
    if len(custom) != len(labels):
        raise ValueError("Duplicate custom label")
    known = custom | {
        "bug", "documentation", "duplicate", "enhancement", "good first issue",
        "help wanted", "invalid", "question", "wontfix", "dependencies", "github_actions",
    }
    check_forms(github, known)
    labeler = yaml.load((github / "labeler.yml").read_text(), Loader=yaml.BaseLoader)
    if not set(labeler).issubset(known):
        raise ValueError("Unknown PR labeler label")
    releases = yaml.load((github / "release.yml").read_text(), Loader=yaml.BaseLoader)
    for category in releases["changelog"]["categories"]:
        if not set(category["labels"]).issubset(known | {"*"}):
            raise ValueError("Unknown release-note label")
    print(f"PASS: {len(files)} YAML files, issue forms and label references")


if __name__ == "__main__":
    main()
