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
    """Describe a EZVIZ Select entity."""

    supported_ext_key: str
    supported_ext_value: list[str]
    option_range: list
    get_current_option: Callable[[dict], int]
    set_current_option: Callable[[EzvizClient, str, int], Any]


SELECT_TYPE = (
    EzvizSelectEntityDescription(
        key="alarm_sound_mod",
        translation_key="alarm_sound_mode",
        entity_category=EntityCategory.CONFIG,
        options=["soft", "intensive", "silent"],
        supported_ext_key=str(SupportExt.SupportTalk.value),
        supported_ext_value=["1", "2", "3", "4"],
        option_range=[0, 1, 2],
        get_current_option=lambda data: getattr(
            SoundMode, data["alarm_sound_mod"]
        ).value,
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
        get_current_option=lambda data: data["battery_camera_work_mode"],
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
            "super_power_save",
            "custom",
            "aov_mode",
        ],
        supported_ext_key=str(SupportExt.SupportNewWorkMode.value),
        supported_ext_value=["1,3,10,9,8"],
        option_range=[1, 2, 3, 4, 7],
        get_current_option=lambda data: data["battery_camera_work_mode"],
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
        get_current_option=lambda data: data["NightVision_Model"]["graphicType"],
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
        ],
        supported_ext_key=str(SupportExt.SupportSmartNightVision.value),
        supported_ext_value=["1"],
        option_range=[0, 1, 2],
        get_current_option=lambda data: data["NightVision_Model"]["graphicType"],
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
        get_current_option=lambda data: data["Alarm_DetectHumanCar"],
        set_current_option=lambda ezviz_client,
        serial,
        value: ezviz_client.set_detection_mode(serial, value),
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
        get_current_option=lambda data: data["Alarm_DetectHumanCar"],
        set_current_option=lambda ezviz_client,
        serial,
        value: ezviz_client.set_detection_mode(serial, value),
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
        EzvizSelect(coordinator, camera, entity_description)
        for camera in coordinator.data
        for capability, value in coordinator.data[camera]["supportExt"].items()
        for entity_description in SELECT_TYPE
        if capability == entity_description.supported_ext_key
        if value in entity_description.supported_ext_value
    )


class EzvizSelect(EzvizEntity, SelectEntity):
    """Representation of a EZVIZ select entity."""

    entity_description: EzvizSelectEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizSelectEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{description.key}"
        self.entity_description = description

    @property
    def current_option(self) -> str | None:
        """Return the selected entity option to represent the entity state."""
        current_value = self.entity_description.get_current_option(self.data)

        if current_value in self.entity_description.option_range:
            option_index = self.entity_description.option_range.index(current_value)
            return self.options[option_index]

        return None

    def select_option(self, option: str) -> None:
        """Change the selected option."""
        option_index = self.options.index(option)
        option_set_value = self.entity_description.option_range[option_index]

        try:
            self.entity_description.set_current_option(
                self.coordinator.ezviz_client, self._serial, option_set_value
            )

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(f"Cannot set option for {self.entity_id}") from err
