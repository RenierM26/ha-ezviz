"""Shared local, editor and CI check entry points; each test layer is isolated."""

import argparse
from importlib.metadata import version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COVERED_MODULES = ("config_flow", "options_flow", "camera_flow", "flow_validation", "diagnostics")


def run(*args: str, env: dict[str, str] | None = None) -> None:
    """Fail on the first unsuccessful check without changing the interpreter."""
    command = [sys.executable, "-m", *args]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def check_sdk_profile() -> None:
    """Keep qualified SDK, manifest minimum and installed test dependency aligned."""
    lines = [
        line.strip()
        for line in (ROOT / "requirements/sdk.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(lines) != 1 or not lines[0].startswith("pyezvizapi=="):
        raise RuntimeError("The SDK profile must contain one exact published pyezvizapi pin")
    expected = lines[0].removeprefix("pyezvizapi==")
    manifest = json.loads((ROOT / "custom_components/ezviz_cloud/manifest.json").read_text())
    if f"pyezvizapi>={expected}" not in manifest["requirements"]:
        raise RuntimeError("SDK qualification pin differs from the manifest minimum")
    installed = version("pyezvizapi")
    if installed != expected:
        raise RuntimeError(
            f"Use the qualified SDK profile: installed {installed}, expected {expected}"
        )
    print("SDK profile:", expected, "(matches manifest and installed distribution)", flush=True)


def check_default_component_policy() -> None:
    """Prove default Ruff discovery checks component files using Core rules."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-fix",
            "--force-exclude",
            "--output-format=json",
            "--stdin-filename",
            "custom_components/ezviz_cloud/policy_probe.py",
            "-",
        ],
        input="from __future__ import annotations\n",
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    findings = json.loads(result.stdout)
    if result.returncode != 1 or not {"TID251", "D100"}.issubset(
        {item["code"] for item in findings}
    ):
        raise RuntimeError("Default Ruff policy must reject obsolete component annotations")
    print("PASS: default component Ruff discovery enforces Core TID251 and D100", flush=True)


def main() -> None:
    """Run one explicit profile; never mix fake HA fixtures with real HA suites."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("lint", "standalone", "native", "acceptance", "core"))
    parser.add_argument("--core", type=Path)
    parser.add_argument("--runtime-tests", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 14, 2):
        parser.error("Checks require Python 3.14.2 or newer")
    check_sdk_profile()
    if args.profile == "lint":
        run("ruff", "check", ".")
        check_default_component_policy()
        run(
            "ruff",
            "format",
            "--check",
            "conftest.py",
            "custom_components/ezviz_cloud",
            "tests/runtime/core_policy",
        )
        run("mypy", "conftest.py", "custom_components", "tests", "tools")
    elif args.profile == "standalone":
        paths = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("test_*.py"))]
        if not paths:
            parser.error("No standalone tests found; refusing unscoped pytest collection")
        run("pytest", *paths)
    elif args.profile == "native":
        if find_spec("pytest_homeassistant_custom_component") is not None:
            parser.error("Native checks require an environment WITHOUT the HA pytest plugin")
        print("HA", version("homeassistant"), "SDK", version("pyezvizapi"), flush=True)
        env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        run(
            "coverage",
            "run",
            "--branch",
            "--source="
            + ",".join(f"custom_components.ezviz_cloud.{name}" for name in COVERED_MODULES),
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "--asyncio-mode=auto",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            "tests/runtime",
            env=env,
        )
        run("coverage", "report", "--show-missing", "--fail-under=100")
    elif args.profile == "acceptance":
        run(
            "pytest",
            "-p",
            "pytest_homeassistant_custom_component",
            "--asyncio-mode=auto",
            "tests/acceptance",
        )
    else:
        if args.core is None:
            parser.error("core requires --core /path/to/core")
        command = [
            sys.executable,
            str(ROOT / "tools/check_core_policy.py"),
            "--core",
            str(args.core.resolve()),
        ]
        if args.runtime_tests:
            command.append("--runtime-tests")
        subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
