"""Keep the single custom-only dependency waiver narrow and fail-closed."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_hassfest", Path(__file__).resolve().parents[1] / "tools/check_hassfest.py"
)
assert SPEC is not None and SPEC.loader is not None
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def integration(**overrides):
    return SimpleNamespace(**({
        "core": False, "domain": "ezviz_cloud",
        "requirements": ["pyezvizapi>=2.3.0"], "errors": [],
    } | overrides))


def error(message=None, plugin="requirements"):
    return SimpleNamespace(plugin=plugin, error=message or CHECK.EXPECTED_ERROR)


def validator(*errors):
    def validate(component, _config):
        component.errors.extend(errors)
        return not errors
    return validate


def test_expected_conflict_warns_and_keeps_prior_plugin_errors(capsys):
    prior = error("Invalid manifest", "manifest")
    component = integration(errors=[prior])
    print("Validating requirements...", end="")
    assert CHECK.validate_with_exception(validator(error()), component, None)
    assert component.errors == [prior]
    assert "\n::warning" in capsys.readouterr().out


def test_expected_conflict_does_not_hide_an_additional_failure():
    other = error("Different requirement is invalid")
    component = integration()
    assert not CHECK.validate_with_exception(validator(error(), other), component, None)
    assert component.errors == [other]


@pytest.mark.parametrize("overrides,reported", [
    ({"core": True}, error()),
    ({"domain": "other"}, error()),
    ({"requirements": ["pyezvizapi>=3.0.0"]}, error()),
    ({"requirements": ["pyezvizapi>=2.1.0"]}, error()),
    ({"requirements": ["pyezvizapi>=2.2.0"]}, error()),
    ({}, error(CHECK.EXPECTED_ERROR.replace(">=2.3.0", ">=2.1.0"))),
    ({"requirements": ["pyezvizapi>=2.3.0", "other>=1"]}, error()),
    ({}, error(CHECK.EXPECTED_ERROR.replace("==1.0.5.0", "==1.0.5.1"))),
    ({}, error(plugin="manifest")),
])
def test_changed_context_is_not_waived(overrides, reported, capsys):
    component = integration(**overrides)
    assert not CHECK.validate_with_exception(validator(reported), component, None)
    assert component.errors == [reported]
    assert not capsys.readouterr().out


def test_upstream_alignment_needs_no_exception(capsys):
    component = integration()
    assert CHECK.validate_with_exception(validator(), component, None)
    assert not component.errors
    assert not capsys.readouterr().out


def test_duplicate_errors_are_not_silently_waived():
    reported = [error(), error()]
    component = integration()
    assert not CHECK.validate_with_exception(validator(*reported), component, None)
    assert component.errors == reported


def test_validator_crashes_are_not_waived():
    def crash(*_args):
        raise RuntimeError("validator changed")
    with pytest.raises(RuntimeError, match="validator changed"):
        CHECK.validate_with_exception(crash, integration(), None)
