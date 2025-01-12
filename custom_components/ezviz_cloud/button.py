"""Support for EZVIZ button controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyezvizapi import EzvizClient
from pyezvizapi.constants import SupportExt
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

PARALLEL_UPDATES = 1


class EzvizButtonEntityHandler:
    """Class to handle multi actions for button."""

    @staticmethod
    def press_ptz(pyezviz_client: EzvizClient, direction: str, serial: str) -> Any:
        """Execute the button action for PTZ."""
        pyezviz_client.ptz_control(direction, serial, "START")
        pyezviz_client.ptz_control(direction, serial, "STOP")


@dataclass(frozen=True, kw_only=True)
class EzvizButtonEntityDescription(ButtonEntityDescription):
    """Describe a EZVIZ Button."""

    method: Callable[[EzvizClient, str], Any]
    supported_ext: str


BUTTON_ENTITIES = (
    EzvizButtonEntityDescription(
        key="ptz_up",
        translation_key="ptz_up",
        method=lambda pyezviz_client, serial: EzvizButtonEntityHandler.press_ptz(
            pyezviz_client, "UP", serial
        ),
        supported_ext=str(SupportExt.SupportPtz.value),
    ),
    EzvizButtonEntityDescription(
        key="ptz_down",
        translation_key="ptz_down",
        method=lambda pyezviz_client, serial: EzvizButtonEntityHandler.press_ptz(
            pyezviz_client, "DOWN", serial
        ),
        supported_ext=str(SupportExt.SupportPtz.value),
    ),
    EzvizButtonEntityDescription(
        key="ptz_left",
        translation_key="ptz_left",
        method=lambda pyezviz_client, serial: EzvizButtonEntityHandler.press_ptz(
            pyezviz_client, "LEFT", serial
        ),
        supported_ext=str(SupportExt.SupportPtz.value),
    ),
    EzvizButtonEntityDescription(
        key="ptz_right",
        translation_key="ptz_right",
        method=lambda pyezviz_client, serial: EzvizButtonEntityHandler.press_ptz(
            pyezviz_client, "RIGHT", serial
        ),
        supported_ext=str(SupportExt.SupportPtz.value),
    ),
    EzvizButtonEntityDescription(
        key="restart_device",
        device_class=ButtonDeviceClass.RESTART,
        translation_key="reboot_device",
        method=lambda pyezviz_client, serial: pyezviz_client.reboot_camera(serial),
        supported_ext=str(SupportExt.SupportRebootDevice.value),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ button based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    # Add button entities if supportExt value indicates Button capbility.
    # Could be missing or "0" for unsupported.
    # If present with value of "1" then add button entity.

    async_add_entities(
        EzvizButtonEntity(coordinator, camera, entity_description)
        for camera in coordinator.data
        for capability, value in coordinator.data[camera]["supportExt"].items()
        for entity_description in BUTTON_ENTITIES
        if capability == entity_description.supported_ext
        if value == "1"
    )


class EzvizButtonEntity(EzvizEntity, ButtonEntity):
    """Representation of a EZVIZ button entity."""

    entity_description: EzvizButtonEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{description.key}"
        self.entity_description = description

    def press(self) -> None:
        """Execute the button action."""
        try:
            self.entity_description.method(self.coordinator.ezviz_client, self._serial)

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot perform action on {self.name}") from err
