"""Qualified detector binary views; unqualified raw codes stay unknown."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .entity import EzvizInventoryEntity

DESCRIPTIONS = (
    (
        "door_status",
        BinarySensorEntityDescription(
            key="door_open",
            device_class=BinarySensorDeviceClass.DOOR,
            entity_registry_enabled_default=False,
        ),
    ),
    (
        "water_leak_status",
        BinarySensorEntityDescription(
            key="water_leak",
            device_class=BinarySensorDeviceClass.MOISTURE,
            entity_registry_enabled_default=False,
        ),
    ),
)


def detector_binary_sensors(coordinator: Any, serial: str, state: Any) -> list:
    return [
        EzvizDetectorBinarySensor(coordinator, serial, desc)
        for raw, desc in DESCRIPTIONS
        if state.capabilities.has(raw) is True
    ]


class EzvizDetectorBinarySensor(EzvizInventoryEntity, BinarySensorEntity):
    """No truthiness conversion of vendor contact/water enums."""

    def __init__(self, coordinator: Any, serial: str, desc: BinarySensorEntityDescription) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = desc
        self._attr_unique_id = f"{serial}_{desc.key}"

    @property
    def is_on(self) -> bool | None:
        state = self.device_state
        return getattr(state, self.entity_description.key, None) if state is not None else None
