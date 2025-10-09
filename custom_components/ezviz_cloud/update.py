"""Support for EZVIZ firmware updates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyezvizapi import HTTPError, PyEzvizError

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

PARALLEL_UPDATES = 1

@dataclass(frozen=True, kw_only=True)
class EzvizUpdateEntityDescription(UpdateEntityDescription):
    """EZVIZ update entity description with optional support predicate."""

    is_supported_fn: Callable[[dict[str, Any]], bool] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any], description: EzvizUpdateEntityDescription
) -> bool:
    """Return True when the update description is supported for the camera."""

    if description.is_supported_fn is None:
        return True
    return description.is_supported_fn(camera_data)


UPDATE_ENTITY_DESCRIPTIONS: tuple[EzvizUpdateEntityDescription, ...] = (
    EzvizUpdateEntityDescription(
        key="version",
        device_class=UpdateDeviceClass.FIRMWARE,
        is_supported_fn=lambda data: isinstance(data.get("version"), str)
        and bool(data.get("version")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ update entities based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    entities = [
        EzvizUpdateEntity(coordinator, serial, description)
        for serial, camera_data in coordinator.data.items()
        for description in UPDATE_ENTITY_DESCRIPTIONS
        if _is_desc_supported(camera_data, description)
    ]

    async_add_entities(entities)


class EzvizUpdateEntity(EzvizEntity, UpdateEntity):
    """Representation of a EZVIZ Update entity."""

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizUpdateEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{description.key}"
        self.entity_description = description

    @property
    def installed_version(self) -> str | None:
        """Version installed and in use."""
        version = self.data.get("version")
        if isinstance(version, str):
            return version
        return None

    @property
    def in_progress(self) -> bool:
        """Update installation progress."""
        return bool(self.data["upgrade_in_progress"])

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        if self.data.get("upgrade_available"):
            latest_info = self.data.get("latest_firmware_info")
            if isinstance(latest_info, dict):
                version = latest_info.get("version")
                if isinstance(version, str):
                    return version

        return self.installed_version

    def release_notes(self) -> str | None:
        """Return full release notes."""
        latest_info = self.data.get("latest_firmware_info")
        if isinstance(latest_info, dict):
            desc = latest_info.get("desc")
            if isinstance(desc, str):
                return desc
        return None

    @property
    def update_percentage(self) -> int | None:
        """Update installation progress."""
        if self.data.get("upgrade_in_progress"):
            percent = self.data.get("upgrade_percent")
            if isinstance(percent, int):
                return percent
            if isinstance(percent, float):
                return int(percent)
        return None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install an update."""
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.ezviz_client.upgrade_device, self._serial
            )

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(
                f"Failed to update firmware on {self.name}"
            ) from err
