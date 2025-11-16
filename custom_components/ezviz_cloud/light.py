"""Support for EZVIZ light entity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from pyezvizapi import EzvizClient
from pyezvizapi.constants import DeviceCatagories, DeviceSwitchType, SupportExt
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
    LightEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .utility import CAMERA_DEVICE_CATEGORIES, coerce_int, passes_description_gates

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
        required_device_categories=CAMERA_DEVICE_CATEGORIES,
        power_on=lambda client, serial: client.switch_light_status(serial, 1),
        power_off=lambda client, serial: client.switch_light_status(serial, 0),
        set_brightness=lambda client, serial, percent: client.set_brightness(
            serial, percent
        ),
    ),
    EzvizLightEntityDescription(
        key="light_bulb",
        translation_key="light_bulb",
        is_on_value=lambda d: d.get("is_on")
        if d.get("device_category") == DeviceCatagories.LIGHTING.value
        else (d.get("switches") or {}).get(DeviceSwitchType.ALARM_LIGHT.value),
        brightness_value=lambda d: d.get("alarm_light_luminance"),
        supported_ext_key=None,
        required_device_categories=(DeviceCatagories.LIGHTING.value,),
        power_on=lambda client, serial: client.switch_light_status(serial, 1),
        power_off=lambda client, serial: client.switch_light_status(serial, 0),
        set_brightness=lambda client, serial, percent: client.set_brightness(
            serial, percent
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ lights based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

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
        self._attr_is_on = bool(self.entity_description.is_on_value(self.data))
        percent = coerce_int(self.entity_description.brightness_value(self.data))
        self._attr_brightness = (
            round(
                percentage_to_ranged_value(
                    self.entity_description.brightness_range, percent
                )
            )
            if percent is not None
            else None
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on light (optionally set brightness first)."""
        try:
            # If brightness provided, set luminance first (1-255 -> 0-100 %)
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
                ok = await self.hass.async_add_executor_job(
                    self.entity_description.set_brightness,
                    self.coordinator.ezviz_client,
                    self._serial,
                    percent,
                )
                if ok:
                    self._attr_brightness = kwargs[ATTR_BRIGHTNESS]

            # Then power on
            ok = await self.hass.async_add_executor_job(
                self.entity_description.power_on,
                self.coordinator.ezviz_client,
                self._serial,
            )
            if ok:
                self._attr_is_on = True
                self.async_write_ha_state()

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Failed to turn on light {self.name}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off light."""
        try:
            ok = await self.hass.async_add_executor_job(
                self.entity_description.power_off,
                self.coordinator.ezviz_client,
                self._serial,
            )
            if ok:
                self._attr_is_on = False
                self.async_write_ha_state()

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Failed to turn off light {self.name}") from err

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = bool(self.entity_description.is_on_value(self.data))

        percent = coerce_int(self.entity_description.brightness_value(self.data))
        self._attr_brightness = (
            round(
                percentage_to_ranged_value(
                    self.entity_description.brightness_range, percent
                )
            )
            if percent is not None
            else None
        )

        super()._handle_coordinator_update()
