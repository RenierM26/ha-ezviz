"""Support for EZVIZ switches."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import json
from typing import Any

from pyezvizapi import EzvizClient
from pyezvizapi.constants import DeviceCatagories, DeviceSwitchType, SupportExt
from pyezvizapi.exceptions import HTTPError, InvalidHost, PyEzvizError

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
from .utility import device_category, device_model, support_ext_has

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizSwitchEntityDescription(SwitchEntityDescription):
    """EZVIZ switch with capability & device-category gating."""

    value_fn: Callable[[dict[str, Any]], Any]
    method: Callable[[EzvizClient, str, int], Any]
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None

    # NEW (safe no-ops until you populate these in coordinator.data):
    required_device_categories: tuple[str, ...] | None = None
    allowed_models: tuple[str, ...] | None = None
    blocked_models: tuple[str, ...] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any], desc: EzvizSwitchEntityDescription
) -> bool:
    """Return True if this switch description is supported by the camera."""
    # 1) Device-category gating (optional)
    if desc.required_device_categories is not None:
        if device_category(camera_data) not in desc.required_device_categories:
            return False

    # 2) Model allow/block (optional)
    model = device_model(camera_data)
    if desc.allowed_models is not None and model not in desc.allowed_models:
        return False
    if desc.blocked_models is not None and model in desc.blocked_models:
        return False

    # 3) Capability gating (SupportExt)
    if desc.supported_ext_key is None:
        return True
    return support_ext_has(
        camera_data, desc.supported_ext_key, desc.supported_ext_value
    )


SWITCHES: tuple[EzvizSwitchEntityDescription, ...] = (
    # ---- Numeric "switches" from camera_data["switches"][ID] ----
    EzvizSwitchEntityDescription(
        key="ALARM_TONE",
        translation_key="voice_prompt",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAlarmVoice.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.ALARM_TONE.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.ALARM_TONE.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="LIGHT",
        translation_key="status_light",
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=lambda d: (d.get("switches") or {}).get(DeviceSwitchType.LIGHT.value),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.LIGHT.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="PRIVACY",
        translation_key="privacy",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportPtzPrivacy.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.PRIVACY.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.PRIVACY.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="INFRARED_LIGHT",
        translation_key="infrared_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportCloseInfraredLight.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.INFRARED_LIGHT.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.INFRARED_LIGHT.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="SLEEP",
        translation_key="sleep",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportSleep.value),
        value_fn=lambda d: (d.get("switches") or {}).get(DeviceSwitchType.SLEEP.value),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.SLEEP.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="SOUND",
        translation_key="audio",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAudioOnoff.value),
        value_fn=lambda d: (d.get("switches") or {}).get(DeviceSwitchType.SOUND.value),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.SOUND.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="MOBILE_TRACKING",
        translation_key="motion_tracking",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportIntelligentTrack.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.MOBILE_TRACKING.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.MOBILE_TRACKING.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="WDR",
        translation_key="wdr",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportWideDynamicRange.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.WIDE_DYNAMIC_RANGE.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.WIDE_DYNAMIC_RANGE.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="DISTORTION_CORRECTION",
        translation_key="distortion_correction",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportDistortionCorrection.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.DISTORTION_CORRECTION.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.DISTORTION_CORRECTION.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="ALL_DAY_VIDEO",
        translation_key="all_day_video_recording",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportFullDayRecord.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.ALL_DAY_VIDEO.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.ALL_DAY_VIDEO.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="AUTO_SLEEP",
        translation_key="auto_sleep",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAutoSleep.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.AUTO_SLEEP.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.AUTO_SLEEP.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="LIGHT_FLICKER",
        translation_key="flicker_light_on_movement",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportActiveDefense.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.LIGHT_FLICKER.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.LIGHT_FLICKER.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="ALARM_LIGHT_RELEVANCE",
        translation_key="pir_motion_activated_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportLightRelate.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.ALARM_LIGHT_RELEVANCE.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.ALARM_LIGHT_RELEVANCE.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="TAMPER_ALARM",
        translation_key="tamper_alarm",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportTamperAlarm.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.TAMPER_ALARM.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.TAMPER_ALARM.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="TRACKING",
        translation_key="follow_movement",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportTracking.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.TRACKING.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.TRACKING.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="WATERMARK",
        translation_key="logo_watermark",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportLogoWatermark.value),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.LOGO_WATERMARK.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.LOGO_WATERMARK.value, enable
        ),
    ),
    # ---- New: additional useful app booleans ----
    EzvizSwitchEntityDescription(
        key="CHANNELOFFLINE",
        translation_key="channel_offline_notify",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportChannelOffline.value),  # 70
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.CHANNELOFFLINE.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.CHANNELOFFLINE.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="WIFI_LIGHT",
        translation_key="wifi_status_light",
        device_class=SwitchDeviceClass.SWITCH,
        required_device_categories=(
            DeviceCatagories.W2H_BASE_STATION_DEVICE_CATEGORY.value
        ),
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.WIFI_LIGHT.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.WIFI_LIGHT.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="OUTLET_RECOVER",
        translation_key="outlet_power_recover",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportPoweroffRecovery.value),  # 189
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.OUTLET_RECOVER.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.OUTLET_RECOVER.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="OUTDOOR_RINGING_SOUND",
        translation_key="outdoor_ringing_sound",
        device_class=SwitchDeviceClass.SWITCH,
        # Matches app gating better than 241 (ring selection)
        supported_ext_key=str(SupportExt.SupportBellSet.value),  # 164
        # required_device_categories=("BDoorBell",),  # uncomment once categories are present
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.OUTDOOR_RINGING_SOUND.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.OUTDOOR_RINGING_SOUND.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="INTELLIGENT_PQ_SWITCH",
        translation_key="intelligent_picture_quality",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportIntelligentPQSwitch.value),  # 366
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.INTELLIGENT_PQ_SWITCH.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.INTELLIGENT_PQ_SWITCH.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="HUMAN_INTELLIGENT_DETECTION",
        translation_key="people_detection",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportSmartBodyDetect.value),  # 244
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.HUMAN_INTELLIGENT_DETECTION.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.HUMAN_INTELLIGENT_DETECTION.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="DEFENCE_PLAN",
        translation_key="defence_plan",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportSafeModePlan.value),  # 22
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.DEFENCE_PLAN.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.DEFENCE_PLAN.value, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="PARTIAL_IMAGE_OPTIMIZE",
        translation_key="partial_image_optimize",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportPartialImageOptimize.value),  # optional
        value_fn=lambda d: (d.get("switches") or {}).get(
            DeviceSwitchType.PARTIAL_IMAGE_OPTIMIZE.value
        ),
        method=lambda client, serial, enable: client.switch_status(
            serial, DeviceSwitchType.PARTIAL_IMAGE_OPTIMIZE.value, enable
        ),
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
)


INTELLIGENT_APP_TRANSLATIONS: dict[str, str] = {
    "app_human_detect": "intelligent_app_human_detect",
    "app_car_detect": "intelligent_app_car_detect",
    "app_video_change": "intelligent_app_video_change",
    "app_wave_recognize": "intelligent_app_wave_recognize",
}


def _iter_intelligent_apps(camera_data: dict[str, Any]) -> Iterator[tuple[str, bool]]:
    """Yield tuples of (base_app_name, enabled_flag) for known intelligent apps."""

    intelligent_app: Any = None
    feature = camera_data.get("FEATURE_INFO")
    if isinstance(feature, dict):
        for group in feature.values():
            if not isinstance(group, dict):
                continue
            video_section = group.get("Video")
            if isinstance(video_section, dict) and "IntelligentAPP" in video_section:
                intelligent_app = video_section["IntelligentAPP"]
                break
            if "IntelligentAPP" in group:
                intelligent_app = group["IntelligentAPP"]
                break
    if intelligent_app is None:
        return

    if isinstance(intelligent_app, str):
        try:
            intelligent_app = json.loads(intelligent_app)
        except json.JSONDecodeError:
            intelligent_app = {}

    if not isinstance(intelligent_app, dict):
        return

    downloaded: Any = intelligent_app.get("DownloadedAPP")
    if isinstance(downloaded, str):
        try:
            downloaded = json.loads(downloaded)
        except json.JSONDecodeError:
            downloaded = {}

    if not isinstance(downloaded, dict):
        return

    apps: Any = downloaded.get("APP", [])
    if isinstance(apps, str):
        try:
            apps = json.loads(apps)
        except json.JSONDecodeError:
            apps = []

    if not isinstance(apps, list):
        return

    for app in apps:
        if not isinstance(app, dict):
            continue
        raw_app_id = app.get("APPID")
        if not isinstance(raw_app_id, str):
            continue
        base = raw_app_id.split("$:$", 1)[0]
        if base not in INTELLIGENT_APP_TRANSLATIONS:
            continue
        enabled = bool(app.get("enabled"))
        yield base, enabled


def _intelligent_app_value_fn(app_name: str) -> Callable[[dict[str, Any]], bool]:
    """Construct a value extractor for the given intelligent app."""

    def _value_fn(camera_data: dict[str, Any]) -> bool:
        for name, enabled in _iter_intelligent_apps(camera_data):
            if name == app_name:
                return enabled
        return False

    return _value_fn


def _intelligent_app_method(app_name: str) -> Callable[[EzvizClient, str, int], bool]:
    """Construct a setter for the given intelligent app."""

    def _method(client: EzvizClient, serial: str, enable: int) -> bool:
        return bool(client.set_intelligent_app_state(serial, app_name, bool(enable)))

    return _method


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
        allowed_keys=tuple(desc.key for desc in SWITCHES)
        + tuple(f"intelligent_app_{name}" for name in INTELLIGENT_APP_TRANSLATIONS),
    )

    entities: list[SwitchEntity] = [
        EzvizSwitch(coordinator, serial, desc)
        for serial, camera_data in coordinator.data.items()
        for desc in SWITCHES
        if _is_desc_supported(camera_data, desc)
    ]

    for serial, camera_data in coordinator.data.items():
        for app_name, _enabled in _iter_intelligent_apps(camera_data):
            translation_key = INTELLIGENT_APP_TRANSLATIONS[app_name]
            dynamic_desc = EzvizSwitchEntityDescription(
                key=f"intelligent_app_{app_name}",
                translation_key=translation_key,
                device_class=SwitchDeviceClass.SWITCH,
                value_fn=_intelligent_app_value_fn(app_name),
                method=_intelligent_app_method(app_name),
            )
            entities.append(EzvizSwitch(coordinator, serial, dynamic_desc))

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
