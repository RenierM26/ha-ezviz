"""Run stock hassfest with one visible custom-only SDK dependency exception.

The built-in ezviz integration is not qualified alongside ezviz_cloud. Do not
broaden this exception: a changed core pin must be reviewed again. Once core
accepts the required SDK version, stock validation passes without an exception.
"""

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
import sys
from typing import Any

EXPECTED_REQUIREMENT = "pyezvizapi>=2.0.0"
EXPECTED_ERROR = (
    "Requirement pyezvizapi>=2.0.0 is incompatible with pyezvizapi==1.0.5.0, "
    "which Home Assistant depends on."
)


def validate_with_exception(
    validate: Callable[[Any, Any], bool], integration: Any, config: Any
) -> bool:
    """Keep every stock error except this exact, documented optional-core pin."""
    start = len(integration.errors)
    valid = validate(integration, config)
    if (
        valid is not False
        or integration.core
        or integration.domain != "ezviz_cloud"
        or integration.requirements != [EXPECTED_REQUIREMENT]
    ):
        return valid

    added = integration.errors[start:]
    allowed = [
        error
        for error in added
        if error.plugin == "requirements" and error.error == EXPECTED_ERROR
    ]
    if len(allowed) != 1:
        return valid

    # Retain errors from earlier plugins and every other requirements finding.
    integration.errors[start:] = [error for error in added if error is not allowed[0]]
    print(
        "::warning title=Known custom-only EZVIZ dependency exception::"
        f"{EXPECTED_ERROR} Custom-only 2.0.0 acceptance passes; simultaneous use "
        "with built-in EZVIZ is not qualified. All other hassfest errors remain fatal."
    )
    return len(integration.errors) == start


def main() -> int:
    """Use the container's stock plugin selection, CLI and final exit status."""
    sys.path.insert(0, str(Path.cwd()))
    checks: Any = import_module("script.hassfest.requirements")
    runner = import_module("script.hassfest.__main__")
    original = checks.validate_custom_requirements

    def validate(integration: Any, config: Any) -> bool:
        return validate_with_exception(original, integration, config)

    checks.validate_custom_requirements = validate
    return runner.main()


if __name__ == "__main__":
    raise SystemExit(main())
