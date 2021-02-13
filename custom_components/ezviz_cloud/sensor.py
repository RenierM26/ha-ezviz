"""Support for Ezviz sensors."""
from typing import Callable, List

from pyezviz.constants import SensorType
from pyezviz.constants import DeviceSwitchType

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.const import (
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_SIGNAL_STRENGTH,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.typing import HomeAssistantType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN, MANUFACTURER
from .coordinator import EzvizDataUpdateCoordinator

# Sensor types are defined like so:
# sensor type name, unit_of_measurement, icon, device class, products supported
SENSOR_TYPES = [
    [
        "wifi",
        SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        None,
        DEVICE_CLASS_SIGNAL_STRENGTH,
    ],
    ["battery", PERCENTAGE, None, DEVICE_CLASS_BATTERY],
]


async def async_setup_entry(
    hass: HomeAssistantType,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Entity], bool], None],
) -> None:
    """Set up Ezviz sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    sensors = []

    for idx, camera in enumerate(coordinator.data):
        sensors.append(EzvizSensor(coordinator, idx, camera))

    async_add_entities(sensors)


class EzvizSensor(CoordinatorEntity, Entity, RestoreEntity):
    """Representation of a Ezviz sensor."""

    def __init__(self, coordinator, idx, camera):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._idx = idx
        self._serial = self.coordinator.data[self._idx]["serial"]
        self._name = self.coordinator.data[self._idx]["name"]

        sensor_type_name = sensor_type[0].replace("_", " ").title()
        self._name = self.coordinator.data[self._idx]["name"] + sensor_type_name

        ezviz_sensor_type = None
        if self._sensor_type[0] == "air_quality":
            ezviz_sensor_type = SensorType.AIR_QUALITY
        elif self._sensor_type[0] == "temperature":
            ezviz_sensor_type = SensorType.TEMPERATURE
        elif self._sensor_type[0] == "humidity":
            ezviz_sensor_type = SensorType.HUMIDITY
        elif self._sensor_type[0] == "wifi":
            ezviz_sensor_type = SensorType.WIFI
        elif self._sensor_type[0] == "battery":
            ezviz_sensor_type = SensorType.BATTERY

        self._ezviz_type = ezviz_sensor_type

    @property
    def reading(self):
        """Return the device sensor reading."""
        readings = self.coordinator.data["readings"][self._device_id]

        value = next(
            (
                reading.value
                for reading in readings
                if reading.sensor_type == self._ezviz_type
            ),
            None,
        )

        if value is not None:
            return round(float(value))

        return None

    @property
    def name(self):
        """Return the name of the Ezviz sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.reading

    @property
    def unique_id(self):
        """Return the unique ID of this sensor."""
        return f"{self._device_id}_{self._sensor_type[0]}"

    @property
    def device_info(self):
        """Return the device_info of the device."""
        return {
            "identifiers": {(DOMAIN, str(self._device_id))},
            "name": self._device_name,
            "model": self._device_type_name,
            "manufacturer": MANUFACTURER,
        }

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self._sensor_type[1]

    @property
    def device_class(self):
        """Device class for the sensor."""
        return self._sensor_type[3]

    @property
    def icon(self):
        """Icon for the sensor."""
        return self._sensor_type[2]

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        reading = self.reading

        return None
