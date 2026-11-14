"""Create isolated fast-check and native HA development environments."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Install public pinned profiles; never launch HA or touch a user's config."""
    if sys.version_info < (3, 14, 2):
        raise SystemExit("Development requires Python 3.14.2 or newer")
    for name, profile in ((".venv-tools", "lint.txt"), (".venv", "dev.txt")):
        directory = ROOT / name
        subprocess.run([sys.executable, "-m", "venv", str(directory)], check=True)
        python = directory / "bin/python"
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements" / profile)],
            cwd=ROOT,
            check=True,
        )
        subprocess.run([str(python), "-m", "pip", "check"], cwd=ROOT, check=True)
    print(
        "Ready: .venv-tools for lint/standalone; .venv for native tests and manual HA development"
    )


if __name__ == "__main__":
    main()
