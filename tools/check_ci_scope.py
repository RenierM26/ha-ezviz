"""Fail closed to full qualification except for exclusively Markdown changes."""

import argparse
from pathlib import PurePosixPath
import re
import subprocess


def needs_full(paths: list[str]) -> bool:
    """Unknown, executable and configuration paths require the complete matrix."""
    return any(PurePosixPath(path).suffix.lower() != ".md" for path in paths)


def changed_paths(base: str, head: str) -> list[str]:
    """Include both sides of renames so moving code to Markdown cannot skip CI."""
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        raise ValueError("Scope comparison requires full Git SHAs")
    data = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"]
    )
    return [path.decode() for path in data.split(b"\0") if path]


def main() -> None:
    """Print a GitHub output value; Git failures must fail the scope job."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    print(f"full={str(needs_full(changed_paths(args.base, args.head))).lower()}")


if __name__ == "__main__":
    main()
