"""Exercise collection isolation through real pytest subprocesses, without HA."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_tree(tmp_path: Path) -> Path:
    """Use the actual repository guard and inert test modules in a private tree."""
    (tmp_path / "conftest.py").write_text((ROOT / "conftest.py").read_text())
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    for path in ("test_unit.py", "runtime/test_native.py", "acceptance/test_plugin.py"):
        target = tmp_path / "tests" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_inert():\n    assert True\n")
    return tmp_path


def collect(tree: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run actual collection in a clean plugin context, with a bounded deadline."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=cwd or tree,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(tree)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("target", ["tests/test_unit.py", "tests/runtime", "tests/acceptance"])
def test_one_layer_collects(isolated_tree: Path, target: str) -> None:
    """A safe explicit layer is not blocked by the isolation guard."""
    result = collect(isolated_tree, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 test collected" in result.stdout


@pytest.mark.parametrize("arguments", [
    (), ("tests",), (".",),
    ("tests/test_unit.py", "tests/runtime"),
    ("tests/runtime", "tests/acceptance"),
    ("tests/test_unit.py", "tests/acceptance/test_plugin.py::test_inert"),
])
def test_mixed_collection_stops_before_import(isolated_tree: Path, arguments: tuple[str, ...]) -> None:
    """Neither a sentinel module nor any tests can execute after rejected selection."""
    for target in (isolated_tree / "tests").rglob("test_*.py"):
        target.write_text('raise RuntimeError("TEST_MODULE_WAS_IMPORTED")\n')
    result = collect(isolated_tree, *arguments)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    output = result.stdout + result.stderr
    assert "must run in separate processes" in output
    assert "TEST_MODULE_WAS_IMPORTED" not in output


def test_absolute_path_and_node_id_from_another_directory(isolated_tree: Path) -> None:
    """Classification follows pytest's invocation directory, not the repo cwd."""
    result = collect(isolated_tree, str(isolated_tree / "tests/test_unit.py") + "::test_inert", cwd=isolated_tree / "tests/runtime")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("arguments", [("unclassified.py",), ("--pyargs", "tests.runtime")])
def test_unsupported_target_has_actionable_error(isolated_tree: Path, arguments: tuple[str, ...]) -> None:
    """Do not silently bypass the guard with an unclassified or package target."""
    (isolated_tree / "unclassified.py").write_text("def test_inert():\n    pass\n")
    result = collect(isolated_tree, *arguments)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "Choose one explicit test layer" in result.stderr


def test_native_rejects_discoverable_patch_plugin_even_without_autoload(isolated_tree: Path) -> None:
    """A discoverable plugin package is prohibited in the native profile."""
    plugin = isolated_tree / "pytest_homeassistant_custom_component"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("")
    result = collect(isolated_tree, "tests/runtime")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "WITHOUT pytest-homeassistant-custom-component" in result.stderr


def test_unguarded_control_really_imports_test_modules(isolated_tree: Path) -> None:
    """The sentinel distinguishes rejection from ordinary import-time failure."""
    (isolated_tree / "conftest.py").rename(isolated_tree / "disabled_policy.py")
    (isolated_tree / "tests/test_unit.py").write_text(
        'raise RuntimeError("TEST_MODULE_WAS_IMPORTED")\n'
    )
    result = collect(isolated_tree, "tests/test_unit.py", "tests/runtime")
    assert result.returncode == pytest.ExitCode.INTERRUPTED
    assert "TEST_MODULE_WAS_IMPORTED" in result.stdout + result.stderr
