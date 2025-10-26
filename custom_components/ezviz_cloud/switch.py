"""Support for EZVIZ switches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyezvizapi import EzvizClient
from pyezvizapi.constants import DeviceCatagories, DeviceSwitchType, SupportExt
from pyezvizapi.exceptions import HTTPError, InvalidHost, PyEzvizError
from pyezvizapi.feature import (
    has_osd_overlay,
    supplement_light_available,
    supplement_light_enabled,
)

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .migration import migrate_unique_ids_with_coordinator
from .utility import (
    PortSecurityToggle,
    intelligent_app_available,
    intelligent_app_method,
    intelligent_app_value_fn,
    passes_description_gates,
)

PARALLEL_UPDATES = 1

PORT_SECURITY_CLIENT_MODE = PortSecurityToggle((80, 443, 8000))
PORT_SECURITY_LINK = PortSecurityToggle.single(50161)


@dataclass(frozen=True, kw_only=True)
class EzvizSwitchEntityDescription(SwitchEntityDescription):
    """EZVIZ switch with capability & device-category gating."""

    value_fn: Callable[[dict[str, Any]], Any]
    method: Callable[[EzvizClient, str, int], Any]
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None
    is_supported_fn: Callable[[dict[str, Any]], bool] | None = None
    required_device_categories: tuple[str, ...] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any], desc: EzvizSwitchEntityDescription
) -> bool:
    """Return True if this switch description is supported by the camera."""
    return passes_description_gates(
        camera_data,
        supported_ext_keys=desc.supported_ext_key,
        supported_ext_values=desc.supported_ext_value,
        required_device_categories=desc.required_device_categories,
        predicate=desc.is_supported_fn,
    )


def _has_switch_entry(
    switch_type: DeviceSwitchType,
) -> Callable[[dict[str, Any]], bool]:
    """Return True when the given switch ID exists in camera_data['switches']."""

    def _predicate(camera_data: dict[str, Any]) -> bool:
        switches = camera_data.get("switches")
        return isinstance(switches, dict) and switch_type.value in switches

    return _predicate


def _has_intelligent_app(app_name: str) -> Callable[[dict[str, Any]], bool]:
    """Return True when the intelligent app is available for this camera."""

    def _predicate(camera_data: dict[str, Any]) -> bool:
        return intelligent_app_available(camera_data, app_name)

    return _predicate


def _combine_predicates(
    base: Callable[[dict[str, Any]], bool],
    extra: Callable[[dict[str, Any]], bool] | None,
) -> Callable[[dict[str, Any]], bool]:
    """Return a predicate that requires both base and optional extra conditions."""

    if extra is None:
        return base

    def _predicate(camera_data: dict[str, Any]) -> bool:
        return base(camera_data) and extra(camera_data)

    return _predicate


def _switch_entry_value_fn(
    switch_type: DeviceSwitchType,
) -> Callable[[dict[str, Any]], Any]:
    """Return a value extractor for camera_data['switches'][switch_type]."""

    def _value(camera_data: dict[str, Any]) -> Any:
        switches = camera_data.get("switches")
        if isinstance(switches, dict):
            return switches.get(switch_type.value)
        return None

    return _value


def _switch_entry_method(
    switch_type: DeviceSwitchType,
) -> Callable[[EzvizClient, str, int], Any]:
    """Return a setter that toggles camera_data['switches'][switch_type]."""

    def _method(client: EzvizClient, serial: str, enable: int) -> Any:
        return client.switch_status(serial, switch_type.value, enable)

    return _method


def _supplement_light_method(client: EzvizClient, serial: str, enable: int) -> Any:
    """Toggle the intelligent fill light mode."""

    return client.set_intelligent_fill_light(serial, enabled=bool(enable))


_STATIC_SWITCHES: tuple[EzvizSwitchEntityDescription, ...] = (
    # ---- Numeric "switches" from camera_data["switches"][ID] ----
    EzvizSwitchEntityDescription(
        key="ALARM_TONE",
        translation_key="voice_prompt",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAlarmVoice.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.ALARM_TONE),
        method=_switch_entry_method(DeviceSwitchType.ALARM_TONE),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.ALARM_TONE),
    ),
    EzvizSwitchEntityDescription(
        key="LIGHT",
        translation_key="status_light",
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=_switch_entry_value_fn(DeviceSwitchType.LIGHT),
        method=_switch_entry_method(DeviceSwitchType.LIGHT),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.LIGHT),
    ),
    EzvizSwitchEntityDescription(
        key="PRIVACY",
        translation_key="privacy",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportPtzPrivacy.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.PRIVACY),
        method=_switch_entry_method(DeviceSwitchType.PRIVACY),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.PRIVACY),
    ),
    EzvizSwitchEntityDescription(
        key="INFRARED_LIGHT",
        translation_key="infrared_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportCloseInfraredLight.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.INFRARED_LIGHT),
        method=_switch_entry_method(DeviceSwitchType.INFRARED_LIGHT),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.INFRARED_LIGHT),
    ),
    EzvizSwitchEntityDescription(
        key="SLEEP",
        translation_key="sleep",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportSleep.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.SLEEP),
        method=_switch_entry_method(DeviceSwitchType.SLEEP),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.SLEEP),
    ),
    EzvizSwitchEntityDescription(
        key="SOUND",
        translation_key="audio",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAudioOnoff.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.SOUND),
        method=_switch_entry_method(DeviceSwitchType.SOUND),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.SOUND),
    ),
    EzvizSwitchEntityDescription(
        key="MOBILE_TRACKING",
        translation_key="motion_tracking",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportIntelligentTrack.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.MOBILE_TRACKING),
        method=_switch_entry_method(DeviceSwitchType.MOBILE_TRACKING),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.MOBILE_TRACKING),
    ),
    EzvizSwitchEntityDescription(
        key="OSD",
        translation_key="osd_overlay",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportOsd.value),
        value_fn=has_osd_overlay,
        method=lambda client, serial, enable: client.set_camera_osd(
            serial,
            enabled=bool(enable),
        ),
    ),
    EzvizSwitchEntityDescription(
        key="WDR",
        translation_key="wdr",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportWideDynamicRange.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.WIDE_DYNAMIC_RANGE),
        method=_switch_entry_method(DeviceSwitchType.WIDE_DYNAMIC_RANGE),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.WIDE_DYNAMIC_RANGE),
    ),
    EzvizSwitchEntityDescription(
        key="DISTORTION_CORRECTION",
        translation_key="distortion_correction",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportDistortionCorrection.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.DISTORTION_CORRECTION),
        method=_switch_entry_method(DeviceSwitchType.DISTORTION_CORRECTION),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.DISTORTION_CORRECTION),
    ),
    EzvizSwitchEntityDescription(
        key="ALL_DAY_VIDEO",
        translation_key="all_day_video_recording",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportFullDayRecord.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.ALL_DAY_VIDEO),
        method=_switch_entry_method(DeviceSwitchType.ALL_DAY_VIDEO),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.ALL_DAY_VIDEO),
    ),
    EzvizSwitchEntityDescription(
        key="AUTO_SLEEP",
        translation_key="auto_sleep",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAutoSleep.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.AUTO_SLEEP),
        method=_switch_entry_method(DeviceSwitchType.AUTO_SLEEP),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.AUTO_SLEEP),
    ),
    EzvizSwitchEntityDescription(
        key="LIGHT_FLICKER",
        translation_key="flicker_light_on_movement",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportActiveDefense.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.LIGHT_FLICKER),
        method=_switch_entry_method(DeviceSwitchType.LIGHT_FLICKER),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.LIGHT_FLICKER),
    ),
    EzvizSwitchEntityDescription(
        key="ALARM_LIGHT_RELEVANCE",
        translation_key="pir_motion_activated_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportLightRelate.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.ALARM_LIGHT_RELEVANCE),
        method=_switch_entry_method(DeviceSwitchType.ALARM_LIGHT_RELEVANCE),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.ALARM_LIGHT_RELEVANCE),
    ),
    EzvizSwitchEntityDescription(
        key="TAMPER_ALARM",
        translation_key="tamper_alarm",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportTamperAlarm.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.TAMPER_ALARM),
        method=_switch_entry_method(DeviceSwitchType.TAMPER_ALARM),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.TAMPER_ALARM),
    ),
    EzvizSwitchEntityDescription(
        key="TRACKING",
        translation_key="follow_movement",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportTracking.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.TRACKING),
        method=_switch_entry_method(DeviceSwitchType.TRACKING),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.TRACKING),
    ),
    EzvizSwitchEntityDescription(
        key="WATERMARK",
        translation_key="logo_watermark",
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=_switch_entry_value_fn(DeviceSwitchType.LOGO_WATERMARK),
        method=_switch_entry_method(DeviceSwitchType.LOGO_WATERMARK),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.LOGO_WATERMARK),
    ),
    # ---- New: additional useful app booleans ----
    EzvizSwitchEntityDescription(
        key="CHANNELOFFLINE",
        translation_key="channel_offline_notify",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportChannelOffline.value),  # 70
        value_fn=_switch_entry_value_fn(DeviceSwitchType.CHANNELOFFLINE),
        method=_switch_entry_method(DeviceSwitchType.CHANNELOFFLINE),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.CHANNELOFFLINE),
    ),
    EzvizSwitchEntityDescription(
        key="WIFI_LIGHT",
        translation_key="wifi_status_light",
        device_class=SwitchDeviceClass.SWITCH,
        required_device_categories=(
            DeviceCatagories.W2H_BASE_STATION_DEVICE_CATEGORY.value
        ),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.WIFI_LIGHT),
        method=_switch_entry_method(DeviceSwitchType.WIFI_LIGHT),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.WIFI_LIGHT),
    ),
    EzvizSwitchEntityDescription(
        key="OUTLET_RECOVER",
        translation_key="outlet_power_recover",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportPoweroffRecovery.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.OUTLET_RECOVER),
        method=_switch_entry_method(DeviceSwitchType.OUTLET_RECOVER),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.OUTLET_RECOVER),
    ),
    EzvizSwitchEntityDescription(
        key="OUTDOOR_RINGING_SOUND",
        translation_key="outdoor_ringing_sound",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportBellSet.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.OUTDOOR_RINGING_SOUND),
        method=_switch_entry_method(DeviceSwitchType.OUTDOOR_RINGING_SOUND),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.OUTDOOR_RINGING_SOUND),
    ),
    EzvizSwitchEntityDescription(
        key="INTELLIGENT_PQ_SWITCH",
        translation_key="intelligent_picture_quality",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportIntelligentPQSwitch.value),  # 366
        value_fn=_switch_entry_value_fn(DeviceSwitchType.INTELLIGENT_PQ_SWITCH),
        method=_switch_entry_method(DeviceSwitchType.INTELLIGENT_PQ_SWITCH),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.INTELLIGENT_PQ_SWITCH),
    ),
    EzvizSwitchEntityDescription(
        key="AUTO_ZOOM_TRACKING",
        translation_key="auto_zoom_tracking",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportFeatureTrack.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.FEATURE_TRACKING),
        method=_switch_entry_method(DeviceSwitchType.FEATURE_TRACKING),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.FEATURE_TRACKING),
    ),
    EzvizSwitchEntityDescription(
        key="intelligent_fill_light",
        translation_key="intelligent_fill_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key="688",
        supported_ext_value=["1,2,4,6"],
        value_fn=supplement_light_enabled,
        method=_supplement_light_method,
        is_supported_fn=supplement_light_available,
    ),
    EzvizSwitchEntityDescription(
        key="HUMAN_INTELLIGENT_DETECTION",
        translation_key="people_detection",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportSmartBodyDetect.value),  # 244
        value_fn=_switch_entry_value_fn(DeviceSwitchType.HUMAN_INTELLIGENT_DETECTION),
        method=_switch_entry_method(DeviceSwitchType.HUMAN_INTELLIGENT_DETECTION),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.HUMAN_INTELLIGENT_DETECTION),
    ),
    EzvizSwitchEntityDescription(
        key="DEFENCE_PLAN",
        translation_key="defence_plan",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportSafeModePlan.value),  # 22
        value_fn=_switch_entry_value_fn(DeviceSwitchType.DEFENCE_PLAN),
        method=_switch_entry_method(DeviceSwitchType.DEFENCE_PLAN),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.DEFENCE_PLAN),
    ),
    EzvizSwitchEntityDescription(
        key="PARTIAL_IMAGE_OPTIMIZE",
        translation_key="partial_image_optimize",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportPartialImageOptimize.value),
        value_fn=_switch_entry_value_fn(DeviceSwitchType.PARTIAL_IMAGE_OPTIMIZE),
        method=_switch_entry_method(DeviceSwitchType.PARTIAL_IMAGE_OPTIMIZE),
        is_supported_fn=_has_switch_entry(DeviceSwitchType.PARTIAL_IMAGE_OPTIMIZE),
    ),
    # ---- Top-level flags in camera_data (not under "switches") ----
    EzvizSwitchEntityDescription(
        key="encrypted",
        translation_key="encrypted",
        supported_ext_key=str(SupportExt.SupportEncrypt.value),
        value_fn=lambda d: d.get("encrypted"),
        method=lambda client, serial, enable: client.set_video_enc(serial, enable),
    ),
    EzvizSwitchEntityDescription(
        key="push_notify_alarm",
        translation_key="push_notify_alarm",
        value_fn=lambda d: d.get("push_notify_alarm"),
        method=lambda client, serial, enable: client.do_not_disturb(serial, enable ^ 1),
    ),
    EzvizSwitchEntityDescription(
        key="push_notify_call",
        translation_key="push_notify_call",
        supported_ext_key=str(SupportExt.SupportAlarmVoice.value),
        value_fn=lambda d: d.get("push_notify_call"),
        method=lambda client, serial, enable: client.set_answer_call(
            serial, enable ^ 1
        ),
    ),
    EzvizSwitchEntityDescription(
        key="offline_notify",
        translation_key="offline_notify",
        value_fn=lambda d: d.get("offline_notify"),
        method=lambda client, serial, enable: client.set_offline_notification(
            serial, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="motion_detection",
        translation_key="motion_detection",
        supported_ext_key=str(SupportExt.SupportDefence.value),
        value_fn=lambda d: d.get("alarm_notify"),
        method=lambda client, serial, enable: client.set_camera_defence(serial, enable),
    ),
    EzvizSwitchEntityDescription(
        key="port_security_client_mode",
        translation_key="port_security_client_mode",
        device_class=SwitchDeviceClass.SWITCH,
        is_supported_fn=PORT_SECURITY_CLIENT_MODE.is_supported,
        value_fn=PORT_SECURITY_CLIENT_MODE.current_value,
        method=PORT_SECURITY_CLIENT_MODE.apply,
    ),
    EzvizSwitchEntityDescription(
        key="port_security_50161",
        translation_key="port_security_50161",
        device_class=SwitchDeviceClass.SWITCH,
        # EZVIZ Cloud only accepts port-security writes for the Link service on 50161.
        is_supported_fn=PORT_SECURITY_LINK.is_supported,
        value_fn=PORT_SECURITY_LINK.current_value,
        method=PORT_SECURITY_LINK.apply,
    ),
)


INTELLIGENT_APP_TRANSLATIONS: dict[str, str] = {
    "app_human_detect": "intelligent_app_human_detect",
    "app_car_detect": "intelligent_app_car_detect",
    "app_video_change": "intelligent_app_video_change",
    "app_wave_recognize": "intelligent_app_wave_recognize",
    "app_pir_detect": "intelligent_app_pir_detect",
}

INTELLIGENT_APP_SUPPORT_EXT: dict[str, tuple[str | None, list[str] | None]] = {
    "app_human_detect": ("508", ["2"]),
    "app_car_detect": ("508", ["2"]),
    "app_video_change": ("338", ["1"]),
    "app_wave_recognize": ("511", ["1"]),
}


INTELLIGENT_APP_EXTRA_PREDICATES: dict[str, Callable[[dict[str, Any]], bool]] = {}


INTELLIGENT_APP_DESCRIPTIONS: tuple[EzvizSwitchEntityDescription, ...] = tuple(
    EzvizSwitchEntityDescription(
        key=f"intelligent_app_{app_name}",
        translation_key=translation_key,
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=intelligent_app_value_fn(app_name),
        method=intelligent_app_method(app_name),
        supported_ext_key=INTELLIGENT_APP_SUPPORT_EXT.get(app_name, (None, None))[0],
        supported_ext_value=INTELLIGENT_APP_SUPPORT_EXT.get(app_name, (None, None))[
            1
        ],
        is_supported_fn=_combine_predicates(
            _has_intelligent_app(app_name),
            INTELLIGENT_APP_EXTRA_PREDICATES.get(app_name),
        ),
    )
    for app_name, translation_key in INTELLIGENT_APP_TRANSLATIONS.items()
)


SWITCHES: tuple[EzvizSwitchEntityDescription, ...] = (
    _STATIC_SWITCHES + INTELLIGENT_APP_DESCRIPTIONS
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ switches based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    await migrate_unique_ids_with_coordinator(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
        platform_domain="switch",
        allowed_keys=tuple(desc.key for desc in SWITCHES),
    )

    entities: list[SwitchEntity] = [
        EzvizSwitch(coordinator, serial, desc)
        for serial, camera_data in coordinator.data.items()
        for desc in SWITCHES
        if _is_desc_supported(camera_data, desc)
    ]

    async_add_entities(entities)


class EzvizSwitch(EzvizEntity, SwitchEntity):
    """Representation of an EZVIZ switch."""

    _attr_has_entity_name = True
    entity_description: EzvizSwitchEntityDescription

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: EzvizSwitchEntityDescription,
    ) -> None:
        """Set up EZVIZ switches from coordinator data."""
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_is_on = bool(description.value_fn(self.data))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on on the device."""
        try:
            await self.hass.async_add_executor_job(
                self.entity_description.method,
                self.coordinator.ezviz_client,
                self._serial,
                1,
            )
        except (HTTPError, PyEzvizError, InvalidHost) as err:
            raise HomeAssistantError(f"Failed to turn on switch {self.name}") from err

        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off on the device."""
        try:
            await self.hass.async_add_executor_job(
                self.entity_description.method,
                self.coordinator.ezviz_client,
                self._serial,
                0,
            )
        except (HTTPError, PyEzvizError, InvalidHost) as err:
            raise HomeAssistantError(f"Failed to turn off switch {self.name}") from err

        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = bool(self.entity_description.value_fn(self.data))
        super()._handle_coordinator_update()
