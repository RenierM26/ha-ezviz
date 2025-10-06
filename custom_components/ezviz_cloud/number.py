"""Support for EZVIZ number controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from pyezvizapi import EzvizClient
from pyezvizapi.constants import SupportExt
from pyezvizapi.exceptions import HTTPError, PyEzvizError
from pyezvizapi.feature import (
    get_algorithm_value,
    has_algorithm_subtype,
    night_vision_duration_value,
    night_vision_luminance_value,
    night_vision_mode_value,
    night_vision_payload,
    resolve_channel,
    support_ext_value,
)

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .utility import device_model

SCAN_INTERVAL = timedelta(seconds=3600)
PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class EzvizNumberEntityDescription(NumberEntityDescription):
    """Describe an EZVIZ Number entity."""

    supported_ext: str | tuple[str, ...] | None
    supported_ext_value: list[str]
    get_value: Callable[[dict[str, Any]], float | None]
    set_value: Callable[[EzvizClient, str, float, dict[str, Any]], Any]
    available_fn: Callable[[dict[str, Any]], bool] | None = None


DETECTION_SENSITIVITY_EXT = str(SupportExt.SupportSensibilityAdjust.value)
DETECTION_SENSITIVITY_VALUES = ["1", "3"]
DETECTION_TRANSLATION_KEY = "detection_sensibility"


def _algorithm_value_getter(
    subtype: str, channel: int
) -> Callable[[dict[str, Any]], float | None]:
    def _getter(camera_data: dict[str, Any]) -> float | None:
        value = get_algorithm_value(camera_data, subtype, channel)
        return float(value) if value is not None else None

    return _getter


def _algorithm_param_setter(
    subtype: str, channel: int
) -> Callable[[EzvizClient, str, float, dict[str, Any]], Any]:
    def _setter(
        client: EzvizClient,
        serial: str,
        value: float,
        _camera_data: dict[str, Any],
    ) -> Any:
        return client.set_algorithm_param(serial, subtype, int(value), channel)

    return _setter


def _detection_setter(
    type_value: int,
) -> Callable[[EzvizClient, str, float, dict[str, Any]], Any]:
    def _setter(
        client: EzvizClient,
        serial: str,
        value: float,
        _camera_data: dict[str, Any],
    ) -> Any:
        return client.set_detection_sensitivity(serial, 1, type_value, int(value))

    return _setter


def _night_vision_luminance_setter() -> Callable[
    [EzvizClient, str, float, dict[str, Any]], Any
]:
    def _setter(
        client: EzvizClient,
        serial: str,
        value: float,
        camera_data: dict[str, Any],
    ) -> Any:
        payload = night_vision_payload(
            camera_data,
            mode=night_vision_mode_value(camera_data),
            luminance=int(round(value)),
        )
        client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "NightVision_Model",
            payload,
        )

    return _setter


def _night_vision_duration_setter() -> Callable[
    [EzvizClient, str, float, dict[str, Any]], Any
]:
    def _setter(
        client: EzvizClient,
        serial: str,
        value: float,
        camera_data: dict[str, Any],
    ) -> Any:
        payload = night_vision_payload(
            camera_data,
            mode=night_vision_mode_value(camera_data),
            duration=int(round(value)),
        )
        client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "NightVision_Model",
            payload,
        )

    return _setter


STATIC_NUMBER_DESCRIPTIONS: tuple[EzvizNumberEntityDescription, ...] = (
    EzvizNumberEntityDescription(
        key=DETECTION_TRANSLATION_KEY,
        translation_key=DETECTION_TRANSLATION_KEY,
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext=DETECTION_SENSITIVITY_EXT,
        supported_ext_value=DETECTION_SENSITIVITY_VALUES,
        get_value=_algorithm_value_getter("0", 1),
        set_value=_detection_setter(3),
        available_fn=lambda data: device_model(data) == "C3A"
        and has_algorithm_subtype(data, "0", 1)
        and support_ext_value(data, DETECTION_SENSITIVITY_EXT) == "3",
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_0_1",
        translation_key="algorithm_sensitivity",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=6,
        native_step=1,
        supported_ext=DETECTION_SENSITIVITY_EXT,
        supported_ext_value=["1"],
        get_value=_algorithm_value_getter("0", 1),
        set_value=_detection_setter(0),
        available_fn=lambda data: has_algorithm_subtype(data, "0", 1)
        and support_ext_value(data, DETECTION_SENSITIVITY_EXT) == "1",
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_3_1",
        translation_key="algorithm_param_pir",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext=str(SupportExt.SupportDetectAreaUnderDefencetype.value),
        supported_ext_value=[],
        get_value=_algorithm_value_getter("3", 1),
        set_value=_algorithm_param_setter("3", 1),
        available_fn=lambda data: has_algorithm_subtype(data, "3", 1),
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_4_1",
        translation_key="algorithm_param_human",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext=str(SupportExt.SupportDetectAreaUnderDefencetype.value),
        supported_ext_value=[],
        get_value=_algorithm_value_getter("4", 1),
        set_value=_algorithm_param_setter("4", 1),
        available_fn=lambda data: has_algorithm_subtype(data, "4", 1),
    ),
    EzvizNumberEntityDescription(
        key="night_vision_luminance",
        translation_key="night_vision_luminance",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        supported_ext=(
            str(SupportExt.SupportNightVisionMode.value),
            str(SupportExt.SupportSmartNightVision.value),
        ),
        supported_ext_value=[],
        get_value=lambda data: float(night_vision_luminance_value(data)),
        set_value=_night_vision_luminance_setter(),
    ),
    EzvizNumberEntityDescription(
        key="night_vision_duration",
        translation_key="night_vision_duration",
        entity_category=EntityCategory.CONFIG,
        native_min_value=15,
        native_max_value=120,
        native_step=5,
        native_unit_of_measurement="s",
        supported_ext=str(SupportExt.SupportIntelligentNightVisionDuration.value),
        supported_ext_value=[],
        get_value=lambda data: float(night_vision_duration_value(data)),
        set_value=_night_vision_duration_setter(),
    ),
)


def _is_description_supported(
    camera_data: dict[str, Any], description: EzvizNumberEntityDescription
) -> bool:
    if description.supported_ext is None:
        return True
    keys: tuple[str, ...]
    if isinstance(description.supported_ext, tuple):
        keys = description.supported_ext
    else:
        keys = (description.supported_ext,)

    for key in keys:
        value = support_ext_value(camera_data, key)
        if value is None:
            continue
        if (
            not description.supported_ext_value
            or value in description.supported_ext_value
        ):
            return True
    return False


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

    @property
    def native_value(self) -> float | None:
        """Return the current numeric value from coordinator data."""
        return self.entity_description.get_value(self.data)

    async def async_set_native_value(self, value: float) -> None:
        """Send a new value to the device and refresh coordinator state."""
        try:
            await self.hass.async_add_executor_job(
                self.entity_description.set_value,
                self.coordinator.ezviz_client,
                self._serial,
                value,
                self.data,
            )
        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot set value for {self.entity_id}") from err

        self._attr_native_value = value
        await self.coordinator.async_request_refresh()
