"""Keep standalone, native and plugin-patched HA tests in separate processes."""

from importlib.util import find_spec
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
_TESTS = _ROOT / "tests"


def _layers_for(target: Path) -> set[str]:
    """Classify requested paths before importing any test modules."""
    layers = {
        name
        for name, directory in (
            ("native", _TESTS / "runtime"),
            ("acceptance", _TESTS / "acceptance"),
        )
        if target == directory
        or directory in target.parents
        or target in directory.parents
    }
    if target in (_ROOT, _TESTS) or (
        target.parent == _TESTS
        and target.name.startswith("test_")
        and target.suffix == ".py"
    ):
        layers.add("standalone")
    return layers


def pytest_sessionstart(session: pytest.Session) -> None:
    """Refuse mixed/unscoped collection before test imports or fixtures run."""
    config = session.config
    layers: set[str] = set()
    for argument in config.args:
        path = (config.invocation_params.dir / argument.split("::", 1)[0]).resolve()
        selected = _layers_for(path)
        if not selected or config.option.pyargs:
            raise pytest.UsageError(
                "Choose one explicit test layer by filesystem path; "
                "use tools/check.py standalone, native or acceptance"
            )
        layers.update(selected)
    if len(layers) != 1:
        raise pytest.UsageError(
            "Mixed or unscoped EZVIZ test collection is unsupported: "
            "standalone, native and acceptance must run in separate processes. "
            "Use tools/check.py standalone, native or acceptance"
        )
    if (
        layers == {"native"}
        and find_spec("pytest_homeassistant_custom_component") is not None
    ):
        raise pytest.UsageError(
            "Native HA tests require an environment WITHOUT "
            "pytest-homeassistant-custom-component, even when plugin autoload is disabled"
        )
