"""Support for EZVIZ select controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.constants import SoundMode, SupportExt
from pyezvizapi.exceptions import HTTPError, PyEzvizError
from pyezvizapi.feature import (
    blc_current_value,
    day_night_mode_value,
    day_night_sensitivity_value,
    device_icr_dss_config,
    display_mode_value,
    lens_defog_value,
    night_vision_config,
    night_vision_mode_value,
    resolve_channel,
)

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .utility import (
    has_lens_defog,
    linked_tracking_takeover_enabled,
    passes_description_gates,
    set_lens_defog_option,
    set_linked_tracking_takeover,
    support_ext_has,
)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizSelectEntityDescription(SelectEntityDescription):
    """EZVIZ select with capability & device-category gating."""

    # Gating
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None
    required_device_categories: tuple[str, ...] | None = None
    is_supported_fn: Callable[[dict[str, Any]], bool] | None = None

    # Select mapping
    option_range: list[int]
    get_current_option: Callable[[dict[str, Any]], int]
    set_current_option: Callable[[EzvizClient, str, int, dict[str, Any]], Any]
    options_fn: Callable[[dict[str, Any]], list[str]] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any],
    desc: EzvizSelectEntityDescription,
) -> bool:
    """Return True if this select description is supported by the camera."""
    return passes_description_gates(
        camera_data,
        supported_ext_keys=desc.supported_ext_key,
        supported_ext_values=desc.supported_ext_value,
        required_device_categories=desc.required_device_categories,
        predicate=desc.is_supported_fn,
    )


SELECTS: tuple[EzvizSelectEntityDescription, ...] = (
    EzvizSelectEntityDescription(
        key="alarm_sound_mod",
        translation_key="alarm_sound_mode",
        entity_category=EntityCategory.CONFIG,
        options=["soft", "intensive", "silent"],
        supported_ext_key=str(SupportExt.SupportTalk.value),
        supported_ext_value=["1", "2", "3", "4"],
        option_range=[0, 1, 2],
        get_current_option=lambda d: getattr(SoundMode, d["alarm_sound_mod"]).value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.alarm_sound(serial, value, 1),
    ),
    EzvizSelectEntityDescription(
        key="battery_camera_work_mode",
        translation_key="battery_camera_work_mode",
        entity_category=EntityCategory.CONFIG,
        options=[
            "power_save",
            "high_performance",
            "plugged_in",
            "super_power_save",
            "custom",
            "hybernate",
        ],
        supported_ext_key=str(SupportExt.SupportWorkModeList.value),
        supported_ext_value=["1,2,3,4,10", "1,2,4,10"],
        option_range=[0, 1, 2, 3, 4, 5],
        get_current_option=lambda d: d["battery_camera_work_mode"],
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.set_battery_camera_work_mode(serial, value),
    ),
    EzvizSelectEntityDescription(
        key="battery_camera_work_mode_aov",
        translation_key="battery_camera_work_mode_aov",
        entity_category=EntityCategory.CONFIG,
        options=[
            "standard",
            "plugged_in",
            "power_save",
            "custom",
            "aov_mode",
        ],
        supported_ext_key=str(SupportExt.SupportNewWorkMode.value),
        supported_ext_value=["1,3,10,9,8"],
        option_range=[1, 2, 3, 4, 7],
        get_current_option=lambda d: d["battery_camera_work_mode"],
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.set_battery_camera_work_mode(serial, value),
    ),
    EzvizSelectEntityDescription(
        key="advanced_detect_human_car_pir",
        translation_key="advanced_detect_human_car_pir",
        entity_category=EntityCategory.CONFIG,
        options=[
            "advanced_detect_human_shape",
            "advanced_detect_pir",
        ],
        supported_ext_key=str(SupportExt.SupportDefenceTypeFull.value),
        supported_ext_value=["3,6"],
        option_range=[1, 5],
        get_current_option=lambda d: d["Alarm_DetectHumanCar"],
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.set_detection_mode(serial, value),
        is_supported_fn=lambda d: str(SupportExt.SupportNewWorkMode.value)
        not in (d.get("supportExt") or {}),
    ),
    EzvizSelectEntityDescription(
        key="advanced_detect_human_car",
        translation_key="advanced_detect_human_car",
        entity_category=EntityCategory.CONFIG,
        options=[
            "advanced_detect_human_shape",
            "advanced_detect_image_change",
        ],
        supported_ext_key=str(SupportExt.SupportDetectHumanCar.value),
        supported_ext_value=["2"],
        option_range=[1, 3],
        get_current_option=lambda d: d["Alarm_DetectHumanCar"],
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.set_detection_mode(serial, value),
    ),
    EzvizSelectEntityDescription(
        key="image_style_setting",
        translation_key="image_style_setting",
        entity_category=EntityCategory.CONFIG,
        options=["image_style_original", "image_style_soft", "image_style_vivid"],
        supported_ext_key=str(SupportExt.SupportBackLight.value),
        supported_ext_value=["1"],
        option_range=[1, 2, 3],
        get_current_option=display_mode_value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        camera_data: ezviz_client.set_device_config_by_key(
            serial,
            value=f'{{"mode":{value}}}',
            key="display_mode",
        ),
    ),
    EzvizSelectEntityDescription(
        key="blc_region",
        translation_key="blc_region",
        entity_category=EntityCategory.CONFIG,
        options=[
            "blc_off",
            "blc_up",
            "blc_down",
            "blc_left",
            "blc_right",
            "blc_center",
        ],
        supported_ext_key=str(SupportExt.SupportBackLight.value),
        supported_ext_value=["1"],
        option_range=[0, 1, 2, 3, 4, 5],  # 0=Off; 1..5 map to positions
        get_current_option=blc_current_value,  # returns one of 0..5
        set_current_option=lambda ezviz_client,
        serial,
        value,
        camera_data: ezviz_client.set_device_config_by_key(
            serial,
            value=(
                '{"mode":1,"enable":0,"position":0}'
                if int(value) == 0
                else f'{{"mode":1,"enable":1,"position":{int(value)}}}'
            ),
            key="inverse_mode",
        ),
    ),
    EzvizSelectEntityDescription(
        key="lens_defog",
        translation_key="lens_defog",
        entity_category=EntityCategory.CONFIG,
        options=[
            "lens_defog_auto",
            "lens_defog_on",
            "lens_defog_off",
        ],
        supported_ext_key="688",
        supported_ext_value=["6"],
        option_range=[0, 1, 2],
        get_current_option=lens_defog_value,
        set_current_option=set_lens_defog_option,
        is_supported_fn=has_lens_defog,
    ),
    EzvizSelectEntityDescription(
        key="night_vision_model",
        translation_key="night_vision_model",
        entity_category=EntityCategory.CONFIG,
        options=[
            "night_vision_b_w",
            "night_vision_colour",
        ],
        supported_ext_key=str(SupportExt.SupportSmartNightVision.value),
        supported_ext_value=["2"],
        option_range=[0, 1],
        get_current_option=night_vision_mode_value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.set_night_vision_mode(serial, value),
        is_supported_fn=lambda data: bool(night_vision_config(data)),
    ),
    EzvizSelectEntityDescription(
        key="smart_night_vision_model",
        translation_key="smart_night_vision_model",
        entity_category=EntityCategory.CONFIG,
        options=[
            "night_vision_b_w",
            "night_vision_colour",
            "night_vision_smart",
        ],
        supported_ext_key=str(SupportExt.SupportSmartNightVision.value),
        supported_ext_value=["1"],
        option_range=[0, 1, 2],
        get_current_option=night_vision_mode_value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.set_night_vision_mode(serial, value),
        is_supported_fn=lambda data: (not support_ext_has(data, "688", ["6"])),
    ),
    EzvizSelectEntityDescription(
        key="smart_night_vision_model_battery",
        translation_key="smart_night_vision_model_battery",
        entity_category=EntityCategory.CONFIG,
        options=[
            "night_vision_b_w",
            "night_vision_smart",
        ],
        supported_ext_key=str(SupportExt.SupportSmartNightVision.value),
        supported_ext_value=["7"],
        option_range=[0, 2],
        get_current_option=night_vision_mode_value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.set_night_vision_mode(serial, value),
        is_supported_fn=lambda data: bool(night_vision_config(data)),
    ),
    EzvizSelectEntityDescription(
        key="super_night_vision_model",
        translation_key="super_night_vision_model",
        entity_category=EntityCategory.CONFIG,
        options=[
            "night_vision_b_w",
            "night_vision_colour",
            "night_vision_smart",
            "super_night_view",
        ],
        supported_ext_key="688",
        supported_ext_value=["1,2,4,6"],
        option_range=[0, 1, 2, 5],
        get_current_option=night_vision_mode_value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        _camera_data: ezviz_client.set_night_vision_mode(serial, value),
        is_supported_fn=lambda data: bool(night_vision_config(data)),
    ),
    EzvizSelectEntityDescription(
        key="ptz_linked_tracking_range",
        translation_key="ptz_linked_tracking_range",
        entity_category=EntityCategory.CONFIG,
        options=[
            "ptz_tracking_range_wide",
            "ptz_tracking_range_full_priority",
        ],
        supported_ext_key="715",
        supported_ext_value=["1"],
        option_range=[0, 1],
        get_current_option=lambda data: 1
        if linked_tracking_takeover_enabled(data)
        else 0,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        camera_data: set_linked_tracking_takeover(
            ezviz_client,
            serial,
            bool(value),
            camera_data,
        ),
    ),
    EzvizSelectEntityDescription(
        key="day_night_mode",
        translation_key="day_night_mode",
        entity_category=EntityCategory.CONFIG,
        options=["day_night_auto", "day_night_day", "day_night_night"],
        option_range=[0, 1, 2],
        is_supported_fn=lambda d: bool(device_icr_dss_config(d)),
        get_current_option=day_night_mode_value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        camera_data: ezviz_client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "device_ICR_DSS",
            {
                "mode": value,
                "sensitivity": day_night_sensitivity_value(camera_data),
            },
        ),
    ),
    EzvizSelectEntityDescription(
        key="day_night_sensitivity",
        translation_key="day_night_sensitivity",
        entity_category=EntityCategory.CONFIG,
        options=[
            "day_night_sensitivity_low",
            "day_night_sensitivity_medium",
            "day_night_sensitivity_high",
        ],
        option_range=[1, 2, 3],
        is_supported_fn=lambda d: (
            support_ext_has(d, str(SupportExt.SupportDayNightSwitch.value))
            or bool(device_icr_dss_config(d))
        ),
        get_current_option=day_night_sensitivity_value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        camera_data: ezviz_client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "device_ICR_DSS",
            {
                "mode": 0,
                "sensitivity": value,
            },
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ select entities based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities(
        EzvizSelect(coordinator, serial, desc)
        for serial, camera_data in coordinator.data.items()
        for desc in SELECTS
        if _is_desc_supported(camera_data, desc)
    )


class EzvizSelect(EzvizEntity, SelectEntity):
    """Representation of an EZVIZ select entity."""

    _attr_has_entity_name = True
    entity_description: EzvizSelectEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizSelectEntityDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        current_value = self.entity_description.get_current_option(self.data)

        try:
            idx = self.entity_description.option_range.index(current_value)
        except ValueError:
            return None
        return self.options[idx] if 0 <= idx < len(self.options) else None

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        try:
            idx = self.options.index(option)
        except ValueError as err:
            raise HomeAssistantError(
                f"Invalid option '{option}' for {self.entity_id}"
            ) from err

        if not self.entity_description.option_range or not (
            0 <= idx < len(self.entity_description.option_range)
        ):
            raise HomeAssistantError(f"Invalid option '{option}' for {self.entity_id}")
        set_value = self.entity_description.option_range[idx]

        try:
            # Run potentially blocking client call in executor
            await self.hass.async_add_executor_job(
                self.entity_description.set_current_option,
                self.coordinator.ezviz_client,
                self._serial,
                set_value,
                self.data,
            )
        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot set option for {self.entity_id}") from err

        await self.coordinator.async_request_refresh()
