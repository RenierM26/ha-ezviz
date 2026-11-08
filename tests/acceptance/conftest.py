"""Use this checkout with a real, isolated HA runtime; no account/device access."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Enable importing this custom integration in the pinned HA runtime."""
