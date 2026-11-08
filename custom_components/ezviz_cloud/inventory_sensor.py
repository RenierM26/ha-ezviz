"""Read-only detector values; raw enums never acquire invented semantics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import PERCENTAGE, EntityCategory

from .entity import EzvizInventoryEntity

DETECTOR_SENSORS = (
    SensorEntityDescription(
        key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
    ),
    *(
        SensorEntityDescription(
            key=key,
            translation_key=key,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        )
        for key in (
            "door_status",
            "water_leak_status",
            "siren_work_state",
            "zigbee_signal",
            "power_mode",
        )
    ),
)


def detector_sensors(coordinator: Any, serial: str, state: Any) -> list:
    return [
        EzvizDetectorSensor(coordinator, serial, desc)
        for desc in DETECTOR_SENSORS
        if state.capabilities.has(desc.key) is True
    ]


class EzvizDetectorSensor(EzvizInventoryEntity, SensorEntity):
    """Diagnostics share inventory freshness and preserve zero/false/unknown."""

    def __init__(self, coordinator: Any, serial: str, desc: SensorEntityDescription) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = desc
        self._attr_unique_id = f"{serial}_{desc.key}"

    @property
    def native_value(self) -> Any:
        state = self.device_state
        if state is None:
            return None
        if self.entity_description.key == "battery_level":
            return state.battery_level
        value = state.values.get(self.entity_description.key)
        return value if isinstance(value, (str, int, float, bool)) else None
