"""Translate SDK-shaped bulb metadata into HA ranges and ordered commands."""

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import re
from typing import TYPE_CHECKING, Any

from pyezvizapi.devices.snapshot import LightState
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.color import brightness_to_value

from .setting_writes import SettingCommand

if TYPE_CHECKING:
    from .light import EzvizInventoryLight

_RGB = re.compile(r"#[0-9a-fA-F]{6}")


def _integer(value: Any) -> int | None:
    """Do not turn missing values, booleans or fractional values into readings."""
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def bulb_items(values: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Read the SDK's structured items, never reparse the vendor FEATURE payload."""
    items: dict[str, Mapping[str, Any]] = {}
    for item in values.get("featureItems", ()):
        if isinstance(item, Mapping) and isinstance(key := item.get("itemKey"), str):
            # Match the SDK's get_feature_item: first matching item wins.
            items.setdefault(key, item)
    return items


def _description(item: Mapping[str, Any] | None) -> Any:
    """Decode the SDK-provided capability description, not a second wire parser."""
    if item is None or item.get("transportType") != "rw":
        return None
    value = item.get("dataDesc")
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        return json.loads(value)
    except ValueError, RecursionError:
        return None


@dataclass(frozen=True)
class BulbRange:
    """A reported integer range with an unscaled unit and aligned step."""

    minimum: int
    maximum: int
    step: int

    def observed(self, value: Any) -> int | None:
        """Return known readings without inventing or clamping device state."""
        number = _integer(value)
        if number is not None and self.minimum <= number <= self.maximum:
            return number
        return None

    def target(self, value: float) -> int:
        """Clamp and quantize a requested value, not an observed one."""
        value = min(self.maximum, max(self.minimum, value))
        return self.minimum + round((value - self.minimum) / self.step) * self.step


def _range(item: Mapping[str, Any] | None, unit: str) -> BulbRange | None:
    desc = _description(item)
    if item is None or item.get("dataType") != "num" or not isinstance(desc, Mapping):
        return None
    low, high, step = (
        _integer(desc.get(key)) for key in ("range_from", "range_to", "interval")
    )
    if (
        low is None
        or high is None
        or step is None
        or not 0 < low < high
        or not 0 < step <= high - low
        or (high - low) % step
        or _integer(desc.get("multiple")) != 0
        or desc.get("unit") != unit
    ):
        return None
    return BulbRange(low, high, step)


@dataclass(frozen=True)
class BulbCapabilities:
    """Only the qualified HA-facing feature shapes; not a model whitelist."""

    brightness: BulbRange | None
    temperature: BulbRange | None
    rgb: bool
    mode: str | None


def bulb_capabilities(values: Mapping[str, Any]) -> BulbCapabilities:
    """Require explicit writable metadata and known units/scales for extra UI."""
    items = bulb_items(values)
    brightness = _range(items.get("brightness"), "")
    if brightness and (brightness.minimum, brightness.maximum) != (1, 100):
        brightness = None
    mode = items.get("light_mode", {})
    choices = _description(mode) if mode.get("dataType") == "enum" else None
    if not isinstance(choices, list) or not all(isinstance(x, str) for x in choices):
        choices = []
    temperature = _range(items.get("color_temperature"), "k")
    if (
        brightness is None
        or "white" not in choices
        or temperature is None
        or not 1000 <= temperature.minimum < temperature.maximum <= 40000
    ):
        temperature = None
    rgb_item = items.get("color_rgb", {})
    rgb_value = rgb_item.get("dataValue")
    rgb = (
        brightness is not None
        and "color" in choices
        and rgb_item.get("dataType") == "char"
        and rgb_item.get("transportType") == "rw"
        and isinstance(rgb_value, str)
        and _RGB.fullmatch(rgb_value) is not None
    )
    mode_value = mode.get("dataValue")
    return BulbCapabilities(
        brightness,
        temperature,
        rgb,
        mode_value if isinstance(mode_value, str) else None,
    )


def bulb_rgb(values: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """Decode only the recorded six-digit RGB representation."""
    value = bulb_items(values).get("color_rgb", {}).get("dataValue")
    if isinstance(value, str) and _RGB.fullmatch(value):
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    return None


def _write_features(
    client: Any, serial: str, product: str, commands: tuple[tuple[str, Any], ...]
) -> bool:
    """Keep the complete sequence under the existing owned account write lock."""
    for key, value in commands:
        if client.set_device_feature_by_key(serial, product, value, key) is not True:
            raise PyEzvizError("Bulb feature command was not accepted")
    return True


def prepare_bulb_turn_on(
    entity: EzvizInventoryLight, request: Mapping[str, Any]
) -> SettingCommand:
    """Revalidate current metadata under the write lock before any command."""
    state = entity.device_state
    if not entity.available or not isinstance(state, LightState):
        raise HomeAssistantError("EZVIZ light is unavailable")
    product = state.values.get("productId")
    if not isinstance(product, str) or not product:
        raise ServiceValidationError("Light has no observed command metadata")
    caps = bulb_capabilities(state.values)
    commands: list[tuple[str, Any]] = []
    if ATTR_COLOR_TEMP_KELVIN in request and ATTR_RGB_COLOR in request:
        raise ServiceValidationError("Choose either RGB or colour temperature")
    if ATTR_COLOR_TEMP_KELVIN in request:
        kelvin = _integer(request[ATTR_COLOR_TEMP_KELVIN])
        if caps.temperature is None or kelvin is None or kelvin <= 0:
            raise ServiceValidationError(
                "Light has no supported Kelvin range or valid target"
            )
        # Always select the requested mode: a recent accepted command may have
        # changed it even when the last cloud observation still shows the old one.
        commands.extend(
            (
                ("light_mode", "white"),
                ("color_temperature", caps.temperature.target(kelvin)),
            )
        )
    if ATTR_RGB_COLOR in request:
        rgb = request[ATTR_RGB_COLOR]
        if (
            not caps.rgb
            or not isinstance(rgb, (list, tuple))
            or len(rgb) != 3
            or any(type(x) is not int or not 0 <= x <= 255 for x in rgb)
        ):
            raise ServiceValidationError(
                "Light has no supported RGB feature or valid target"
            )
        commands.extend(
            (
                ("light_mode", "color"),
                ("color_rgb", "#" + "".join(f"{x:02X}" for x in rgb)),
            )
        )
    if ATTR_BRIGHTNESS in request:
        level = _integer(request[ATTR_BRIGHTNESS])
        if caps.brightness is None or level is None or not 1 <= level <= 255:
            raise ServiceValidationError(
                "Light has no supported brightness range or valid target"
            )
        commands.append(
            ("brightness", caps.brightness.target(brightness_to_value((1, 100), level)))
        )
    commands.append(("light_switch", True))
    return SettingCommand(
        _write_features,
        (entity.coordinator.ezviz_client, entity.serial, product, tuple(commands)),
    )
