"""Support for EZVIZ light entity."""

from collections.abc import Callable
from dataclasses import dataclass, replace
import re
from typing import Any, override

from pyezvizapi import EzvizClient
from pyezvizapi.constants import DeviceSwitchType, SupportExt
from pyezvizapi.devices.snapshot import DeviceState, LightState
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
    LightEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .coordinator import EzvizDataUpdateCoordinator, camera_alarm_light_state
from .entity import EzvizEntity, EzvizInventoryEntity
from .errors import request_error
from .inventory import (
    async_add_camera_inventory_entities,
    async_add_inventory_entities,
    async_set_power,
)
from .runtime import EzvizConfigEntry
from .setting_writes import SettingCommand
from .utility import passes_description_gates, support_ext_dict

PARALLEL_UPDATES = 1

_ALARM_LIGHT_SUPPORT = str(SupportExt.SupportAlarmLight.value)
_FLASH_LAMP_SUPPORT = str(SupportExt.SupportFlashLamp.value)


@dataclass(frozen=True, kw_only=True)
class EzvizLightEntityDescription(LightEntityDescription):
    """EZVIZ light with gating + state/brightness getters + action callables."""

    # Getters from coordinator data:
    # - is_on_value: return on/off, or None if unknown
    # - brightness_value: return an int percent (0-100) or None if not available
    is_on_value: Callable[[dict[str, Any]], Any]
    brightness_value: Callable[[dict[str, Any]], int | None]

    # Brightness scaling (Home Assistant brightness range)
    brightness_range: tuple[int, int] = (1, 255)

    # Actions (plug different APIs here per light type)
    power_on: Callable[[EzvizClient, str], Any]
    power_off: Callable[[EzvizClient, str], Any]
    # set_brightness expects percent (0-100). If None, entity won't attempt to set brightness.
    set_brightness: Callable[[EzvizClient, str, int], Any] | None = None

    # Capability gating via camera_data["supportExt"]
    supported_ext_key: str | None = None
    # If provided, one of these raw strings must match supportExt value.
    # If omitted/None/empty, presence of the key is enough.
    supported_ext_value: list[str] | None = None
    is_supported_fn: Callable[[dict[str, Any]], bool] | None = None

    # Optional device-category gating via camera_data["device_category"]
    required_device_categories: tuple[str, ...] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any], desc: EzvizLightEntityDescription
) -> bool:
    """Return True if this light description is supported by the camera."""
    return passes_description_gates(
        camera_data,
        supported_ext_keys=desc.supported_ext_key,
        supported_ext_values=desc.supported_ext_value,
        required_device_categories=desc.required_device_categories,
        predicate=desc.is_supported_fn,
    )


def _supports_combined_view(data: dict[str, Any]) -> bool:
    """Check the app's advertised list of channels in a combined view."""
    channels = support_ext_dict(data).get(str(SupportExt.SupportMultiChannelType.value))
    return (
        isinstance(channels, str)
        and re.fullmatch(r"[1-9][0-9]*(?:-[1-9][0-9]*)+", channels) is not None
        and len(set(channels.split("-"))) > 1
    )


_ALARM_LIGHT = EzvizLightEntityDescription(
    key="Light",
    translation_key="light",
    is_on_value=camera_alarm_light_state,
    brightness_value=lambda d: d.get("alarm_light_luminance"),
    brightness_range=(1, 255),
    supported_ext_key=_ALARM_LIGHT_SUPPORT,
    supported_ext_value=["1"],
    power_on=lambda client, serial: client.switch_status(
        serial, DeviceSwitchType.ALARM_LIGHT.value, 1
    ),
    power_off=lambda client, serial: client.switch_status(
        serial, DeviceSwitchType.ALARM_LIGHT.value, 0
    ),
    set_brightness=lambda client, serial, percent: client.set_floodlight_brightness(
        serial, percent
    ),
)
LIGHTS: tuple[EzvizLightEntityDescription, ...] = (
    _ALARM_LIGHT,
    # The app sends switch 303 to channel zero from the combined view.
    replace(
        _ALARM_LIGHT,
        supported_ext_key=_FLASH_LAMP_SUPPORT,
        is_supported_fn=_supports_combined_view,
        set_brightness=None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ lights based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = entry.runtime_data.coordinator

    async_add_inventory_entities(
        coordinator, entry, async_add_entities, "light", light_entities
    )

    def camera_light(
        serial: str, data: dict[str, Any], existing: EzvizLight | None
    ) -> EzvizLight | None:
        # Both descriptions represent the same lamp; prefer legacy brightness.
        for desc in LIGHTS:
            if _is_desc_supported(data, desc):
                if existing is None:
                    return EzvizLight(coordinator, serial, desc)
                existing.set_description(desc)
                return None
        if (
            existing is not None
            and (active_key := existing.entity_description.supported_ext_key)
            is not None
        ):
            # An omitted active flag does not revoke the last observed support.
            if str(support_ext_dict(data).get(active_key)).strip() == "0":
                existing.disable_control()
        return None

    async_add_camera_inventory_entities(
        coordinator, entry, async_add_entities, camera_light
    )


class EzvizLight(EzvizEntity, LightEntity):
    """Representation of an EZVIZ light.

    Binds a camera's light capability to HA using an entity description:
    - unique_id: f"{serial}_{description.key}" (canonical, name-free)
    - initial state snapshot from coordinator via is_on_value/brightness_value
    - actions routed through description callables (on/off/brightness)
    """

    _attr_has_entity_name = True
    entity_description: EzvizLightEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizLightEntityDescription,
    ) -> None:
        """Initialize the EZVIZ light entity and pre-populate state from snapshot."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{description.key}"
        self.set_description(description)

    def set_description(self, description: EzvizLightEntityDescription) -> None:
        """Apply the poll's capabilities before the entity writes its state."""
        self._control_supported = True
        if getattr(self, "entity_description", None) is description:
            return
        self.entity_description = description
        self._attr_color_mode = (
            ColorMode.BRIGHTNESS
            if description.set_brightness is not None
            else ColorMode.ONOFF
        )
        self._attr_supported_color_modes = {self._attr_color_mode}

    def disable_control(self) -> None:
        """Keep the entity registered when its control route is revoked."""
        self._control_supported = False

    @property
    @override
    def available(self) -> bool:
        """Report availability only if the camera and its light are supported."""
        return self._control_supported and super().available

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the observed power, or unknown without a shared observation."""
        if (
            self.entity_description.is_on_value is camera_alarm_light_state
            and self.serial in self.coordinator.camera_alarm_lights
        ):
            return self.coordinator.camera_alarm_lights[self.serial]
        value = self.entity_description.is_on_value(self.data)
        return None if value is None else bool(value)

    @property
    @override
    def brightness(self) -> int | None:
        """Read brightness without changing the accepted camera snapshot."""
        if self.color_mode == ColorMode.ONOFF:
            return None
        percent = self.entity_description.brightness_value(self.data)
        return (
            round(
                percentage_to_ranged_value(
                    self.entity_description.brightness_range, percent
                )
            )
            if isinstance(percent, int)
            else None
        )

    def _control_description(self) -> EzvizLightEntityDescription:
        """Validate support under the setting write lock, before dispatch."""
        if not self._control_supported:
            raise HomeAssistantError("EZVIZ Alarm light is unavailable")
        return self.entity_description

    def _prepare_power(self, on: bool) -> SettingCommand:
        description = self._control_description()
        return SettingCommand(
            description.power_on if on else description.power_off,
            (self.coordinator.ezviz_client, self.serial),
        )

    def _prepare_brightness(self, brightness: int) -> SettingCommand:
        description = self._control_description()
        if description.set_brightness is None:
            raise HomeAssistantError("EZVIZ Alarm light does not support brightness")
        percent = int(
            ranged_value_to_percentage(description.brightness_range, brightness)
        )
        return SettingCommand(
            description.set_brightness,
            (self.coordinator.ezviz_client, self.serial, percent),
        )

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set related values before the shared delayed observation."""
        try:
            if (
                ATTR_BRIGHTNESS in kwargs
                and self.entity_description.set_brightness is not None
            ):
                await self.async_write_setting(
                    prepare=lambda: self._prepare_brightness(kwargs[ATTR_BRIGHTNESS])
                )
            await self.async_write_setting(prepare=lambda: self._prepare_power(True))
        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Turn on light", err) from err

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off through the shared setting writer."""
        try:
            await self.async_write_setting(prepare=lambda: self._prepare_power(False))
        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Turn off light", err) from err


class EzvizInventoryLight(EzvizInventoryEntity, LightEntity):
    """An actual light bulb, separate from the existing camera floodlight."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_name = None

    def __init__(self, coordinator: EzvizDataUpdateCoordinator, serial: str) -> None:
        """Initialize the owner and its cached dependencies."""
        super().__init__(coordinator, serial)
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_unique_id = f"{serial}_power"

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the known power state, or unknown when unavailable."""
        state = self.device_state
        return state.is_on if isinstance(state, LightState) else None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on power through the shared command owner."""
        await async_set_power(self, True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off power through the shared command owner."""
        await async_set_power(self, False)


def light_entities(
    coordinator: EzvizDataUpdateCoordinator, serial: str, state: DeviceState
) -> list[EzvizInventoryLight]:
    """Create an on/off light only with observed command metadata."""
    items = state.values.get("featureItems", ())
    if (
        isinstance(state, LightState)
        and state.is_on is not None
        and state.values.get("productId")
        and any(item.get("itemKey") == "light_switch" for item in items)
    ):
        return [EzvizInventoryLight(coordinator, serial)]
    return []
