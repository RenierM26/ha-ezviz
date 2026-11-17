"""Discover SDK channel entities without another account poll or device entry."""

from collections.abc import Callable, Iterable
from typing import Any

from pyezvizapi.devices.channels import (
    CameraChannel,
    CameraChannelError,
    camera_channels,
    resolve_camera_channel,
)

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .availability import device_available
from .const import DOMAIN, SIGNAL_PICTURE_CAPTURED
from .coordinator import EzvizDataUpdateCoordinator
from .runtime import EzvizConfigEntry

CONF_LENS_RTSP_PATHS = "lens_rtsp_paths"
SIGNAL_CHANNEL_NOTIFICATION = "ezviz_cloud_channel_notification_{}_{}"


def capture_signal(serial: str, channel_key: str | None = None) -> str:
    """Keep existing capture subscriptions unchanged and isolate channel captures."""
    signal = SIGNAL_PICTURE_CAPTURED.format(serial)
    return signal if channel_key is None else f"{signal}:{channel_key}"


def channel_available(
    coordinator: EzvizDataUpdateCoordinator, serial: str, key: str
) -> bool:
    """Missing metadata is unavailable, not a reason to select a different lens."""
    if not device_available(coordinator, serial):
        return False
    try:
        resolve_camera_channel(coordinator.device_data(serial), key)
    except CameraChannelError:
        return False
    return True


@callback
def async_add_channel_entities(
    coordinator: EzvizDataUpdateCoordinator,
    entry: EzvizConfigEntry,
    add: AddConfigEntryEntitiesCallback,
    factory: Callable[[str, CameraChannel], Iterable[Any]],
) -> None:
    """Add stable child entities when multi-channel metadata becomes available."""
    seen: set[str] = set()
    registry_entries = er.async_entries_for_config_entry(
        er.async_get(coordinator.hass), entry.entry_id
    )
    multi_channel: set[str] = {
        serial
        for serial in coordinator.data
        if any(
            e.platform == DOMAIN and e.unique_id.startswith(f"{serial}_channel_")
            for e in registry_entries
        )
    }
    accepted_snapshot = None

    @callback
    def discover() -> None:
        nonlocal accepted_snapshot
        if coordinator.inventory_snapshot is accepted_snapshot:
            return
        accepted_snapshot = coordinator.inventory_snapshot
        entities = []
        for serial, data in coordinator.data.items():
            channels = camera_channels(data)
            if len(channels) > 1:
                multi_channel.add(serial)
            if serial not in multi_channel:
                continue
            for channel in channels:
                for entity in factory(serial, channel):
                    if entity.unique_id not in seen:
                        seen.add(entity.unique_id)
                        entities.append(entity)
        if entities:
            add(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))
