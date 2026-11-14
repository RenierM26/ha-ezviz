"""Support for EZVIZ number controls."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
import math
from typing import Any, override

from pyezvizapi import EzvizClient
from pyezvizapi.constants import SupportExt
from pyezvizapi.devices.features import (
    custom_voice_volume_config,
    get_algorithm_value,
    has_algorithm_subtype,
    night_vision_duration_value,
    night_vision_luminance_value,
    night_vision_mode_value,
    night_vision_payload,
    optionals_mapping,
    resolve_channel,
)
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .errors import request_error
from .runtime import EzvizConfigEntry
from .utility import SupportExtView, passes_description_gates

SCAN_INTERVAL = timedelta(seconds=3600)
PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class EzvizNumberEntityDescription(NumberEntityDescription):
    """Describe an EZVIZ Number entity."""

    supported_ext_key: str | tuple[str, ...] | None = None
    supported_ext_value: list[str] | None = None
    required_device_categories: tuple[str, ...] | None = None
    get_value: Callable[[dict[str, Any]], float | None]
    set_value: Callable[[EzvizClient, str, float, dict[str, Any]], Any]
    is_supported_fn: Callable[[dict[str, Any]], bool] | None = None
    get_max_value: Callable[[dict[str, Any]], float] | None = None


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
            luminance=round(value),
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
            duration=round(value),
        )
        client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "NightVision_Model",
            payload,
        )

    return _setter


def _volume_capability(camera_data: dict[str, Any], key: SupportExt) -> bool:
    """Require an enabled numeric flag, never mere presence or truthiness."""
    value = SupportExtView.from_camera_data(camera_data).get(str(key.value))
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return False
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    # Native voice-volume UI requires 203 == 1; RN chime UI uses enabled 185.
    return value == 1 if key is SupportExt.SupportChangeVolume else value > 0


def _volume_config(camera_data: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Decode a complete reported volume object, retaining unrecognized fields."""
    config = optionals_mapping(camera_data).get(key)
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            return None
    return dict(config) if isinstance(config, Mapping) else None


def _alarm_volume_max(camera_data: dict[str, Any]) -> float:
    """The app's DBScreen ChimeMusic slider is 0-7, not a percentage."""
    category = camera_data.get("device_category")
    if category is None and isinstance(
        infos := camera_data.get("deviceInfos"), Mapping
    ):
        category = infos.get("deviceCategory")
    return 7 if category == "DBScreen" else 100


def _alarm_volume_config(camera_data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the reported alarm chime object without changing its payload."""
    return _volume_config(camera_data, "ChimeMusic")


def _reported_volume(config: dict[str, Any] | None, maximum: float) -> float | None:
    """Keep missing/invalid readings unknown, including boolean enable flags."""
    value = config.get("volume") if config else None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        return None
    return float(value)


def _alarm_volume_getter(camera_data: dict[str, Any]) -> float | None:
    """Require app-backed support as well as a valid observed chime value."""
    if not _volume_capability(camera_data, SupportExt.SupportDeviceRing):
        return None
    return _reported_volume(
        _alarm_volume_config(camera_data), _alarm_volume_max(camera_data)
    )


def _alarm_volume_setter() -> Callable[[EzvizClient, str, float, dict[str, Any]], Any]:
    def _setter(
        client: EzvizClient,
        serial: str,
        value: float,
        camera_data: dict[str, Any],
    ) -> Any:
        config = _alarm_volume_config(camera_data)
        if config is None or _alarm_volume_getter(camera_data) is None:
            raise PyEzvizError("Camera does not report a supported alarm chime volume")
        if isinstance(value, bool) or not math.isfinite(value):
            raise PyEzvizError("Alarm chime volume must be a finite number")
        payload = {
            **config,
            "volume": max(0, min(int(_alarm_volume_max(camera_data)), round(value))),
        }
        return client.set_dev_config_kv(
            serial, resolve_channel(camera_data), "ChimeMusic", payload
        )

    return _setter


def _alarm_voice_volume_getter(camera_data: dict[str, Any]) -> float | None:
    """Report voice volume independently of ChimeMusic and microphone gain."""
    if not _volume_capability(camera_data, SupportExt.SupportChangeVolume):
        return None
    value = _reported_volume(_volume_config(camera_data, "CustomVoice_Volume"), 100)
    return None if value is not None and 0 < value < 10 else value


def _alarm_voice_volume_setter() -> Callable[
    [EzvizClient, str, float, dict[str, Any]], Any
]:
    def _setter(
        client: EzvizClient,
        serial: str,
        value: float,
        camera_data: dict[str, Any],
    ) -> Any:
        config = _volume_config(camera_data, "CustomVoice_Volume")
        if config is None or _alarm_voice_volume_getter(camera_data) is None:
            raise PyEzvizError("Camera does not report a supported alarm voice volume")
        # App voice sliders select 10-100. A reported zero remains a valid muted
        # reading, but changing volume must not silently select a sound mode.
        if (
            isinstance(value, bool)
            or not math.isfinite(value)
            or not 10 <= value <= 100
        ):
            raise PyEzvizError("Alarm voice volume must be between 10 and 100")
        return client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "CustomVoice_Volume",
            {**config, "volume": round(value)},
        )

    return _setter


def _microphone_volume_getter(camera_data: dict[str, Any]) -> float | None:
    config = custom_voice_volume_config(camera_data)
    if not config:
        return None
    value = config.get("microphone_volume")
    return float(value) if isinstance(value, int) else None


def _microphone_volume_setter() -> Callable[
    [EzvizClient, str, float, dict[str, Any]], Any
]:
    def _setter(
        client: EzvizClient,
        serial: str,
        value: float,
        camera_data: dict[str, Any],
    ) -> Any:
        config = custom_voice_volume_config(camera_data) or {}
        speaker_volume = config.get("volume", 100)

        def _clamp(val: int) -> int:
            return max(0, min(100, val))

        payload = {
            "volume": _clamp(int(speaker_volume)),
            "microphone_volume": _clamp(round(value)),
        }

        return client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "CustomVoice_Volume",
            payload,
        )

    return _setter


STATIC_NUMBER_DESCRIPTIONS: tuple[EzvizNumberEntityDescription, ...] = (
    EzvizNumberEntityDescription(
        key="detection_sensibility",
        translation_key="detection_sensibility",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext_key=str(SupportExt.SupportSensibilityAdjust.value),
        supported_ext_value=["3"],
        get_value=_algorithm_value_getter("0", 1),
        set_value=_detection_setter(3),
        is_supported_fn=lambda data: (
            data.get("device_sub_category") == "C3A"
            and has_algorithm_subtype(data, "0", 1)
        ),
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_0_1",
        translation_key="algorithm_sensitivity",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=6,
        native_step=1,
        supported_ext_key=str(SupportExt.SupportSensibilityAdjust.value),
        supported_ext_value=["1"],
        get_value=_algorithm_value_getter("0", 1),
        set_value=_detection_setter(0),
        is_supported_fn=lambda data: has_algorithm_subtype(data, "0", 1),
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_3_1",
        translation_key="algorithm_param_pir",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext_key=str(SupportExt.SupportDetectAreaUnderDefencetype.value),
        get_value=_algorithm_value_getter("3", 1),
        set_value=_algorithm_param_setter("3", 1),
        is_supported_fn=lambda data: has_algorithm_subtype(data, "3", 1),
    ),
    EzvizNumberEntityDescription(
        key="algorithm_param_4_1",
        translation_key="algorithm_param_human",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        supported_ext_key=str(SupportExt.SupportDetectAreaUnderDefencetype.value),
        get_value=_algorithm_value_getter("4", 1),
        set_value=_algorithm_param_setter("4", 1),
        is_supported_fn=lambda data: has_algorithm_subtype(data, "4", 1),
    ),
    EzvizNumberEntityDescription(
        key="night_vision_luminance",
        translation_key="night_vision_luminance",
        entity_category=EntityCategory.CONFIG,
        native_min_value=20,
        native_max_value=100,
        native_step=1,
        supported_ext_key=str(SupportExt.SupportSmartNightVision.value),
        get_value=lambda data: float(night_vision_luminance_value(data)),
        set_value=_night_vision_luminance_setter(),
    ),
    EzvizNumberEntityDescription(
        key="night_vision_duration",
        translation_key="night_vision_duration",
        entity_category=EntityCategory.CONFIG,
        native_min_value=15,
        native_max_value=120,
        native_step=1,
        native_unit_of_measurement="s",
        supported_ext_key=str(SupportExt.SupportIntelligentNightVisionDuration.value),
        supported_ext_value=["1"],
        get_value=lambda data: float(night_vision_duration_value(data)),
        set_value=_night_vision_duration_setter(),
    ),
    EzvizNumberEntityDescription(
        key="alarm_volume",
        translation_key="alarm_volume",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        supported_ext_key=str(SupportExt.SupportDeviceRing.value),
        get_max_value=_alarm_volume_max,
        get_value=_alarm_volume_getter,
        set_value=_alarm_volume_setter(),
        is_supported_fn=lambda data: _alarm_volume_getter(data) is not None,
    ),
    EzvizNumberEntityDescription(
        key="alarm_voice_volume",
        translation_key="alarm_voice_volume",
        entity_category=EntityCategory.CONFIG,
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        supported_ext_key=str(SupportExt.SupportChangeVolume.value),
        get_value=_alarm_voice_volume_getter,
        set_value=_alarm_voice_volume_setter(),
        is_supported_fn=lambda data: _alarm_voice_volume_getter(data) is not None,
    ),
    EzvizNumberEntityDescription(
        key="microphone_volume",
        translation_key="microphone_volume",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        supported_ext_key=str(SupportExt.SupportAudioOnoff.value),
        get_value=_microphone_volume_getter,
        set_value=_microphone_volume_setter(),
    ),
)


def _is_desc_supported(
    camera_data: dict[str, Any], description: EzvizNumberEntityDescription
) -> bool:
    return passes_description_gates(
        camera_data,
        supported_ext_keys=description.supported_ext_key,
        supported_ext_values=description.supported_ext_value,
        required_device_categories=description.required_device_categories,
        predicate=description.is_supported_fn,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = entry.runtime_data.coordinator

    entities: list[NumberEntity] = [
        EzvizNumber(coordinator, serial, desc)
        for serial, camera_data in coordinator.data.items()
        for desc in STATIC_NUMBER_DESCRIPTIONS
        if _is_desc_supported(camera_data, desc)
    ]

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
    @override
    def native_value(self) -> float | None:
        """Return the current numeric value from coordinator data."""
        return self.entity_description.get_value(self.data)

    @property
    @override
    def native_max_value(self) -> float:
        """Use the current device category's range for dynamic descriptions."""
        if getter := self.entity_description.get_max_value:
            return getter(self.data)
        return super().native_max_value

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Send a new value to the device and refresh coordinator state."""
        try:
            await self.coordinator.async_execute(
                self.entity_description.set_value,
                self.coordinator.ezviz_client,
                self._serial,
                value,
                self.data,
            )
        except (HTTPError, PyEzvizError, OSError, TimeoutError) as err:
            raise request_error("Set numeric value", err) from err

        self._attr_native_value = value
        await self.coordinator.async_request_refresh()
