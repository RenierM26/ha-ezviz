"""An abstract class common to all EZVIZ entities."""

from collections.abc import Mapping
import re
from typing import Any, override

from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .availability import device_available
from .const import DOMAIN, MANUFACTURER
from .coordinator import EzvizDataUpdateCoordinator

_MAC_REGEX = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


def _normalize_mac(device: Mapping[str, Any]) -> str | None:
    """Return a normalized MAC address if the value looks real."""

    if device.get("device_category") == "IGateWay":
        return None

    mac_address = device.get("mac_address")
    if not isinstance(mac_address, str):
        return None

    try:
        normalized = format_mac(mac_address)
    except ValueError:
        return None

    if not _MAC_REGEX.fullmatch(normalized):
        return None

    if normalized in {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}:
        return None

    return normalized


def _build_device_info(device: Mapping[str, Any], serial: str) -> DeviceInfo:
    """Return the Home Assistant device description for an EZVIZ device."""

    mac_address = _normalize_mac(device)
    if mac_address:
        return DeviceInfo(
            identifiers={(DOMAIN, serial)},
            connections={(CONNECTION_NETWORK_MAC, mac_address)},
            manufacturer=MANUFACTURER,
            model=device["device_sub_category"],
            name=device["name"],
            sw_version=device["version"],
            serial_number=serial,
        )

    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer=MANUFACTURER,
        model=device["device_sub_category"],
        name=device["name"],
        sw_version=device["version"],
        serial_number=serial,
    )


class EzvizEntityIdMixin:
    """Subscriptions are keyed by serial, so renames need no resubscription."""

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt into HA 2026.11 in-place renaming, preserving base bookkeeping."""
        if parent_hook := getattr(super(), "async_entity_id_changed", None):
            parent_hook(old_entity_id)


class EzvizEntity(
    EzvizEntityIdMixin, CoordinatorEntity[EzvizDataUpdateCoordinator], Entity
):
    """Generic entity encapsulating common features of EZVIZ device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._serial = serial
        device: Mapping[str, Any] = self.data
        self._camera_name = device["name"]
        self._attr_device_info = _build_device_info(device, serial)

    @property
    def serial(self) -> str:
        """Return the canonical identifier used for device commands."""
        return self._serial

    @property
    def data(self) -> Any:
        """Return coordinator data for this entity."""
        return (self.coordinator.data or {}).get(self._serial, {})

    @property
    @override
    def available(self) -> bool:
        """Return True if entity is available."""
        return device_available(self.coordinator, self._serial)


class EzvizBaseEntity(EzvizEntityIdMixin, Entity):
    """Generic entity for EZVIZ individual poll entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
    ) -> None:
        """Initialize the entity."""
        self._serial = serial
        self.coordinator = coordinator
        device: Mapping[str, Any] = self.data
        self._camera_name = device["name"]
        self._attr_device_info = _build_device_info(device, serial)

    @property
    def data(self) -> Any:
        """Return coordinator data for this entity."""
        return (self.coordinator.data or {}).get(self._serial, {})

    @property
    @override
    def available(self) -> bool:
        """Return True if entity is available."""
        return device_available(self.coordinator, self._serial)


class EzvizInventoryEntity(EzvizEntity):
    """Non-camera entities share the same poll and registry identity policy."""

    @property
    @override
    def data(self) -> Any:
        """Return this device cached coordinator projection."""
        return self.coordinator.device_data(self._serial)

    @property
    def device_state(self) -> Any:
        """Return the typed device view from the accepted inventory."""
        snapshot = self.coordinator.inventory_snapshot
        return snapshot.devices.get(self._serial) if snapshot is not None else None
