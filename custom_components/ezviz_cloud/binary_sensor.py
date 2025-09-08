"""Support for EZVIZ binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizBinarySensorEntityDescription(BinarySensorEntityDescription):
    """EZVIZ binary sensor description with value & capability gating."""

    value_fn: Callable[[dict], bool]
    supported_ext_key: str | None = None
    supported_ext_value: tuple[str, ...] | None = None


def _is_desc_supported(
    camera_data: dict, desc: EzvizBinarySensorEntityDescription
) -> bool:
    """Return True if this entity description is supported by the camera."""
    # No gating configured: always supported
    if desc.supported_ext_key is None and desc.supported_ext_value is None:
        return True

    support_ext = camera_data.get("supportExt") or {}
    if not isinstance(support_ext, dict):
        return False

    current_val = support_ext.get(desc.supported_ext_key)
    if current_val is None:
        return False

    # If supported_ext_value is None -> presence of the key is enough
    if desc.supported_ext_value is None:
        return True

    return str(current_val) in desc.supported_ext_value


BINARY_SENSORS: tuple[EzvizBinarySensorEntityDescription, ...] = (
    EzvizBinarySensorEntityDescription(
        key="Motion_Trigger",
        translation_key="motion_trigger",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda d: bool(d.get("Motion_Trigger")),
        supported_ext_key=None,
        supported_ext_value=None,
    ),
    EzvizBinarySensorEntityDescription(
        key="alarm_schedules_enabled",
        translation_key="alarm_schedules_enabled",
        value_fn=lambda d: bool(d.get("alarm_schedules_enabled")),
        supported_ext_key=None,
        supported_ext_value=None,
    ),
    EzvizBinarySensorEntityDescription(
        key="encrypted",
        translation_key="encrypted",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.get("encrypted")),
        supported_ext_key=None,
        supported_ext_value=None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ binary sensors from a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    # One-time registry migration: "<serial>_<camera name>.<key>" -> "{serial}_{key}"
    await migrate_unique_ids_with_coordinator(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
        platform_domain="binary_sensor",
        allowed_keys=tuple(desc.key for desc in BINARY_SENSORS),
        mark_once_option="uid_migrated_v1_binary_sensor",
    )

    entities: list[EzvizBinarySensor] = []
    for serial, camera_data in coordinator.data.items():
        for desc in BINARY_SENSORS:
            if not _is_desc_supported(camera_data, desc):
                continue
            if desc.key in camera_data and camera_data[desc.key] is not None:
                entities.append(EzvizBinarySensor(coordinator, serial, desc))

    if entities:
        async_add_entities(entities)


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
        """Initialize the binary sensor."""
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def is_on(self) -> bool:
        """Return the sensor state."""
        return self.entity_description.value_fn(self.data)
