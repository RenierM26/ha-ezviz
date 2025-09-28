"""Support for EZVIZ number controls."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from pyezvizapi import EzvizClient
from pyezvizapi.constants import SupportExt
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

SCAN_INTERVAL = timedelta(seconds=3600)
PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class EzvizNumberEntityDescription(NumberEntityDescription):
    """Describe an EZVIZ Number entity."""

    supported_ext: str | None
    supported_ext_value: list[str]
    get_value: Callable[[dict[str, Any]], float | None]
    set_value: Callable[[EzvizClient, str, float], Any]
    translation_placeholders: dict[str, str] | None = None
    available_fn: Callable[[dict[str, Any]], bool] | None = None


DETECTION_SENSITIVITY_EXT = str(SupportExt.SupportSensibilityAdjust.value)
DETECTION_SENSITIVITY_VALUES = ["1", "3"]
DETECTION_TRANSLATION_KEY = "detection_sensibility"


def _support_ext_dict(camera_data: dict[str, Any]) -> dict[str, Any]:
    support_ext = camera_data.get("supportExt")
    return support_ext if isinstance(support_ext, dict) else {}


def _device_sub_category(camera_data: dict[str, Any]) -> str | None:
    sub_category = camera_data.get("device_sub_category")
    return str(sub_category) if isinstance(sub_category, str) else None


def _optionals_dict(camera_data: dict[str, Any]) -> dict[str, Any]:
    status = camera_data.get("STATUS")
    if not isinstance(status, dict):
        return {}
    optionals = status.get("optionals")
    return optionals if isinstance(optionals, dict) else {}


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _iter_algorithm_entries(camera_data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    optionals = _optionals_dict(camera_data)
    entries = optionals.get("AlgorithmInfo")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if isinstance(entry, dict):
            yield entry


def _iter_channel_algorithm_entries(
    camera_data: dict[str, Any], channel: int
) -> Iterable[dict[str, Any]]:
    for entry in _iter_algorithm_entries(camera_data):
        entry_channel = _coerce_int(entry.get("channel")) or 1
        if entry_channel == channel:
            yield entry


def _get_algorithm_value(
    camera_data: dict[str, Any], subtype: str, channel: int
) -> int | None:
    for entry in _iter_channel_algorithm_entries(camera_data, channel):
        if entry.get("SubType") != subtype:
            continue
        return _coerce_int(entry.get("Value"))
    return None


def _has_algorithm_subtype(
    camera_data: dict[str, Any], subtype: str, channel: int = 1
) -> bool:
    """Return True if AlgorithmInfo contains the given subtype and channel."""
    return _get_algorithm_value(camera_data, subtype, channel) is not None


def _support_ext_value(camera_data: dict[str, Any], ext_key: str) -> str | None:
    """Fetch a supportExt value as a string (if available)."""
    value = _support_ext_dict(camera_data).get(ext_key)
    return str(value) if value is not None else None


def _algorithm_value_getter(
    subtype: str, channel: int
) -> Callable[[dict[str, Any]], float | None]:
    def _getter(camera_data: dict[str, Any]) -> float | None:
        value = _get_algorithm_value(camera_data, subtype, channel)
        return float(value) if value is not None else None

    return _getter


def _algorithm_param_setter(
    subtype: str, channel: int
) -> Callable[[EzvizClient, str, float], Any]:
    def _setter(client: EzvizClient, serial: str, value: float) -> Any:
        return client.set_algorithm_param(serial, subtype, int(value), channel)

    return _setter


def _detection_setter(type_value: int) -> Callable[[EzvizClient, str, float], Any]:
    def _setter(client: EzvizClient, serial: str, value: float) -> Any:
        return client.set_detection_sensitivity(serial, 1, type_value, int(value))

    return _setter


STATIC_NUMBER_DESCRIPTIONS: tuple[EzvizNumberEntityDescription, ...] = (
    EzvizNumberEntityDescription(
        key=DETECTION_TRANSLATION_KEY,
        translation_key=DETECTION_TRANSLATION_KEY,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext=DETECTION_SENSITIVITY_EXT,
        supported_ext_value=DETECTION_SENSITIVITY_VALUES,
        get_value=_algorithm_value_getter("0", 1),
        set_value=_detection_setter(3),
        translation_placeholders={"channel_suffix": ""},
        available_fn=lambda data: _device_sub_category(data) == "C3A"
        and _has_algorithm_subtype(data, "0", 1)
        and _support_ext_value(data, DETECTION_SENSITIVITY_EXT) == "3",
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_0_1",
        translation_key="algorithm_sensitivity",
        native_min_value=1,
        native_max_value=6,
        native_step=1,
        supported_ext=DETECTION_SENSITIVITY_EXT,
        supported_ext_value=DETECTION_SENSITIVITY_VALUES,
        get_value=_algorithm_value_getter("0", 1),
        set_value=_detection_setter(0),
        translation_placeholders={"channel_suffix": ""},
        available_fn=lambda data: _has_algorithm_subtype(data, "0", 1)
        and _support_ext_value(data, DETECTION_SENSITIVITY_EXT) == "1",
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_3_1",
        translation_key="algorithm_param_pir",
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext=None,
        supported_ext_value=[],
        get_value=_algorithm_value_getter("3", 1),
        set_value=_algorithm_param_setter("3", 1),
        translation_placeholders={"subtype": "3"},
        available_fn=lambda data: _has_algorithm_subtype(data, "3", 1),
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_4_1",
        translation_key="algorithm_param_human",
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext=None,
        supported_ext_value=[],
        get_value=_algorithm_value_getter("4", 1),
        set_value=_algorithm_param_setter("4", 1),
        translation_placeholders={"subtype": "4"},
        available_fn=lambda data: _has_algorithm_subtype(data, "4", 1),
    ),
)


def _is_description_supported(
    camera_data: dict[str, Any], description: EzvizNumberEntityDescription
) -> bool:
    if description.available_fn and not description.available_fn(camera_data):
        return False
    if description.supported_ext is None:
        return True
    value = _support_ext_value(camera_data, description.supported_ext)
    if value is None:
        return False
    if not description.supported_ext_value:
        return True
    return value in description.supported_ext_value


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    entities: list[NumberEntity] = []

    for serial, camera_data in coordinator.data.items():
        for description in STATIC_NUMBER_DESCRIPTIONS:
            if not _is_description_supported(camera_data, description):
                continue
            entities.append(EzvizNumber(coordinator, serial, description))

    if entities:
        async_add_entities(entities)


class EzvizNumber(EzvizEntity, NumberEntity):
    """Generic EZVIZ number entity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    entity_description: EzvizNumberEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizNumberEntityDescription,
    ) -> None:
        """Initialize the generic EZVIZ number entity."""
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"
        if description.native_min_value is not None:
            self._attr_native_min_value = description.native_min_value
        if description.native_max_value is not None:
            self._attr_native_max_value = description.native_max_value
        if description.native_step is not None:
            self._attr_native_step = description.native_step
        if description.translation_placeholders:
            self._attr_translation_placeholders = description.translation_placeholders
        self._cached_value: float | None = description.get_value(self.data)

    @property
    def native_value(self) -> float | None:
        """Return the current numeric value from coordinator data."""
        value = self.entity_description.get_value(self.data)
        if value is not None:
            self._cached_value = value
        return self._cached_value

    async def async_set_native_value(self, value: float) -> None:
        """Send a new value to the device and refresh coordinator state."""
        try:
            await self.hass.async_add_executor_job(
                self.entity_description.set_value,
                self.coordinator.ezviz_client,
                self._serial,
                value,
            )
        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot set value for {self.entity_id}") from err

        self._cached_value = float(value)
        await self.coordinator.async_request_refresh()
