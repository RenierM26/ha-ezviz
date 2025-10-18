"""An abstract class common to all EZVIZ entities."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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


class EzvizEntity(CoordinatorEntity[EzvizDataUpdateCoordinator], Entity):
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
    def data(self) -> Any:
        """Return coordinator data for this entity."""
        return self.coordinator.data[self._serial]

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return bool(self.data["status"] != 2)


class EzvizBaseEntity(Entity):
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
        return self.coordinator.data[self._serial]

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return bool(self.data["status"] != 2)
