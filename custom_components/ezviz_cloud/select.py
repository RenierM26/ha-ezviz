"""Support for EZVIZ select controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.constants import SoundMode, SupportExt
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizSelectEntityDescription(SelectEntityDescription):
    """EZVIZ select with capability & device-category gating."""

    # Gating
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None
    required_device_categories: tuple[str, ...] | None = None
    available_fn: Callable[[dict[str, Any]], bool] | None = None

    # Select mapping
    option_range: list[int]
    get_current_option: Callable[[dict[str, Any]], int]
    set_current_option: Callable[[EzvizClient, str, int], Any]


def _is_desc_supported(
    camera_data: dict[str, Any],
    desc: EzvizSelectEntityDescription,
) -> bool:
    """Return True if this select description is supported by the camera."""
    if desc.required_device_categories is not None:
        if camera_data.get("device_category") not in desc.required_device_categories:
            return False

    if desc.supported_ext_key is not None:
        support_ext = camera_data.get("supportExt") or {}
        if not isinstance(support_ext, dict):
            return False
        current_val = support_ext.get(desc.supported_ext_key)
        if current_val is None:
            return False
        if desc.supported_ext_value and str(current_val).strip() not in {
            v.strip() for v in desc.supported_ext_value
        }:
            return False

    if desc.available_fn is not None and not desc.available_fn(camera_data):
        return False

    return True


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
        set_current_option=lambda ezviz_client, serial, value: ezviz_client.alarm_sound(
            serial, value, 1
        ),
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
        supported_ext_value=["1,2,3,4,10"],
        option_range=[0, 1, 2, 3, 4, 5],
        get_current_option=lambda d: d["battery_camera_work_mode"],
        set_current_option=lambda ezviz_client,
        serial,
        value: ezviz_client.set_battery_camera_work_mode(serial, value),
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
        value: ezviz_client.set_battery_camera_work_mode(serial, value),
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
        get_current_option=lambda d: d["NightVision_Model"]["graphicType"],
        set_current_option=lambda ezviz_client,
        serial,
        value: ezviz_client.set_night_vision_mode(serial, value),
    ),
    EzvizSelectEntityDescription(
        key="smart_night_vision_model",
        translation_key="smart_night_vision_model",
        entity_category=EntityCategory.CONFIG,
        options=[
            "night_vision_b_w",
            "night_vision_colour",
            "night_vision_smart",
            "super_night_view",
        ],
        supported_ext_key=str(SupportExt.SupportSmartNightVision.value),
        supported_ext_value=["1"],
        option_range=[0, 1, 2, 5],
        get_current_option=lambda d: d["NightVision_Model"]["graphicType"],
        set_current_option=lambda ezviz_client,
        serial,
        value: ezviz_client.set_night_vision_mode(serial, value),
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
        get_current_option=lambda d: d["NightVision_Model"]["graphicType"],
        set_current_option=lambda ezviz_client,
        serial,
        value: ezviz_client.set_night_vision_mode(serial, value),
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
        value: ezviz_client.set_detection_mode(serial, value),
        available_fn=lambda d: str(SupportExt.SupportNewWorkMode.value)
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
        value: ezviz_client.set_detection_mode(serial, value),
    ),
    EzvizSelectEntityDescription(
        key="image_style_setting",
        translation_key="image_style_setting",
        entity_category=EntityCategory.CONFIG,
        options=["image_style_original", "image_style_soft", "image_style_vivid"],
        supported_ext_key=str(SupportExt.SupportBackLight.value),
        supported_ext_value=["1"],
        option_range=[1, 2, 3],
        get_current_option=lambda d: d["optionals"]["display_mode"]["mode"],
        set_current_option=lambda ezviz_client,
        serial,
        value: ezviz_client.set_device_config_by_key(
            serial, value=f'{{"mode":{value}}}', key="display_mode"
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

        if not (0 <= idx < len(self.entity_description.option_range)):
            raise HomeAssistantError(f"Invalid option '{option}' for {self.entity_id}")

        set_value = self.entity_description.option_range[idx]

        try:
            # Run potentially blocking client call in executor
            await self.hass.async_add_executor_job(
                self.entity_description.set_current_option,
                self.coordinator.ezviz_client,
                self._serial,
                set_value,
            )
        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot set option for {self.entity_id}") from err

        await self.coordinator.async_request_refresh()
