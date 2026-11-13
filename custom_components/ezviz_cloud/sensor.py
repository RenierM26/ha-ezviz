"""Support for EZVIZ sensors."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast, override

from pyezvizapi.constants import SupportExt

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity, EzvizInventoryEntity
from .inventory import async_add_inventory_entities
from .migration import migrate_unique_ids_with_coordinator
from .runtime import EzvizConfigEntry
from .utility import (
    network_type_value,
    passes_description_gates,
    sd_card_capacity_gb,
    wifi_signal_value,
    wifi_ssid_value,
)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizSensorEntityDescription(SensorEntityDescription):
    """EZVIZ sensor with value, capability & device-category gating."""

    value_fn: Callable[[dict[str, Any]], Any]
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None
    required_device_categories: tuple[str, ...] | None = None
    is_supported_fn: Callable[[dict[str, Any]], bool] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any],
    desc: EzvizSensorEntityDescription,
) -> bool:
    """Return True if this sensor description is supported by the camera."""

    return passes_description_gates(
        camera_data,
        supported_ext_keys=desc.supported_ext_key,
        supported_ext_values=desc.supported_ext_value,
        required_device_categories=desc.required_device_categories,
        predicate=desc.is_supported_fn,
    )


SENSORS: tuple[EzvizSensorEntityDescription, ...] = (
    EzvizSensorEntityDescription(
        key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda d: d.get("battery_level"),
        # Supported when battery_level is present in data
        is_supported_fn=lambda d: d.get("battery_level") is not None,
    ),
    EzvizSensorEntityDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=wifi_signal_value,
        is_supported_fn=lambda d: wifi_signal_value(d) is not None,
    ),
    EzvizSensorEntityDescription(
        key="wifi_ssid",
        translation_key="wifi_ssid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=wifi_ssid_value,
        is_supported_fn=lambda d: wifi_ssid_value(d) is not None,
    ),
    EzvizSensorEntityDescription(
        key="network_type",
        translation_key="network_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=network_type_value,
        is_supported_fn=lambda d: network_type_value(d) is not None,
    ),
    EzvizSensorEntityDescription(
        key="sd_card_capacity",
        translation_key="sd_card_capacity",
        device_class=SensorDeviceClass.DATA_SIZE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        value_fn=sd_card_capacity_gb,
        is_supported_fn=lambda d: sd_card_capacity_gb(d) is not None,
    ),
    # Battery charge state derived from optionals.powerStatus
    EzvizSensorEntityDescription(
        key="battery_charge_state",
        translation_key="battery_charge_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        supported_ext_key=str(SupportExt.SupportBatteryManage.value),
        supported_ext_value=["1"],
        value_fn=lambda d: {
            0: "not_charging",
            1: "charging",
            2: "full",
            3: "no_battery",
            4: "fault",
        }.get(cast(int, (d.get("optionals") or {}).get("powerStatus"))),
    ),
    EzvizSensorEntityDescription(
        key="alarm_sound_mod",
        translation_key="alarm_sound_mod",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("alarm_sound_mod"),
    ),
    EzvizSensorEntityDescription(
        key="last_alarm_time",
        translation_key="last_alarm_time",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("last_alarm_time"),
    ),
    EzvizSensorEntityDescription(
        key="Seconds_Last_Trigger",
        translation_key="seconds_last_trigger",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("Seconds_Last_Trigger"),
    ),
    EzvizSensorEntityDescription(
        key="last_alarm_pic",
        translation_key="last_alarm_pic",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("last_alarm_pic"),
    ),
    EzvizSensorEntityDescription(
        key="supported_channels",
        translation_key="supported_channels",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("supported_channels"),
    ),
    EzvizSensorEntityDescription(
        key="local_ip",
        translation_key="local_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("local_ip"),
    ),
    EzvizSensorEntityDescription(
        key="wan_ip",
        translation_key="wan_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("wan_ip"),
    ),
    EzvizSensorEntityDescription(
        key="PIR_Status",
        translation_key="pir_status",
        value_fn=lambda d: d.get("PIR_Status"),
    ),
    EzvizSensorEntityDescription(
        key="last_alarm_type_code",
        translation_key="last_alarm_type_code",
        value_fn=lambda d: d.get("last_alarm_type_code"),
    ),
    EzvizSensorEntityDescription(
        key="last_alarm_type_name",
        translation_key="last_alarm_type_name",
        value_fn=lambda d: d.get("last_alarm_type_name"),
    ),
    EzvizSensorEntityDescription(
        key="last_offline_time",
        translation_key="last_offline_time",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("last_offline_time"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = entry.runtime_data.coordinator

    await migrate_unique_ids_with_coordinator(
        hass,
        entry,
        coordinator,
        platform_domain="sensor",
        allowed_keys=tuple(desc.key for desc in SENSORS),
    )

    async_add_inventory_entities(
        coordinator, entry, async_add_entities, "detector", detector_sensors
    )

    async_add_entities(
        EzvizSensor(coordinator, serial, desc)
        for serial, camera_data in coordinator.data.items()
        for desc in SENSORS
        if _is_desc_supported(camera_data, desc)
    )


class EzvizSensor(EzvizEntity, SensorEntity):
    """Set up EZVIZ sensors from coordinator data."""

    _attr_has_entity_name = True
    entity_description: EzvizSensorEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    @override
    def native_value(self) -> StateType:
        """Return the sensor's value from the coordinator snapshot."""
        return self.entity_description.value_fn(self.data)


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
    """Create only observed detector sensor readings."""
    return [
        EzvizDetectorSensor(coordinator, serial, desc)
        for desc in DETECTOR_SENSORS
        if state.capabilities.has(desc.key) is True
    ]


class EzvizDetectorSensor(EzvizInventoryEntity, SensorEntity):
    """Diagnostics share inventory freshness and preserve zero/false/unknown."""

    def __init__(
        self, coordinator: Any, serial: str, desc: SensorEntityDescription
    ) -> None:
        """Initialize the owner and its cached dependencies."""
        super().__init__(coordinator, serial)
        self.entity_description = desc
        self._attr_unique_id = f"{serial}_{desc.key}"

    @property
    @override
    def native_value(self) -> StateType:
        """Return the cached reading without inventing enum semantics."""
        state = self.device_state
        if state is None:
            return None
        if self.entity_description.key == "battery_level":
            return state.battery_level
        value = state.values.get(self.entity_description.key)
        return value if isinstance(value, (str, int, float, bool)) else None
