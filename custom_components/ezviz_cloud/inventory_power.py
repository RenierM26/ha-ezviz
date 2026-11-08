"""Thin on/off adapters; no guessed brightness, temperature or energy scales."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity

from .entity import EzvizInventoryEntity
from .inventory import async_set_power


class EzvizInventoryLight(EzvizInventoryEntity, LightEntity):
    """An actual light bulb, separate from the existing camera floodlight."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.ONOFF}
    _attr_name = None

    def __init__(self, coordinator: Any, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_power"

    @property
    def is_on(self) -> bool | None:
        return self.device_state.is_on if self.device_state is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await async_set_power(self, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_set_power(self, False)


class EzvizInventoryPlug(EzvizInventoryEntity, SwitchEntity):
    """An on/off plug sharing the account inventory and freshness policy."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_name = None

    def __init__(self, coordinator: Any, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_power"

    @property
    def is_on(self) -> bool | None:
        return self.device_state.is_on if self.device_state is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await async_set_power(self, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_set_power(self, False)


def light_entities(coordinator: Any, serial: str, state: Any) -> list:
    items = state.values.get("featureItems", ())
    if (
        state.is_on is not None
        and state.values.get("productId")
        and any(item.get("itemKey") == "light_switch" for item in items)
    ):
        return [EzvizInventoryLight(coordinator, serial)]
    return []


def plug_entities(coordinator: Any, serial: str, state: Any) -> list:
    return [EzvizInventoryPlug(coordinator, serial)] if state.is_on is not None else []
