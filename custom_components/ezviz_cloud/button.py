"""Support for EZVIZ button controls."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, override

from pyezvizapi import EzvizClient
from pyezvizapi.constants import SupportExt
from pyezvizapi.devices.intercom import intercom_relays, unlock_intercom
from pyezvizapi.devices.snapshot import DeviceState
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity, EzvizInventoryEntity
from .errors import request_error
from .inventory import async_add_inventory_entities
from .runtime import EzvizConfigEntry
from .utility import passes_description_gates

PARALLEL_UPDATES = 1

PTZDirection = Literal["UP", "DOWN", "LEFT", "RIGHT"]
PTZ_DIRECTIONS: tuple[PTZDirection, PTZDirection, PTZDirection, PTZDirection] = (
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
)


class EzvizButtonEntityHandler:
    """Class to handle multi actions for button."""

    @staticmethod
    def press_ptz(
        pyezviz_client: EzvizClient, direction: PTZDirection, serial: str
    ) -> None:
        """Execute the button action for PTZ."""
        pyezviz_client.ptz_control(direction, serial, "START")
        pyezviz_client.ptz_control(direction, serial, "STOP")

    @staticmethod
    def make_ptz_method(direction: PTZDirection) -> Callable[[EzvizClient, str], None]:
        """Factory that returns a typed callable for a single PTZ direction."""

        def _run(client: EzvizClient, serial: str) -> None:
            EzvizButtonEntityHandler.press_ptz(client, direction, serial)

        return _run


@dataclass(frozen=True, kw_only=True)
class EzvizButtonEntityDescription(ButtonEntityDescription):
    """Describe a EZVIZ Button."""

    method: Callable[[EzvizClient, str], Any]
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None
    required_device_categories: tuple[str, ...] | None = None
    is_supported_fn: Callable[[dict[str, Any]], bool] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any], desc: EzvizButtonEntityDescription
) -> bool:
    """Return True if this button description is supported by the camera."""

    return passes_description_gates(
        camera_data,
        supported_ext_keys=desc.supported_ext_key,
        supported_ext_values=desc.supported_ext_value,
        required_device_categories=desc.required_device_categories,
        predicate=desc.is_supported_fn,
    )


BUTTON_ENTITIES: tuple[EzvizButtonEntityDescription, ...] = (
    *(
        EzvizButtonEntityDescription(
            key=f"ptz_{direction.lower()}",
            translation_key=f"ptz_{direction.lower()}",
            method=EzvizButtonEntityHandler.make_ptz_method(direction),
            supported_ext_key=str(SupportExt.SupportPtz.value),
            supported_ext_value=["1"],
        )
        for direction in PTZ_DIRECTIONS
    ),
    EzvizButtonEntityDescription(
        key="restart_device",
        device_class=ButtonDeviceClass.RESTART,
        translation_key="reboot_device",
        method=lambda pyezviz_client, serial: pyezviz_client.reboot_camera(serial),
        supported_ext_key=str(SupportExt.SupportRebootDevice.value),
        supported_ext_value=["1"],
    ),
    EzvizButtonEntityDescription(
        key="flip_image",
        translation_key="flip_image",
        method=lambda pyezviz_client, serial: pyezviz_client.flip_image(serial),
        supported_ext_key=str(SupportExt.SupportPtzCenterMirror.value),
        supported_ext_value=["1"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ button based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = entry.runtime_data.coordinator
    async_add_inventory_entities(
        coordinator, entry, async_add_entities, "camera", intercom_buttons
    )

    async_add_entities(
        EzvizButtonEntity(coordinator, serial, description)
        for serial, camera_data in coordinator.data.items()
        for description in BUTTON_ENTITIES
        if _is_desc_supported(camera_data, description)
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

    @override
    async def async_press(self) -> None:
        """Execute the button action."""
        try:
            await self.coordinator.async_execute(
                self.entity_description.method,
                self.coordinator.ezviz_client,
                self._serial,
            )

        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Press button", err) from err


def intercom_buttons(
    coordinator: EzvizDataUpdateCoordinator, serial: str, state: DeviceState
) -> list[EzvizIntercomUnlock]:
    """Create only local relays explicitly advertised by the SDK."""
    return [
        EzvizIntercomUnlock(coordinator, serial, relay.name)
        for relay in intercom_relays(state.as_dict())
    ]


class EzvizIntercomUnlock(EzvizInventoryEntity, ButtonEntity):
    """One-shot door/gate command with no inferred physical lock state."""

    def __init__(
        self, coordinator: EzvizDataUpdateCoordinator, serial: str, relay: str
    ) -> None:
        """Retain the relay role, never credentials or stale command metadata."""
        super().__init__(coordinator, serial)
        self._relay = relay
        self._attr_unique_id = f"{serial}_unlock_{relay}"
        self._attr_translation_key = f"unlock_{relay}"

    @override
    async def async_press(self) -> None:
        """Revalidate current capabilities before sending exactly one command."""
        if not self.available or self._relay not in ("door", "gate"):
            raise HomeAssistantError("EZVIZ intercom is unavailable")
        try:
            result = await self.coordinator.async_execute(
                unlock_intercom,
                self.coordinator.ezviz_client,
                self.serial,
                self._relay,
                self.data,
            )
            if result is False:
                raise HomeAssistantError("EZVIZ did not accept the unlock command")
        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Unlock intercom", err) from err
