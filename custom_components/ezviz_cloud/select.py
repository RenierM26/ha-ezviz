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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .utility import (
    day_night_mode_value,
    day_night_sensitivity_value,
    device_category,
    device_icr_dss_config,
    display_mode_value,
    night_vision_config,
    night_vision_mode_value,
    night_vision_payload,
    resolve_channel,
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
    available_fn: Callable[[dict[str, Any]], bool] | None = None

    # Select mapping
    option_range: list[int] | None = None
    get_current_option: Callable[[dict[str, Any]], int]
    set_current_option: Callable[[EzvizClient, str, int, dict[str, Any]], Any]
    options_map: dict[str, int] | None = None
    options_fn: Callable[[dict[str, Any]], list[str]] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any],
    desc: EzvizSelectEntityDescription,
) -> bool:
    """Return True if this select description is supported by the camera."""
    if desc.required_device_categories is not None:
        if device_category(camera_data) not in desc.required_device_categories:
            return False

    if desc.supported_ext_key is not None:
        if not support_ext_has(
            camera_data, desc.supported_ext_key, desc.supported_ext_value
        ):
            return False

    if desc.available_fn is not None and not desc.available_fn(camera_data):
        return False

    return True


def _night_vision_options(camera_data: dict[str, Any]) -> list[str]:
    """Return allowed night vision options for this camera."""

    options = ["night_vision_b_w", "night_vision_colour"]

    supports_smart = support_ext_has(
        camera_data, str(SupportExt.SupportSmartNightVision.value)
    ) or support_ext_has(
        camera_data, str(SupportExt.SupportIntelligentNightVisionDuration.value)
    )

    config = night_vision_config(camera_data)
    if config.get("graphicType") == 2 or supports_smart:
        options.append("night_vision_smart")

    # Super night view is offered on select wired models; include when the
    # device already reports it or exposes the duration flag.
    if config.get("graphicType") == 5 or support_ext_has(
        camera_data, str(SupportExt.SupportIntelligentNightVisionDuration.value)
    ):
        options.append("night_vision_super")

    # Preserve original ordering while removing duplicates
    return list(dict.fromkeys(options))


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
        set_current_option=lambda ezviz_client, serial, value, _camera_data: ezviz_client.alarm_sound(
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
        camera_data: ezviz_client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "display_mode",
            {"mode": value},
        ),
    ),
    EzvizSelectEntityDescription(
        key="night_vision_mode",
        translation_key="night_vision_mode",
        entity_category=EntityCategory.CONFIG,
        options=[
            "night_vision_b_w",
            "night_vision_colour",
            "night_vision_smart",
            "night_vision_super",
        ],
        supported_ext_key=str(SupportExt.SupportNightVisionMode.value),
        get_current_option=night_vision_mode_value,
        set_current_option=lambda ezviz_client,
        serial,
        value,
        camera_data: ezviz_client.set_dev_config_kv(
            serial,
            resolve_channel(camera_data),
            "NightVision_Model",
            night_vision_payload(
                camera_data,
                mode=int(value),
            ),
        ),
        options_map={
            "night_vision_b_w": 0,
            "night_vision_colour": 1,
            "night_vision_smart": 2,
            "night_vision_super": 5,
        },
        options_fn=_night_vision_options,
        available_fn=lambda data: (
            support_ext_has(data, str(SupportExt.SupportNightVisionMode.value))
            or support_ext_has(data, str(SupportExt.SupportSmartNightVision.value))
        )
        and bool(night_vision_config(data)),
    ),
    EzvizSelectEntityDescription(
        key="day_night_mode",
        translation_key="day_night_mode",
        entity_category=EntityCategory.CONFIG,
        options=["day_night_auto", "day_night_day", "day_night_night"],
        option_range=[0, 1, 2],
        available_fn=lambda d: (
            support_ext_has(d, str(SupportExt.SupportDayNightSwitch.value))
            or bool(device_icr_dss_config(d))
        ),
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
        available_fn=lambda d: (
            (support_ext_has(d, str(SupportExt.SupportDayNightSwitch.value))
            or bool(device_icr_dss_config(d)))
            and night_vision_mode_value(d) != 5
        )
        and day_night_mode_value(d) == 0,
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
        self._attr_options = self._compute_options()

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        current_value = self.entity_description.get_current_option(self.data)
        if self.entity_description.options_map:
            for option in self.options:
                if self.entity_description.options_map.get(option) == current_value:
                    return option
            return None

        if not self.entity_description.option_range:
            return None
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

        if self.entity_description.options_map:
            if option not in self.entity_description.options_map:
                raise HomeAssistantError(
                    f"Invalid option '{option}' for {self.entity_id}"
                )
            set_value = self.entity_description.options_map[option]
        else:
            if not self.entity_description.option_range or not (
                0 <= idx < len(self.entity_description.option_range)
            ):
                raise HomeAssistantError(
                    f"Invalid option '{option}' for {self.entity_id}"
                )
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

    def _compute_options(self) -> list[str]:
        """Return the list of options for the entity."""

        desc = self.entity_description
        if desc.options_map:
            base_options = list(desc.options_map.keys())
            if desc.options_fn is not None:
                filtered = desc.options_fn(self.data)
                return [opt for opt in base_options if opt in filtered]
            return base_options
        return list(desc.options or [])

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle data update from the coordinator."""

        options = self._compute_options()
        if options != list(self.options):
            self._attr_options = options
        super()._handle_coordinator_update()
