"""Native HA fixtures, independent of the version-pinned custom-component plugin."""

import asyncio
import socket

import pytest
import pytest_asyncio

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry, entity_registry, frame


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """SDK/broker/image boundaries must stay synthetic, including DNS."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Native runtime controls must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest_asyncio.fixture
async def hass(tmp_path):
    """Use real HA core, entries and disk storage without plugin monkeypatches."""
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    frame.async_setup(instance)
    async with asyncio.timeout(10):
        await instance.config_entries.async_initialize()
        device_registry.async_setup(instance)
        await device_registry.async_load(instance)
        await entity_registry.async_load(instance)
    instance.set_state(CoreState.running)
    yield instance
    if instance.state is not CoreState.stopped:
        await instance.async_stop()
