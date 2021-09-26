"""Support for Ezviz binary sensors."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    DEVICE_CLASS_MOTION,
    DEVICE_CLASS_UPDATE,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

PARALLEL_UPDATES = 1

BINARY_SENSOR_TYPES: dict[str, BinarySensorEntityDescription] = {
    "Motion_Trigger": BinarySensorEntityDescription(
        key="Motion_Trigger",
        device_class=DEVICE_CLASS_MOTION,
    ),
    "alarm_schedules_enabled": BinarySensorEntityDescription(
        key="alarm_schedules_enabled"
    ),
    "encrypted": BinarySensorEntityDescription(key="encrypted"),
    "upgrade_available": BinarySensorEntityDescription(
        key="upgrade_available",
        device_class=DEVICE_CLASS_UPDATE,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Ezviz sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities(
        [
            EzvizBinarySensor(coordinator, camera, binary_sensor)
            for camera in coordinator.data
            for binary_sensor, value in coordinator.data[camera].items()
            if binary_sensor in BINARY_SENSOR_TYPES
            if value is not None
        ]
    )


class EzvizBinarySensor(EzvizEntity, BinarySensorEntity):
    """Representation of a Ezviz sensor."""

    coordinator: EzvizDataUpdateCoordinator

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        binary_sensor: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._sensor_name = binary_sensor
        self._attr_name = f"{self._camera_name} {binary_sensor.title()}"
        self._attr_unique_id = f"{serial}_{self._camera_name}.{binary_sensor}"
        self.entity_description = BINARY_SENSOR_TYPES[binary_sensor]

    @property
    def is_on(self) -> bool:
        """Return the state of the sensor."""
        return self.data[self._sensor_name]
