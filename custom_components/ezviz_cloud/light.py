"""Support for EZVIZ light entity."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, override

from pyezvizapi import EzvizClient
from pyezvizapi.constants import DeviceSwitchType, SupportExt
from pyezvizapi.devices.channels import CameraChannel
from pyezvizapi.devices.observation import power_state
from pyezvizapi.devices.payload import switch_rows
from pyezvizapi.devices.snapshot import DeviceState, LightState
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
    LightEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .channels import async_add_channel_entities, channel_available
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity, EzvizInventoryEntity
from .errors import request_error
from .inventory import async_add_inventory_entities, async_set_power
from .runtime import EzvizConfigEntry
from .setting_writes import SettingCommand
from .utility import passes_description_gates

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizLightEntityDescription(LightEntityDescription):
    """EZVIZ light with gating + state/brightness getters + action callables."""

    # Getters from coordinator data:
    # - is_on_value: return a truthy/falsey value indicating on/off (or None if unknown)
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
        predicate=None,
    )


LIGHTS: tuple[EzvizLightEntityDescription, ...] = (
    EzvizLightEntityDescription(
        key="Light",
        translation_key="light",
        is_on_value=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.ALARM_LIGHT.value
        ),
        brightness_value=lambda d: d.get("alarm_light_luminance"),
        brightness_range=(1, 255),
        supported_ext_key=str(SupportExt.SupportAlarmLight.value),
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

    async_add_entities(
        EzvizLight(coordinator, serial, desc)
        for serial, camera_data in coordinator.data.items()
        for desc in LIGHTS
        if _is_desc_supported(camera_data, desc)
    )

    async_add_channel_entities(
        coordinator,
        entry,
        async_add_entities,
        lambda serial, channel: _warning_light_entities(coordinator, serial, channel),
    )


def _warning_light_state(data: dict[str, Any], channel: CameraChannel) -> bool | None:
    """Apply the SDK warning-light read-back rules to the accepted inventory."""
    rows = switch_rows(data.get("SWITCH"), channel_no=channel.channel_no)
    row = rows.get(DeviceSwitchType.ALARM_LIGHT.value)
    if row is None or "channelNo" not in row:
        return None
    return power_state(row.get("enable"))


def _warning_light_entities(
    coordinator: EzvizDataUpdateCoordinator, serial: str, channel: CameraChannel
) -> list[EzvizWarningLight]:
    data = coordinator.device_data(serial)
    if (
        channel.channel_no > 0
        and _is_desc_supported(data, LIGHTS[0])
        and _warning_light_state(data, channel) is not None
    ):
        return [EzvizWarningLight(coordinator, serial, channel)]
    return []


class EzvizWarningLight(EzvizEntity, LightEntity):
    """On/off warning light for one explicitly reported lens."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_translation_key = "lens_warning_light"

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        channel: CameraChannel,
    ) -> None:
        """Bind the entity to the stable video route, not its inventory position."""
        super().__init__(coordinator, serial)
        self._channel = channel
        self._attr_unique_id = f"{serial}_warning_channel_{channel.key}"
        self._attr_translation_placeholders = {"channel": channel.local_index}
        self._attr_supported_color_modes = {ColorMode.ONOFF}

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the actual state of this channel only."""
        return _warning_light_state(self.data, self._channel)

    @property
    @override
    def available(self) -> bool:
        """Require current lens metadata and explicit switch state."""
        return (
            channel_available(self.coordinator, self.serial, self._channel.key)
            and _is_desc_supported(self.data, LIGHTS[0])
            and self.is_on is not None
        )

    async def _async_set_power(self, on: bool) -> None:
        def prepare() -> SettingCommand:
            if not self.available:
                raise ServiceValidationError("EZVIZ lens warning light is unavailable")
            return SettingCommand(
                self.coordinator.ezviz_client.switch_light_status,
                (self.serial, int(on), self._channel.channel_no),
            )

        try:
            await self.async_write_setting(prepare=prepare)
        except (PyEzvizError, OSError) as err:
            raise request_error("Set lens warning light", err) from err

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on this lens warning light."""
        await self._async_set_power(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off this lens warning light."""
        await self._async_set_power(False)


class EzvizLight(EzvizEntity, LightEntity):
    """Representation of an EZVIZ light.

    Binds a camera's light capability to HA using an entity description:
    - unique_id: f"{serial}_{description.key}" (canonical, name-free)
    - initial state snapshot from coordinator via is_on_value/brightness_value
    - actions routed through description callables (on/off/brightness)
    """

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_has_entity_name = True
    _SUPPORTED_COLOR_MODES: ClassVar[set[ColorMode]] = {ColorMode.BRIGHTNESS}
    entity_description: EzvizLightEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizLightEntityDescription,
    ) -> None:
        """Initialize the EZVIZ light entity and pre-populate state from snapshot."""
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_supported_color_modes = self._SUPPORTED_COLOR_MODES

    @property
    @override
    def is_on(self) -> bool:
        """Return the SDK-observed light power."""
        return bool(self.entity_description.is_on_value(self.data))

    @property
    @override
    def brightness(self) -> int | None:
        """Read brightness without changing the accepted camera snapshot."""
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

    async def _async_power(self, on: bool) -> None:
        await self.async_write_setting(
            self.entity_description.power_on
            if on
            else self.entity_description.power_off,
            self.coordinator.ezviz_client,
            self.serial,
        )

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set related values before the shared delayed observation."""
        try:
            if (
                ATTR_BRIGHTNESS in kwargs
                and self.entity_description.set_brightness is not None
            ):
                percent = int(
                    ranged_value_to_percentage(
                        self.entity_description.brightness_range,
                        kwargs[ATTR_BRIGHTNESS],
                    )
                )
                await self.async_write_setting(
                    self.entity_description.set_brightness,
                    self.coordinator.ezviz_client,
                    self.serial,
                    percent,
                )
            await self._async_power(True)
        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Turn on light", err) from err

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off through the shared setting writer."""
        try:
            await self._async_power(False)
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
