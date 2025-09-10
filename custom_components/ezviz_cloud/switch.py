"""Support for EZVIZ switches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyezvizapi import EzvizClient
from pyezvizapi.constants import SupportExt
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

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EzvizSwitchEntityDescription(SwitchEntityDescription):
    """EZVIZ switch with capability & device-category gating."""

    value_fn: Callable[[dict[str, Any]], Any]
    method: Callable[[EzvizClient, str, int], Any]
    supported_ext_key: str | None = None
    supported_ext_value: list[str] | None = None
    required_device_categories: tuple[str, ...] | None = None


def _is_desc_supported(
    camera_data: dict[str, Any], desc: EzvizSwitchEntityDescription
) -> bool:
    """Return True if this switch description is supported by the camera."""
    # 1) Device-category gating
    if desc.required_device_categories is not None:
        device_category = camera_data.get("device_category")
        if device_category not in desc.required_device_categories:
            return False

    # 2) Capability gating (supportExt)
    if desc.supported_ext_key is None:
        return True

    support_ext = camera_data.get("supportExt") or {}
    if not isinstance(support_ext, dict):
        return False

    current_val = support_ext.get(desc.supported_ext_key)
    if current_val is None:
        return False

    # Presence-only if no explicit values provided
    if not desc.supported_ext_value:
        return True

    current_val_str = str(current_val).strip()
    return any(current_val_str == opt.strip() for opt in desc.supported_ext_value)


SWITCHES: tuple[EzvizSwitchEntityDescription, ...] = (
    # Numeric "switches" from camera_data["switches"][ID]
    EzvizSwitchEntityDescription(
        key="ALARM_TONE",
        translation_key="voice_prompt",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAlarmVoice.value),
        supported_ext_value=None,  # presence of capability is enough
        value_fn=lambda d: (d.get("switches") or {}).get(1),
        method=lambda client, serial, enable: client.switch_status(serial, 1, enable),
    ),
    EzvizSwitchEntityDescription(
        key="LIGHT",
        translation_key="status_light",
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=lambda d: (d.get("switches") or {}).get(3),
        method=lambda client, serial, enable: client.switch_status(serial, 3, enable),
    ),
    EzvizSwitchEntityDescription(
        key="PRIVACY",
        translation_key="privacy",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportPtzPrivacy.value),
        value_fn=lambda d: (d.get("switches") or {}).get(7),
        method=lambda client, serial, enable: client.switch_status(serial, 7, enable),
    ),
    EzvizSwitchEntityDescription(
        key="INFRARED_LIGHT",
        translation_key="infrared_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportCloseInfraredLight.value),
        value_fn=lambda d: (d.get("switches") or {}).get(10),
        method=lambda client, serial, enable: client.switch_status(serial, 10, enable),
    ),
    EzvizSwitchEntityDescription(
        key="SLEEP",
        translation_key="sleep",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportSleep.value),
        value_fn=lambda d: (d.get("switches") or {}).get(21),
        method=lambda client, serial, enable: client.switch_status(serial, 21, enable),
    ),
    EzvizSwitchEntityDescription(
        key="SOUND",
        translation_key="audio",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAudioOnoff.value),
        value_fn=lambda d: (d.get("switches") or {}).get(22),
        method=lambda client, serial, enable: client.switch_status(serial, 22, enable),
    ),
    EzvizSwitchEntityDescription(
        key="MOBILE_TRACKING",
        translation_key="motion_tracking",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportIntelligentTrack.value),
        value_fn=lambda d: (d.get("switches") or {}).get(25),
        method=lambda client, serial, enable: client.switch_status(serial, 25, enable),
    ),
    EzvizSwitchEntityDescription(
        key="ALL_DAY_VIDEO",
        translation_key="all_day_video_recording",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportFullDayRecord.value),
        value_fn=lambda d: (d.get("switches") or {}).get(29),
        method=lambda client, serial, enable: client.switch_status(serial, 29, enable),
    ),
    EzvizSwitchEntityDescription(
        key="AUTO_SLEEP",
        translation_key="auto_sleep",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportAutoSleep.value),
        value_fn=lambda d: (d.get("switches") or {}).get(32),
        method=lambda client, serial, enable: client.switch_status(serial, 32, enable),
    ),
    EzvizSwitchEntityDescription(
        key="LIGHT_FLICKER",
        translation_key="flicker_light_on_movement",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportActiveDefense.value),
        value_fn=lambda d: (d.get("switches") or {}).get(301),
        method=lambda client, serial, enable: client.switch_status(serial, 301, enable),
    ),
    EzvizSwitchEntityDescription(
        key="ALARM_LIGHT_RELEVANCE",
        translation_key="pir_motion_activated_light",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportLightRelate.value),
        value_fn=lambda d: (d.get("switches") or {}).get(305),
        method=lambda client, serial, enable: client.switch_status(serial, 305, enable),
    ),
    EzvizSwitchEntityDescription(
        key="TAMPER_ALARM",
        translation_key="tamper_alarm",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportTamperAlarm.value),
        value_fn=lambda d: (d.get("switches") or {}).get(306),
        method=lambda client, serial, enable: client.switch_status(serial, 306, enable),
    ),
    EzvizSwitchEntityDescription(
        key="TRACKING",
        translation_key="follow_movement",
        device_class=SwitchDeviceClass.SWITCH,
        supported_ext_key=str(SupportExt.SupportTracking.value),
        value_fn=lambda d: (d.get("switches") or {}).get(650),
        method=lambda client, serial, enable: client.switch_status(serial, 650, enable),
    ),
    EzvizSwitchEntityDescription(
        key="WATERMARK",
        translation_key="watermark",
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=lambda d: (d.get("switches") or {}).get(702),
        method=lambda client, serial, enable: client.switch_status(serial, 702, enable),
    ),
    # Top-level flags in camera_data (not under "switches")
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
        supported_ext_key=str(SupportExt.SupportAlarmVoice.value),
        value_fn=lambda d: d.get("offline_notify"),
        method=lambda client, serial, enable: client.set_offline_notification(
            serial, enable
        ),
    ),
    EzvizSwitchEntityDescription(
        key="alarm_notify",
        translation_key="motion_detection",
        supported_ext_key=str(SupportExt.SupportDefence.value),
        value_fn=lambda d: d.get("alarm_notify"),
        method=lambda client, serial, enable: client.set_camera_defence(serial, enable),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up EZVIZ switches based on a config entry."""
    coordinator: EzvizDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    key_renames: dict[str, str] = {
        "motion_detection": "alarm_notify",  # legacy key -> new canonical key
    }

    await migrate_unique_ids_with_coordinator(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
        platform_domain="switch",
        allowed_keys=tuple(desc.key for desc in SWITCHES),
        key_renames=key_renames,
    )

    entities: list[EzvizSwitch] = []
    for serial, camera_data in coordinator.data.items():
        for desc in SWITCHES:
            if not _is_desc_supported(camera_data, desc):
                continue
            state_val = desc.value_fn(camera_data)
            if state_val is not None:
                entities.append(EzvizSwitch(coordinator, serial, desc))

    if entities:
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
