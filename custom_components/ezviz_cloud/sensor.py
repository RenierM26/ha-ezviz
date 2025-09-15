"""Support for EZVIZ sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from pyezvizapi.constants import SupportExt

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .migration import migrate_unique_ids_with_coordinator

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizSensorEntityDescription(SensorEntityDescription):
    """EZVIZ sensor with value, capability & device-category gating."""

    value_fn: Callable[[dict[str, Any]], Any]
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None
    required_device_categories: tuple[str, ...] | None = None
    # Optional predicate to decide availability based on camera data
    available_fn: Callable[[dict[str, Any]], bool] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any],
    desc: EzvizSensorEntityDescription,
) -> bool:
    """Return True if this sensor description is supported by the camera."""

    if desc.required_device_categories is not None:
        device_category = camera_data.get("device_category")
        if device_category not in desc.required_device_categories:
            return False

    if desc.supported_ext_key is None:
        # No explicit supportExt requirement; continue
        pass
    else:
        support_ext = camera_data.get("supportExt") or {}
        if not isinstance(support_ext, dict):
            return False
        current_val = support_ext.get(desc.supported_ext_key)
        if current_val is None:
            return False
        current_val_str = str(current_val).strip()
        if desc.supported_ext_value and not any(
            current_val_str == option.strip() for option in desc.supported_ext_value
        ):
            return False
    # If an availability predicate is provided, respect it
    if desc.available_fn is not None:
        return bool(desc.available_fn(camera_data))

    return True


SENSORS: tuple[EzvizSensorEntityDescription, ...] = (
    EzvizSensorEntityDescription(
        key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda d: d.get("battery_level"),
        # Available when battery_level is present in data
        available_fn=lambda d: d.get("battery_level") is not None,
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
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    await migrate_unique_ids_with_coordinator(
        hass,
        entry,
        coordinator,
        platform_domain="sensor",
        allowed_keys=tuple(desc.key for desc in SENSORS),
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
    def native_value(self) -> Any:
        """Return the sensor's value from the coordinator snapshot."""
        return self.entity_description.value_fn(self.data)
