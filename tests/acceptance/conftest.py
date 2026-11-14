"""Real HA acceptance, kept separate from the lightweight Python 3.14 job."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Enable this checkout; all account/device boundaries remain synthetic."""


@pytest.fixture
def hass_storage():
    """Use the real Store path, not the plugin's mocked successful disk writes."""
    return {}


@pytest.fixture
def hass_config_dir(tmp_path):
    """All actual storage is isolated to pytest's temporary config directory."""
    return str(tmp_path)
