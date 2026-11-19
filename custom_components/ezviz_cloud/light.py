"""Support for EZVIZ light entity."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, ClassVar, override

from pyezvizapi import EzvizClient
from pyezvizapi.constants import DeviceSwitchType, SupportExt
from pyezvizapi.devices.snapshot import DeviceState, LightState
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.color import value_to_brightness
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .bulb import BulbCapabilities, bulb_capabilities, bulb_rgb, prepare_bulb_turn_on
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity, EzvizInventoryEntity
from .errors import request_error
from .inventory import async_add_inventory_entities, async_set_power
from .runtime import EzvizConfigEntry
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
    """A bulb with controls gated by observed SDK feature metadata."""

    _attr_name = None

    def __init__(self, coordinator: EzvizDataUpdateCoordinator, serial: str) -> None:
        """Initialize the owner without caching mutable feature metadata."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_power"

    @property
    def _bulb_values(self) -> Mapping[str, Any]:
        state = self.device_state
        return (
            state.values
            if isinstance(state, LightState) and state.kind == "light"
            else {}
        )

    @property
    def _capabilities(self) -> BulbCapabilities:
        return bulb_capabilities(self._bulb_values)

    @property
    @override
    def available(self) -> bool:
        """A retained entity cannot control a newly classified different family."""
        return bool(self._bulb_values) and super().available

    @property
    @override
    def is_on(self) -> bool | None:
        """Return known power, never an accepted command's target."""
        state = self.device_state
        return (
            state.is_on
            if isinstance(state, LightState) and state.kind == "light"
            else None
        )

    @property
    @override
    def supported_color_modes(self) -> set[ColorMode]:
        """Re-evaluate capabilities after each accepted SDK snapshot."""
        caps = self._capabilities
        modes = set()
        if caps.temperature:
            modes.add(ColorMode.COLOR_TEMP)
        if caps.rgb:
            modes.add(ColorMode.RGB)
        if modes:
            return modes
        return {ColorMode.BRIGHTNESS} if caps.brightness else {ColorMode.ONOFF}

    @property
    @override
    def color_mode(self) -> ColorMode:
        """Do not claim scene/music or unknown device modes are white/RGB."""
        modes = self.supported_color_modes
        if modes == {ColorMode.ONOFF}:
            return ColorMode.ONOFF
        if modes == {ColorMode.BRIGHTNESS}:
            return ColorMode.BRIGHTNESS
        mode = self._capabilities.mode
        if mode == "white" and ColorMode.COLOR_TEMP in modes:
            return ColorMode.COLOR_TEMP
        if mode == "color" and ColorMode.RGB in modes:
            return ColorMode.RGB
        return ColorMode.UNKNOWN

    @property
    @override
    def brightness(self) -> int | None:
        """Convert the known percentage to HA's 1-255 scale."""
        feature = self._capabilities.brightness
        value = (
            feature.observed(self._bulb_values.get("brightness")) if feature else None
        )
        return value_to_brightness((1, 100), value) if value is not None else None

    @property
    @override
    def color_temp_kelvin(self) -> int | None:
        """Return a known active white-mode temperature, without scaling guesses."""
        feature = self._capabilities.temperature
        if feature and self.color_mode == ColorMode.COLOR_TEMP:
            return feature.observed(self._bulb_values.get("color_temperature"))
        return None

    @property
    @override
    def min_color_temp_kelvin(self) -> int:
        feature = self._capabilities.temperature
        return feature.minimum if feature else super().min_color_temp_kelvin

    @property
    @override
    def max_color_temp_kelvin(self) -> int:
        feature = self._capabilities.temperature
        return feature.maximum if feature else super().max_color_temp_kelvin

    @property
    @override
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Do not expose a stale RGB field as the active white-mode colour."""
        return bulb_rgb(self._bulb_values) if self.color_mode == ColorMode.RGB else None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Apply one serialized feature sequence, then request observed refresh."""
        if type(kwargs.get(ATTR_BRIGHTNESS)) is int and kwargs[ATTR_BRIGHTNESS] == 0:
            await self.async_turn_off()
            return
        request = {
            key: deepcopy(kwargs[key])
            for key in (ATTR_BRIGHTNESS, ATTR_COLOR_TEMP_KELVIN, ATTR_RGB_COLOR)
            if key in kwargs
        }
        if not request:
            await async_set_power(self, True)
            return

        async def operation() -> None:
            try:
                await self.async_write_setting(
                    prepare=lambda: prepare_bulb_turn_on(self, request)
                )
            except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
                # Some earlier properties may already have been accepted. Observe
                # the result even if the caller canceled; never retry or roll back.
                self.coordinator.settings.async_schedule_refresh()
                raise request_error("Set bulb light", err) from err

        await self.coordinator.async_execute_owned(operation)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off through the existing shared command owner."""
        await async_set_power(self, False)


def light_entities(
    coordinator: EzvizDataUpdateCoordinator, serial: str, state: DeviceState
) -> list[EzvizInventoryLight]:
    """Create a bulb only with observed power and command metadata."""
    items = state.values.get("featureItems", ())
    if (
        isinstance(state, LightState)
        and state.is_on is not None
        and state.values.get("productId")
        and any(item.get("itemKey") == "light_switch" for item in items)
    ):
        return [EzvizInventoryLight(coordinator, serial)]
    return []
