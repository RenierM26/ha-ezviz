"""Support for EZVIZ binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .migration import migrate_unique_ids_with_coordinator
from .utility import passes_description_gates

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizBinarySensorEntityDescription(BinarySensorEntityDescription):
    """EZVIZ binary sensor description with value, capability & device-category gating."""

    value_fn: Callable[[dict[str, Any]], bool]
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None
    required_device_categories: tuple[str, ...] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any],
    desc: EzvizBinarySensorEntityDescription,
) -> bool:
    """Return True if this binary sensor description is supported by the camera."""

    return passes_description_gates(
        camera_data,
        supported_ext_keys=desc.supported_ext_key,
        supported_ext_values=desc.supported_ext_value,
        required_device_categories=desc.required_device_categories,
        predicate=None,
    )


BINARY_SENSORS: tuple[EzvizBinarySensorEntityDescription, ...] = (
    EzvizBinarySensorEntityDescription(
        key="Motion_Trigger",
        translation_key="motion_trigger",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda d: bool(d.get("Motion_Trigger")),
    ),
    EzvizBinarySensorEntityDescription(
        key="alarm_schedules_enabled",
        translation_key="alarm_schedules_enabled",
        value_fn=lambda d: bool(d.get("alarm_schedules_enabled")),
    ),
    EzvizBinarySensorEntityDescription(
        key="encrypted",
        translation_key="encrypted",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.get("encrypted")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ binary sensors from coordinator data."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    await migrate_unique_ids_with_coordinator(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
        platform_domain="binary_sensor",
        allowed_keys=tuple(desc.key for desc in BINARY_SENSORS),
    )

    async_add_entities(
        EzvizBinarySensor(coordinator, serial, desc)
        for serial, camera_data in coordinator.data.items()
        for desc in BINARY_SENSORS
        if _is_desc_supported(camera_data, desc)
    )


class EzvizBinarySensor(EzvizEntity, BinarySensorEntity):
    """Representation of an EZVIZ binary sensor."""

    _attr_has_entity_name = True
    entity_description: EzvizBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary_sensor."""
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def is_on(self) -> bool:
        """Return the sensor state."""
        return self.entity_description.value_fn(self.data)
