"""Check this integration in an isolated Core namespace, without a live HA config."""

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "ezviz_cloud"
CORE_TESTS = ROOT / "tests/runtime/core_policy"


def run(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Print the bounded command and fail on any policy finding."""
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True, timeout=180)


def check_argument_type_guard(
    command: list[str], probe: Path, core: Path, env: dict[str, str]
) -> None:
    """Prove the actual Pylint invocation rejects a mistyped HA common argument."""
    probe.write_text(
        '"""Negative control for Home Assistant argument typing."""\n'
        "from typing import Any\n\n"
        "def policy_probe(hass: Any) -> None:\n"
        '    """The checker must reject this annotation."""\n'
    )
    try:
        result = subprocess.run(
            [*command, "--output-format=json", str(probe)],
            cwd=core,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        messages = json.loads(result.stdout)
        if (
            result.returncode != 2
            or len(messages) != 1
            or messages[0]["message-id"] != "E7402"
            or messages[0]["symbol"] != "home-assistant-argument-type"
        ):
            raise RuntimeError(
                f"Pylint common-argument negative control failed: {result.stdout} {result.stderr}"
            )
        print("PASS: Pylint negative control rejects hass: Any with E7402", flush=True)
    finally:
        probe.unlink()


def prepare_test_overlay(core: Path, overlay: Path) -> tuple[Path, Path]:
    """Copy focused tests and Core's own test-policy overlay."""
    # Use Core's test overlay, not production rules for private-access tests.
    tests = overlay / "tests"
    tests.mkdir()
    (overlay / "pyproject.toml").symlink_to(core / "pyproject.toml")
    test_config = core / "tests/ruff.toml"
    policy = (
        test_config.read_text()
        if test_config.is_file()
        else subprocess.check_output(
            ["git", "show", "HEAD:tests/ruff.toml"],
            cwd=core,
            text=True,
        )
    )
    (tests / "ruff.toml").write_text(policy)
    shutil.copyfile(ROOT / "tests/__init__.py", tests / "__init__.py")
    runtime_tests = tests / "runtime"
    runtime_tests.mkdir()
    shutil.copyfile(ROOT / "tests/runtime/__init__.py", runtime_tests / "__init__.py")
    focused_tests = runtime_tests / "core_policy"
    shutil.copytree(
        CORE_TESTS, focused_tests, ignore=shutil.ignore_patterns("__pycache__", "ruff.toml")
    )
    return tests / "ruff.toml", focused_tests


def main() -> None:
    """Mirror only integration sources and apply unmodified Core policy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--runtime-tests", action="store_true")
    args = parser.parse_args()
    core = args.core.resolve()
    if not (core / "homeassistant/components/__init__.py").is_file():
        parser.error("--core must be a Home Assistant Core checkout")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=core, text=True).strip()
    source = ROOT / "custom_components/ezviz_cloud"
    files = {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in (".py", ".json", ".yaml")
    }
    print(
        json.dumps(
            {
                "core_commit": revision,
                "python": sys.version,
                "versions": {
                    name: version(name)
                    for name in ("homeassistant", "pyezvizapi", "ruff", "mypy", "pylint")
                },
                "source_sha256": files,
                "core_test_sha256": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(CORE_TESTS.glob("*.py"))
                },
            },
            indent=2,
        ),
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="ezviz-core-policy-") as directory:
        overlay = Path(directory)
        package = overlay / "homeassistant"
        package.mkdir()
        for path in (core / "homeassistant").iterdir():
            if path.name not in ("components", "__pycache__"):
                (package / path.name).symlink_to(path, target_is_directory=path.is_dir())
        components = package / "components"
        components.mkdir()
        for path in (core / "homeassistant/components").iterdir():
            if path.name not in (DOMAIN, "__pycache__"):
                (components / path.name).symlink_to(path, target_is_directory=path.is_dir())
        mirror = components / DOMAIN
        shutil.copytree(source, mirror, ignore=shutil.ignore_patterns("__pycache__"))
        test_policy, focused_tests = prepare_test_overlay(core, overlay)
        for name in ("script", "requirements_all.txt"):
            (overlay / name).symlink_to(core / name, target_is_directory=(core / name).is_dir())
        env = {**os.environ, "MYPYPATH": f"{overlay}{os.pathsep}{core / 'stubs'}"}
        python = sys.executable
        run([python, "-m", "script.translations", "develop", "--integration", DOMAIN], overlay, env)
        if json.loads((mirror / "translations/en.json").read_text()) != json.loads(
            (source / "translations/en.json").read_text()
        ):
            raise ValueError(
                "English translations differ from Core generation; regenerate translations/en.json"
            )
        run(
            [
                python,
                "-m",
                "ruff",
                "check",
                "--no-fix",
                "--no-respect-gitignore",
                "--config",
                str(core / "pyproject.toml"),
                str(mirror),
            ],
            core,
            env,
        )
        run(
            [
                python,
                "-m",
                "ruff",
                "format",
                "--check",
                "--no-respect-gitignore",
                "--config",
                str(core / "pyproject.toml"),
                str(mirror),
            ],
            core,
            env,
        )
        run(
            [
                python,
                "-m",
                "ruff",
                "check",
                "--no-fix",
                "--extend-select",
                "ANN",
                "--no-respect-gitignore",
                "--config",
                str(test_policy),
                str(focused_tests),
            ],
            core,
            env,
        )
        run(
            [
                python,
                "-m",
                "ruff",
                "format",
                "--check",
                "--no-respect-gitignore",
                "--config",
                str(test_policy),
                str(focused_tests),
            ],
            core,
            env,
        )
        pylint_command = [
            python,
            "-m",
            "pylint",
            "--rcfile",
            str(core / "pyproject.toml"),
            "--ignore-missing-annotations=n",
            "--reports=n",
            "-j",
            "1",
        ]
        check_argument_type_guard(pylint_command, mirror / "_type_policy_probe.py", core, env)
        run([*pylint_command, str(mirror)], core, env)
        run(
            [
                python,
                "-m",
                "mypy",
                "--config-file",
                str(core / "mypy.ini"),
                "--follow-imports=silent",
                "--show-error-codes",
                "--no-error-summary",
                str(mirror),
            ],
            core,
            env,
        )
        # The wrapper permits only the documented stock built-in SDK conflict.
        run(
            [
                python,
                str(ROOT / "tools/check_hassfest.py"),
                "--action",
                "validate",
                "--integration-path",
                str(source),
            ],
            core,
            env,
        )
        if args.runtime_tests:
            run(
                [
                    python,
                    "-m",
                    "pytest",
                    "-p",
                    "pytest_asyncio.plugin",
                    "--asyncio-mode=auto",
                    "-o",
                    "asyncio_default_fixture_loop_scope=function",
                    "tests/runtime",
                ],
                ROOT,
                {**env, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )


if __name__ == "__main__":
    main()
